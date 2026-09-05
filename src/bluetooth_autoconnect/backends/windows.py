"""Windows backend — WinRT Bluetooth APIs.

Uses the Windows Runtime (WinRT) APIs exposed by the ``winrt`` package
(``pip install winrt-Windows.Devices.Bluetooth`` etc.).

Minimum requirements
--------------------
* Windows 10 version 1809 (build 17763) or Windows 11
* Python 3.10+
* ``winrt`` package — installed automatically with
  ``pip install bluetooth-autoconnect[windows]``

Design notes
------------
WinRT's Bluetooth stack differs from BlueZ in several important ways:

* **No explicit pairing concept in the auto-connect sense.**  Windows
  manages pairing through its own device consent dialog.  Devices that
  appear in "Bluetooth & devices" with a green tick are considered both
  paired *and* trusted for our purposes.

* **No ``Adapter.path`` analogue.**  WinRT exposes adapter information
  through ``Windows.Devices.Radios``.  We use the adapter's Bluetooth
  address as its ``path`` so the daemon's adapter-path logic continues to
  work without changes.

* **Event model uses WinRT event tokens** rather than D-Bus match rules.
  The ``BluetoothLEDevice`` and ``DeviceWatcher`` classes fire synchronous
  CLR-style events; we bridge these into asyncio tasks via a queue.

* **``set_adapter_powered`` raises ``BackendUnsupportedError``.**  The
  Windows OS manages radio power state; we cannot set it programmatically
  in a way that is safe across all device configurations.  Users should
  use Windows Settings.

* **``connect_device`` uses ``FromBluetoothAddressAsync`` + GATT/RFCOMM
  service lookup** to initiate a connection.  For Classic Bluetooth devices
  we use ``RfcommDeviceService``; for BLE devices we use GATT services.
  The first successful service connection triggers ``Connected=True``
  in our synthetic event stream.

Event path
----------
WinRT fires ``DeviceWatcher`` events and device ``ConnectionStatusChanged``
events on the WinRT thread pool.  We push synthetic ``EventCallback``
payloads onto an ``asyncio.Queue`` and drain it in ``_event_pump_loop``,
which runs as a background task alongside the daemon's periodic scanner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..exceptions import (
    BackendNotAvailableError,
    BackendUnsupportedError,
    DeviceConnectionError,
)
from ..models import Adapter, Device

logger = logging.getLogger("bluetooth_autoconnect.backends.windows")

EventCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]

# Synthetic interface names used in EventCallback payloads so that
# daemon._on_dbus_event() can handle Windows events without change.
# These mirror the Linux D-Bus interface names the daemon already reacts to.
_ADAPTER_IFACE = "org.bluez.Adapter1"
_DEVICE_IFACE = "org.bluez.Device1"

# WinRT connection status values
_CONNECTION_STATUS_CONNECTED = 1
_CONNECTION_STATUS_DISCONNECTED = 0


def _require_winrt() -> None:
    """Raise if the winrt package (or its sub-packages) are not installed."""
    try:
        import winrt.windows.devices.bluetooth  # noqa: F401
        import winrt.windows.devices.enumeration  # noqa: F401
        import winrt.windows.devices.radios  # noqa: F401
    except ImportError as exc:
        raise BackendNotAvailableError(
            "The 'winrt' package is required for Windows Bluetooth support. "
            "Install it with: pip install bluetooth-autoconnect[windows]"
        ) from exc


def _mac_from_uint64(address: int) -> str:
    """Convert a WinRT uint64 Bluetooth address to ``AA:BB:CC:DD:EE:FF``."""
    parts = []
    for _ in range(6):
        parts.append(f"{address & 0xFF:02X}")
        address >>= 8
    return ":".join(reversed(parts))


def _mac_to_uint64(mac: str) -> int:
    """Convert ``AA:BB:CC:DD:EE:FF`` to a WinRT uint64 Bluetooth address."""
    parts = mac.split(":")
    result = 0
    for part in parts:
        result = (result << 8) | int(part, 16)
    return result


class WindowsBackend:
    """``BluetoothBackend`` implementation backed by Windows Runtime APIs.

    Instantiate via
    :func:`~bluetooth_autoconnect.backends.create_backend` rather than
    directly so the correct backend is selected automatically.
    """

    def __init__(self) -> None:
        _require_winrt()
        self._callback: EventCallback | None = None
        self._event_queue: asyncio.Queue[tuple[str, str, str, dict[str, Any]]] = (
            asyncio.Queue()
        )
        self._pump_task: asyncio.Task[None] | None = None
        # MAC → WinRT BluetoothDevice object (kept alive for event tokens)
        self._watched_devices: dict[str, Any] = {}
        # MAC → event_registration_token for ConnectionStatusChanged
        self._connection_tokens: dict[str, Any] = {}
        # Current known connection states: MAC → bool
        self._connected_state: dict[str, bool] = {}
        self._watcher: Any = None  # DeviceWatcher
        self._watcher_added_token: Any = None
        self._watcher_updated_token: Any = None
        self._watcher_removed_token: Any = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Verify WinRT is available and start the device watcher."""
        # Build an AQS query for all paired Bluetooth devices (Classic + LE)
        import winrt.windows.devices.bluetooth as bt
        import winrt.windows.devices.enumeration as de

        aqf_classic = bt.BluetoothDevice.get_device_selector()
        bt.BluetoothLEDevice.get_device_selector()

        # We watch Classic devices; LE events come from a separate watcher.
        self._watcher = de.DeviceInformation.create_watcher(aqf_classic)

        def _on_added(sender: Any, info: Any) -> None:
            self._event_queue.put_nowait(
                (
                    "added",
                    str(info.id),
                    _DEVICE_IFACE,
                    {"Id": str(info.id), "Name": str(info.name)},
                )
            )

        def _on_updated(sender: Any, update: Any) -> None:
            self._event_queue.put_nowait(
                (
                    "properties_changed",
                    str(update.id),
                    _DEVICE_IFACE,
                    {"Id": str(update.id)},
                )
            )

        def _on_removed(sender: Any, info: Any) -> None:
            self._event_queue.put_nowait(("removed", str(info.id), _DEVICE_IFACE, {}))

        self._watcher_added_token = self._watcher.add_added(_on_added)
        self._watcher_updated_token = self._watcher.add_updated(_on_updated)
        self._watcher_removed_token = self._watcher.add_removed(_on_removed)
        self._watcher.start()
        logger.debug("WindowsBackend: DeviceWatcher started.")

    async def close(self) -> None:
        """Stop the device watcher and release all event tokens."""
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None

        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:  # noqa: BLE001
                pass
            self._watcher = None

        # Release per-device event tokens
        for mac, device_obj in self._watched_devices.items():
            token = self._connection_tokens.get(mac)
            if token is not None:
                try:
                    device_obj.remove_connection_status_changed(token)
                except Exception:  # noqa: BLE001
                    pass
        self._watched_devices.clear()
        self._connection_tokens.clear()
        logger.debug("WindowsBackend: closed.")

    # ── Adapter enumeration ───────────────────────────────────────────────

    async def get_adapters(self) -> list[Adapter]:
        """Return all Bluetooth radios visible to Windows.

        Uses ``Windows.Devices.Radios`` to enumerate Bluetooth radios
        and maps them to :class:`~bluetooth_autoconnect.models.Adapter`.
        """
        import winrt.windows.devices.radios as radios

        result: list[Adapter] = []
        try:
            radio_list = await radios.Radio.get_radios_async()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WindowsBackend: could not enumerate radios: %s", exc)
            return result

        for radio in radio_list:
            from winrt.windows.devices.radios import RadioKind, RadioState

            if radio.kind != RadioKind.BLUETOOTH:
                continue
            powered = radio.state == RadioState.ON
            # WinRT Radio does not expose a MAC address directly.
            # We use the radio name as the address field; the daemon
            # uses address for logging only, not for connection.
            result.append(
                Adapter(
                    path=radio.name,  # used as opaque ID
                    name=radio.name,
                    address=radio.name,  # best available identifier
                    powered=powered,
                )
            )
        return result

    # ── Device enumeration ────────────────────────────────────────────────

    async def get_devices(self, adapter_path: str | None = None) -> list[Device]:
        """Return all paired Bluetooth devices known to Windows.

        Uses ``DeviceInformation.find_all_async`` with a selector for
        paired Classic Bluetooth devices.  BLE devices are included if
        they appear via their paired service entries.

        Parameters
        ----------
        adapter_path:
            Not used on Windows (the OS does not expose multi-adapter
            APIs in WinRT at the device level).  Accepted for interface
            compatibility; ignored.
        """
        import winrt.windows.devices.bluetooth as bt
        import winrt.windows.devices.enumeration as de

        devices: list[Device] = []

        # ── Classic Bluetooth ─────────────────────────────────────────────
        try:
            selector = bt.BluetoothDevice.get_device_selector()
            properties = ["System.Devices.Aep.Bluetooth.IsPaired"]
            info_collection = await de.DeviceInformation.find_all_async(
                selector,
                properties,
            )
            for info in info_collection:
                try:
                    bt_device = await bt.BluetoothDevice.from_id_async(info.id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "WindowsBackend: could not open Classic device %s: %s",
                        info.id,
                        exc,
                    )
                    continue

                mac = _mac_from_uint64(bt_device.bluetooth_address)
                from winrt.windows.devices.bluetooth import BluetoothConnectionStatus

                connected = (
                    bt_device.connection_status == BluetoothConnectionStatus.CONNECTED
                )
                self._connected_state[mac] = connected

                devices.append(
                    Device(
                        path=mac,  # MAC address used as opaque path on Windows
                        address=mac,
                        name=bt_device.name or mac,
                        adapter_path=adapter_path or "",
                        paired=True,  # selector only returns paired devices
                        trusted=True,  # paired = trusted on Windows
                        connected=connected,
                    )
                )
                # Register for connection-status events if not already tracked
                if mac not in self._watched_devices:
                    self._watched_devices[mac] = bt_device
                    token = bt_device.add_connection_status_changed(
                        self._make_connection_status_handler(mac)
                    )
                    self._connection_tokens[mac] = token

        except Exception as exc:  # noqa: BLE001
            logger.warning("WindowsBackend: error enumerating Classic devices: %s", exc)

        # ── Bluetooth LE ──────────────────────────────────────────────────
        try:
            selector_le = bt.BluetoothLEDevice.get_device_selector()
            info_le = await de.DeviceInformation.find_all_async(selector_le)
            for info in info_le:
                try:
                    le_device = await bt.BluetoothLEDevice.from_id_async(info.id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "WindowsBackend: could not open LE device %s: %s",
                        info.id,
                        exc,
                    )
                    continue

                mac = _mac_from_uint64(le_device.bluetooth_address)
                if mac in {d.address for d in devices}:
                    # Already listed via Classic path
                    continue

                from winrt.windows.devices.bluetooth import BluetoothConnectionStatus

                connected = (
                    le_device.connection_status == BluetoothConnectionStatus.CONNECTED
                )
                self._connected_state[mac] = connected

                devices.append(
                    Device(
                        path=mac,
                        address=mac,
                        name=le_device.name or mac,
                        adapter_path=adapter_path or "",
                        paired=True,
                        trusted=True,
                        connected=connected,
                    )
                )
                if mac not in self._watched_devices:
                    self._watched_devices[mac] = le_device
                    token = le_device.add_connection_status_changed(
                        self._make_connection_status_handler(mac)
                    )
                    self._connection_tokens[mac] = token

        except Exception as exc:  # noqa: BLE001
            logger.warning("WindowsBackend: error enumerating LE devices: %s", exc)

        return devices

    # ── Adapter control ───────────────────────────────────────────────────

    async def set_adapter_powered(self, adapter_path: str, powered: bool) -> None:
        """Not supported on Windows.

        Windows manages Bluetooth radio state through its own consent UI.
        Applications cannot reliably enable/disable the radio without
        explicit user interaction.

        Raises
        ------
        BackendUnsupportedError
            Always.
        """
        raise BackendUnsupportedError(
            "set_adapter_powered is not supported on Windows. "
            "Use Windows Settings → Bluetooth & devices to control the radio."
        )

    # ── Device connection ─────────────────────────────────────────────────

    async def connect_device(self, device_path: str) -> None:
        """Connect to the device whose MAC address is *device_path*.

        Attempts a Classic Bluetooth RFCOMM connection first; falls back
        to a BLE GATT connection.  Raises :class:`DeviceConnectionError`
        if both fail.

        Parameters
        ----------
        device_path:
            MAC address in ``AA:BB:CC:DD:EE:FF`` format (used as ``path``
            for Windows devices).

        Raises
        ------
        DeviceConnectionError
            Both RFCOMM and GATT connection attempts failed.
        """
        import winrt.windows.devices.bluetooth as bt

        mac = device_path  # on Windows, path == MAC
        address_int = _mac_to_uint64(mac)

        # ── Try Classic RFCOMM ────────────────────────────────────────────
        try:
            bt_device = await bt.BluetoothDevice.from_bluetooth_address_async(
                address_int
            )
            services = await bt_device.get_rfcomm_services_async()
            if services and services.error == 0 and len(services.services) > 0:
                # Opening a service handle triggers Windows to establish the
                # underlying ACL connection; we open and immediately close it.
                service = services.services[0]
                import winrt.windows.networking.sockets as sockets

                sock = sockets.StreamSocket()
                host_name = service.connection_host_name
                service_name = service.connection_service_name
                await sock.connect_async(host_name, service_name)
                sock.close()
                logger.info(
                    "WindowsBackend: Classic RFCOMM connect succeeded mac=%s", mac
                )
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("WindowsBackend: Classic connect failed mac=%s: %s", mac, exc)

        # ── Try BLE GATT ──────────────────────────────────────────────────
        try:
            le_device = await bt.BluetoothLEDevice.from_bluetooth_address_async(
                address_int
            )
            # Requesting any GATT service forces Windows to connect.
            gatt_result = await le_device.get_gatt_services_async()
            if gatt_result and gatt_result.status == 0:
                logger.info("WindowsBackend: BLE GATT connect succeeded mac=%s", mac)
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("WindowsBackend: BLE connect failed mac=%s: %s", mac, exc)

        raise DeviceConnectionError(
            mac,
            "Both Classic RFCOMM and BLE GATT connection attempts failed.",
        )

    # ── Event subscription ────────────────────────────────────────────────

    async def subscribe(self, callback: EventCallback) -> None:
        """Start the async event pump and register *callback*.

        Events collected from WinRT handlers (device watcher,
        connection-status changes) are queued and dispatched here via
        :meth:`_event_pump_loop`.

        Parameters
        ----------
        callback:
            Async callable matching the ``EventCallback`` signature.
        """
        self._callback = callback
        self._pump_task = asyncio.get_event_loop().create_task(
            self._event_pump_loop(),
            name="windows-event-pump",
        )
        logger.debug("WindowsBackend: event pump started.")

    # ── Internal event handling ───────────────────────────────────────────

    def _make_connection_status_handler(self, mac: str) -> Any:
        """Return a WinRT-compatible sync event handler for *mac*.

        The handler is called by Windows on a thread-pool thread whenever
        the device's connection status changes.  It enqueues a synthetic
        ``properties_changed`` event into the asyncio queue so the async
        event pump can dispatch it as an ``EventCallback``.
        """

        def _handler(sender: Any, _args: Any) -> None:
            import winrt.windows.devices.bluetooth as bt

            try:
                connected = (
                    sender.connection_status == bt.BluetoothConnectionStatus.CONNECTED
                )
            except Exception:  # noqa: BLE001
                return

            previous = self._connected_state.get(mac)
            if previous == connected:
                return  # no real state change; discard

            self._connected_state[mac] = connected
            self._event_queue.put_nowait(
                (
                    "properties_changed",
                    mac,  # used as object_path
                    _DEVICE_IFACE,
                    {"Connected": connected},
                )
            )
            logger.debug(
                "WindowsBackend: connection status changed mac=%s connected=%s",
                mac,
                connected,
            )

        return _handler

    async def _event_pump_loop(self) -> None:
        """Drain the WinRT event queue and forward items to the callback.

        Runs as a background asyncio task.  Exits cleanly on cancellation.
        """
        while True:
            try:
                event_type, path, iface, changed = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if self._callback is not None:
                try:
                    await self._callback(event_type, path, iface, changed)
                except Exception:  # noqa: BLE001
                    logger.exception("WindowsBackend: error in event callback")

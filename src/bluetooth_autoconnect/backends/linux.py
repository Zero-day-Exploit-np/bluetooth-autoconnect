"""Linux backend — BlueZ via D-Bus (``dbus-next``).

This module is the direct evolution of the original ``dbus_client.py``.
The public class is ``LinuxBackend``; the ``dbus_client`` module re-exports
it as ``BlueZClient`` for backward compatibility with existing code and tests.

Only this file imports ``dbus_next``.  Every other module in the package
receives a ``BluetoothBackend``-typed reference and never imports D-Bus
types directly.

Discovery support
-----------------
``start_discovery(adapter_path)`` and ``stop_discovery(adapter_path)``
call ``org.bluez.Adapter1.StartDiscovery()`` / ``StopDiscovery()`` over
D-Bus.  The daemon uses these during its periodic scan windows to actively
scan for paired devices that have returned to range.

BlueZ discovery sessions are reference-counted per client.  Calling
``StartDiscovery()`` when another session is already active returns
``org.bluez.Error.InProgress`` — this is NOT an error; it means scanning
is already underway and we continue listening for events normally.
Calling ``StopDiscovery()`` only releases *our* session; other clients
keep their sessions running.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus, ProxyInterface, ProxyObject

from ..exceptions import (
    BlueZNotAvailableError,
    DBusConnectionError,
)
from ..models import Adapter, Device

# ── D-Bus constants ───────────────────────────────────────────────────────────

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

# BlueZ error names we handle explicitly in discovery calls.
_BLUEZ_IN_PROGRESS = "org.bluez.Error.InProgress"
_BLUEZ_NOT_READY = "org.bluez.Error.NotReady"
_BLUEZ_FAILED = "org.bluez.Error.Failed"

logger = logging.getLogger("bluetooth_autoconnect.backends.linux")

EventCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
_NOT_CONNECTED = "LinuxBackend is not connected; call connect() first."


# ── Internal helpers ──────────────────────────────────────────────────────────


def _unwrap(value: Any) -> Any:  # noqa: ANN401
    """Recursively unwrap ``dbus_next.Variant`` wrappers into plain Python."""
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def _schedule(coro: Any) -> None:  # noqa: ANN401
    """Schedule *coro* on the running asyncio event loop.

    D-Bus signal callbacks are called *synchronously* by dbus-next from
    within its message-dispatch loop.  We cannot ``await`` from there, so
    we post the coroutine as a task instead.

    Uses ``asyncio.get_running_loop()`` (Python 3.7+) which is the correct
    API for obtaining the *currently executing* loop from within an async
    context.  ``asyncio.get_event_loop()`` is deprecated for this use-case
    in Python 3.10+ and may return the wrong loop or silently create a new
    one, causing reconnect events to be dropped.

    Falls back to a WARNING (not DEBUG) when no loop is running, so that
    discarded reconnect events are visible in logs rather than invisible.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop — can happen in unit tests that exercise the
        # synchronous callback helpers outside an async context.
        # In production (daemon mode) this must never occur; log at WARNING
        # so it is visible if it does.
        logger.warning(
            "_schedule: no running event loop — reconnect event discarded. "
            "This is a bug; please report it."
        )


def _bluez_error_name(exc: Exception) -> str | None:
    """Extract the BlueZ D-Bus error name from an exception, if any."""
    error_type = getattr(exc, "type", None)
    if error_type is not None:
        return str(error_type)
    msg = str(exc)
    for candidate in (_BLUEZ_IN_PROGRESS, _BLUEZ_NOT_READY, _BLUEZ_FAILED):
        if candidate in msg:
            return candidate
    return None


# ── LinuxBackend ──────────────────────────────────────────────────────────────


class LinuxBackend:
    """``BluetoothBackend`` implementation backed by BlueZ over D-Bus.

    Instantiate via :func:`~bluetooth_autoconnect.backends.create_backend`
    rather than directly so the correct backend is selected automatically.

    All public methods are async and satisfy the
    :class:`~bluetooth_autoconnect.backends.BluetoothBackend` protocol.
    """

    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._bluez_root: ProxyObject | None = None
        # Declared as ProxyInterface so mypy accepts the assignment.
        self._object_manager: ProxyInterface | None = None
        # Track which adapters we have called StartDiscovery() on, so we
        # can call StopDiscovery() symmetrically.
        self._discovery_active: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the D-Bus system bus and locate the BlueZ service.

        Raises
        ------
        DBusConnectionError
            The system bus is not reachable.
        BlueZNotAvailableError
            The ``org.bluez`` service is not registered on the bus.
        """
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:  # noqa: BLE001
            raise DBusConnectionError(
                f"Could not connect to the D-Bus system bus: {exc}"
            ) from exc

        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, "/")
            self._bluez_root = self._bus.get_proxy_object(
                BLUEZ_SERVICE, "/", introspection
            )
            self._object_manager = self._bluez_root.get_interface(OBJECT_MANAGER_IFACE)
        except Exception as exc:  # noqa: BLE001
            raise BlueZNotAvailableError(
                "org.bluez is not available on the system bus. "
                "Is bluetooth.service running and BlueZ installed?"
            ) from exc

    async def close(self) -> None:
        """Disconnect from the D-Bus system bus.

        Any active discovery sessions are stopped before disconnecting so
        BlueZ can release its scan resources.
        """
        # Best-effort: stop any discovery sessions we started.
        for adapter_path in list(self._discovery_active):
            try:
                await self.stop_discovery(adapter_path)
            except Exception:  # noqa: BLE001
                pass
        self._discovery_active.clear()

        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    # ── Device enumeration ────────────────────────────────────────────────

    async def get_managed_objects(self) -> dict[str, Any]:
        """Return the full BlueZ object tree as a plain Python dict.

        All ``dbus_next.Variant`` wrappers are stripped by ``_unwrap``.

        Raises
        ------
        DBusConnectionError
            :meth:`connect` has not been called yet.
        """
        if self._object_manager is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        objects = await cast(Any, self._object_manager).call_get_managed_objects()
        return _unwrap(objects)

    async def get_adapters(self) -> list[Adapter]:
        """Return all Bluetooth adapters registered with BlueZ."""
        objects = await self.get_managed_objects()
        adapters: list[Adapter] = []
        for path, interfaces in objects.items():
            props = interfaces.get(ADAPTER_IFACE)
            if props is None:
                continue
            adapters.append(
                Adapter(
                    path=path,
                    name=props.get("Alias", path.rsplit("/", 1)[-1]),
                    address=props.get("Address", ""),
                    powered=bool(props.get("Powered", False)),
                )
            )
        return adapters

    async def get_devices(self, adapter_path: str | None = None) -> list[Device]:
        """Return all devices known to BlueZ.

        Parameters
        ----------
        adapter_path:
            Optional D-Bus object path of an adapter.  When given, only
            devices whose ``Adapter`` property matches are returned.
        """
        objects = await self.get_managed_objects()
        devices: list[Device] = []
        for path, interfaces in objects.items():
            props = interfaces.get(DEVICE_IFACE)
            if props is None:
                continue
            owning_adapter = props.get("Adapter", "")
            if adapter_path is not None and owning_adapter != adapter_path:
                continue
            address = props.get("Address", "")
            devices.append(
                Device(
                    path=path,
                    address=address,
                    name=props.get("Name") or props.get("Alias") or address,
                    adapter_path=owning_adapter,
                    paired=bool(props.get("Paired", False)),
                    trusted=bool(props.get("Trusted", False)),
                    connected=bool(props.get("Connected", False)),
                    rssi=props.get("RSSI"),
                )
            )
        return devices

    # ── Adapter control ───────────────────────────────────────────────────

    async def set_adapter_powered(self, adapter_path: str, powered: bool) -> None:
        """Set the ``Powered`` property on an adapter via D-Bus Properties.

        Raises
        ------
        DBusConnectionError
            :meth:`connect` has not been called yet.
        """
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, introspection)
        props_iface = proxy.get_interface(PROPERTIES_IFACE)
        await cast(Any, props_iface).call_set(
            ADAPTER_IFACE, "Powered", Variant("b", powered)
        )

    # ── Discovery ─────────────────────────────────────────────────────────

    async def start_discovery(self, adapter_path: str) -> bool:
        """Call ``org.bluez.Adapter1.StartDiscovery()`` on *adapter_path*.

        Sets a discovery filter of ``Transport="auto"`` to scan both
        Bluetooth Classic (BR/EDR) and BLE without requiring separate
        filter calls for each transport type.  ``DuplicateData`` is left
        at its BlueZ default (``false``) to avoid flooding the event loop
        with repeated BLE advertisements.

        BlueZ discovery sessions are reference-counted.  If another client
        already started discovery, ``StartDiscovery()`` returns
        ``org.bluez.Error.InProgress`` — this is handled gracefully: we
        mark the adapter as "in discovery" and continue listening for
        device events normally.

        Parameters
        ----------
        adapter_path:
            D-Bus object path of the adapter, e.g. ``/org/bluez/hci0``.

        Returns
        -------
        bool
            ``True`` if discovery was successfully started (or was already
            in progress), ``False`` if the adapter is not ready.

        Raises
        ------
        DBusConnectionError
            :meth:`connect` has not been called yet.
        """
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)

        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, adapter_path, introspection
            )
            adapter_iface = proxy.get_interface(ADAPTER_IFACE)

            # Set filter: Transport=auto covers both BR/EDR and BLE.
            # DuplicateData defaults to False which avoids repeated BLE
            # advertisement floods.
            discovery_filter = {"Transport": Variant("s", "auto")}
            await cast(Any, adapter_iface).call_set_discovery_filter(discovery_filter)

            await cast(Any, adapter_iface).call_start_discovery()
            self._discovery_active.add(adapter_path)
            logger.debug("discovery started on adapter %s", adapter_path)
            return True

        except Exception as exc:  # noqa: BLE001
            err = _bluez_error_name(exc)
            if err == _BLUEZ_IN_PROGRESS:
                # Another client is already scanning — piggyback on their
                # session.  We still add to _discovery_active so we call
                # StopDiscovery() at the right time.
                self._discovery_active.add(adapter_path)
                logger.debug(
                    "discovery already in progress on %s"
                    " — using existing session",
                    adapter_path,
                )
                return True
            if err == _BLUEZ_NOT_READY:
                logger.debug(
                    "adapter %s not ready for discovery: %s", adapter_path, exc
                )
                return False
            # Any other error: log and continue; don't crash the daemon.
            logger.warning(
                "start_discovery failed on %s: %s", adapter_path, exc
            )
            return False

    async def stop_discovery(self, adapter_path: str) -> None:
        """Call ``org.bluez.Adapter1.StopDiscovery()`` on *adapter_path*.

        Only releases *our* discovery session.  If other BlueZ clients are
        scanning, their sessions remain active.  Errors are logged and
        swallowed so that a failed ``StopDiscovery`` never prevents the
        daemon from continuing.

        Parameters
        ----------
        adapter_path:
            D-Bus object path of the adapter.

        Raises
        ------
        DBusConnectionError
            :meth:`connect` has not been called yet.
        """
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)

        self._discovery_active.discard(adapter_path)

        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, adapter_path, introspection
            )
            adapter_iface = proxy.get_interface(ADAPTER_IFACE)
            await cast(Any, adapter_iface).call_stop_discovery()
            logger.debug("discovery stopped on adapter %s", adapter_path)
        except Exception as exc:  # noqa: BLE001
            # StopDiscovery can fail if the adapter was powered off or
            # BlueZ was restarted.  This is not fatal.
            logger.debug(
                "stop_discovery on %s raised (ignored): %s", adapter_path, exc
            )

    # ── Device connection ─────────────────────────────────────────────────

    async def connect_device(self, device_path: str) -> None:
        """Call ``org.bluez.Device1.Connect()`` on the given D-Bus path.

        Returns as soon as the kernel accepts the attempt.  Full profile
        negotiation may not be complete; the authoritative signal is the
        ``PropertiesChanged(Connected=True)`` emitted later by BlueZ.

        Raises
        ------
        DBusConnectionError
            :meth:`connect` has not been called yet.
        """
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        introspection = await self._bus.introspect(BLUEZ_SERVICE, device_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, device_path, introspection)
        device_iface = proxy.get_interface(DEVICE_IFACE)
        await cast(Any, device_iface).call_connect()

    # ── Event subscription ────────────────────────────────────────────────

    async def subscribe(self, callback: EventCallback) -> None:
        """Wire up BlueZ D-Bus signals to *callback*.

        Three signal sources are wired:

        1. ``ObjectManager.InterfacesAdded``   → ``event_type="added"``
        2. ``ObjectManager.InterfacesRemoved`` → ``event_type="removed"``
        3. ``PropertiesChanged`` match rule    → ``event_type="properties_changed"``

        All three deliver::

            callback(event_type, object_path, interface_name, changed_props)

        The callback is async; it is dispatched via :func:`_schedule` so
        the synchronous D-Bus signal handler never blocks.

        Raises
        ------
        DBusConnectionError
            :meth:`connect` has not been called yet.
        """
        if self._bus is None or self._object_manager is None:
            raise DBusConnectionError(_NOT_CONNECTED)

        # ── InterfacesAdded ───────────────────────────────────────────────
        def _on_interfaces_added(path: str, interfaces: dict[str, Any]) -> None:
            unwrapped = _unwrap(interfaces)
            logger.debug(
                "InterfacesAdded: path=%s interfaces=%s",
                path,
                list(unwrapped.keys()),
            )
            for iface_name, props in unwrapped.items():
                _schedule(callback("added", path, iface_name, props))

        # ── InterfacesRemoved ─────────────────────────────────────────────
        def _on_interfaces_removed(path: str, interfaces: list[str]) -> None:
            logger.debug("InterfacesRemoved: path=%s interfaces=%s", path, interfaces)
            for iface_name in interfaces:
                _schedule(callback("removed", path, iface_name, {}))

        cast(Any, self._object_manager).on_interfaces_added(_on_interfaces_added)
        cast(Any, self._object_manager).on_interfaces_removed(_on_interfaces_removed)

        # ── PropertiesChanged ─────────────────────────────────────────────
        def _message_handler(message: Any) -> None:  # noqa: ANN401
            if (
                message.interface == PROPERTIES_IFACE
                and message.member == "PropertiesChanged"
                and message.path
                and message.path.startswith("/org/bluez")
            ):
                iface_name, changed, _invalidated = message.body
                unwrapped = _unwrap(changed)
                logger.debug(
                    "PropertiesChanged: path=%s iface=%s changed=%s",
                    message.path,
                    iface_name,
                    list(unwrapped.keys()),
                )
                _schedule(
                    callback(
                        "properties_changed",
                        message.path,
                        iface_name,
                        unwrapped,
                    )
                )

        self._bus.add_message_handler(_message_handler)

        rule = (
            "type='signal',sender='org.bluez',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged'"
        )
        dbus_iface = await self._get_dbus_daemon_interface()
        await cast(Any, dbus_iface).call_add_match(rule)
        logger.debug("Subscribed to BlueZ D-Bus signals.")

    # ── Internal ──────────────────────────────────────────────────────────

    async def _get_dbus_daemon_interface(self) -> ProxyInterface:
        """Return the ``org.freedesktop.DBus`` interface for ``AddMatch``."""
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        introspection = await self._bus.introspect(DBUS_SERVICE, DBUS_PATH)
        proxy = self._bus.get_proxy_object(DBUS_SERVICE, DBUS_PATH, introspection)
        return proxy.get_interface(DBUS_SERVICE)

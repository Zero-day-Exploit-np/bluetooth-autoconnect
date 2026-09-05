"""Linux backend — BlueZ via D-Bus (``dbus-next``).

This module is the direct evolution of the original ``dbus_client.py``.
The public class is ``LinuxBackend``; the ``dbus_client`` module re-exports
it as ``BlueZClient`` for backward compatibility with existing code and tests.

Only this file imports ``dbus_next``.  Every other module in the package
receives a ``BluetoothBackend``-typed reference and never imports D-Bus
types directly.
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

    Falls back silently when there is no running loop (test environments).
    """
    try:
        asyncio.get_event_loop().create_task(coro)
    except RuntimeError:
        logger.debug("_schedule: no running event loop — coroutine discarded.")


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
            self._object_manager = self._bluez_root.get_interface(
                OBJECT_MANAGER_IFACE
            )
        except Exception as exc:  # noqa: BLE001
            raise BlueZNotAvailableError(
                "org.bluez is not available on the system bus. "
                "Is bluetooth.service running and BlueZ installed?"
            ) from exc

    async def close(self) -> None:
        """Disconnect from the D-Bus system bus."""
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

    async def get_devices(
        self, adapter_path: str | None = None
    ) -> list[Device]:
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
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, adapter_path, introspection
        )
        props_iface = proxy.get_interface(PROPERTIES_IFACE)
        await cast(Any, props_iface).call_set(
            ADAPTER_IFACE, "Powered", Variant("b", powered)
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
        proxy = self._bus.get_proxy_object(
            BLUEZ_SERVICE, device_path, introspection
        )
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
        def _on_interfaces_added(
            path: str, interfaces: dict[str, Any]
        ) -> None:
            unwrapped = _unwrap(interfaces)
            logger.debug(
                "InterfacesAdded: path=%s interfaces=%s",
                path,
                list(unwrapped.keys()),
            )
            for iface_name, props in unwrapped.items():
                _schedule(callback("added", path, iface_name, props))

        # ── InterfacesRemoved ─────────────────────────────────────────────
        def _on_interfaces_removed(
            path: str, interfaces: list[str]
        ) -> None:
            logger.debug(
                "InterfacesRemoved: path=%s interfaces=%s", path, interfaces
            )
            for iface_name in interfaces:
                _schedule(callback("removed", path, iface_name, {}))

        cast(Any, self._object_manager).on_interfaces_added(_on_interfaces_added)
        cast(Any, self._object_manager).on_interfaces_removed(
            _on_interfaces_removed
        )

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

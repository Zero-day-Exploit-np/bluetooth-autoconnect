"""Async BlueZ D-Bus client.

Wraps the ``org.bluez`` D-Bus service using ``dbus-next`` (a pure-Python,
asyncio-native D-Bus library) so that bluetooth-autoconnect does not
depend on the ``dbus-python`` C extension, which is not reliably
pip-installable across distributions.

BlueZ D-Bus object hierarchy (simplified)::

    /org/bluez/hci0                          org.bluez.Adapter1
    /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF     org.bluez.Device1
    /org/bluez/hci1                          org.bluez.Adapter1
    ...

We discover this hierarchy via ``org.freedesktop.DBus.ObjectManager
.GetManagedObjects`` on the root ``org.bluez`` object, and watch for
``InterfacesAdded`` / ``InterfacesRemoved`` / ``PropertiesChanged``
signals to react to adapters/devices appearing, disappearing, or
changing state (e.g. a device coming into range and becoming
connectable).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus, ProxyObject
from dbus_next.errors import DBusError

from .exceptions import BlueZNotAvailableError, DBusConnectionError
from .models import Adapter, Device

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

logger = logging.getLogger("bluetooth_autoconnect.dbus_client")

# Signature: (event_type, object_path, interface, changed_properties)
EventCallback = Callable[[str, str, str, dict], Awaitable[None]]


def _unwrap(value: Any) -> Any:
    """Unwrap a dbus-next Variant into a plain Python value, recursively."""
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


class BlueZClient:
    """High-level async wrapper around the BlueZ D-Bus API."""

    def __init__(self) -> None:
        self._bus: Optional[MessageBus] = None
        self._bluez_root: Optional[ProxyObject] = None
        self._object_manager = None

    async def connect(self) -> None:
        """Connect to the D-Bus system bus and verify BlueZ is present.

        Raises:
            DBusConnectionError: If the system bus cannot be reached.
            BlueZNotAvailableError: If ``org.bluez`` is not registered
                on the bus (BlueZ not installed or not running).
        """
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:  # noqa: BLE001 - surface as our own type
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
        except DBusError as exc:
            raise BlueZNotAvailableError(
                "org.bluez is not available on the system bus. Is the "
                "bluetooth.service running and BlueZ installed?"
            ) from exc

    async def close(self) -> None:
        """Disconnect from the bus, if connected."""
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def get_managed_objects(self) -> dict:
        """Return the raw BlueZ object tree (path -> interface -> props)."""
        assert self._object_manager is not None, "call connect() first"
        objects = await self._object_manager.call_get_managed_objects()
        return _unwrap(objects)

    async def get_adapters(self) -> list[Adapter]:
        """Return all Bluetooth adapters currently known to BlueZ."""
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

    async def get_devices(self, adapter_path: Optional[str] = None) -> list[Device]:
        """Return known devices, optionally filtered to one adapter.

        Args:
            adapter_path: If given, only devices belonging to this
                adapter's D-Bus path are returned. Otherwise, devices
                from all adapters are returned.
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

    async def set_adapter_powered(self, adapter_path: str, powered: bool) -> None:
        """Power an adapter on or off."""
        assert self._bus is not None, "call connect() first"
        introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, introspection)
        props_iface = proxy.get_interface(PROPERTIES_IFACE)
        await props_iface.call_set(ADAPTER_IFACE, "Powered", Variant("b", powered))

    async def connect_device(self, device_path: str) -> None:
        """Ask BlueZ to connect a device by D-Bus object path.

        Raises:
            DBusError: Propagated as-is; callers (the connector module)
                are responsible for retry/backoff and translating this
                into a :class:`DeviceConnectionError` after retries are
                exhausted.
        """
        assert self._bus is not None, "call connect() first"
        introspection = await self._bus.introspect(BLUEZ_SERVICE, device_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, device_path, introspection)
        device_iface = proxy.get_interface(DEVICE_IFACE)
        await device_iface.call_connect()

    async def subscribe(self, callback: EventCallback) -> None:
        """Subscribe to adapter/device add/remove/property-change events.

        Args:
            callback: An async function called with
                ``(event_type, object_path, interface, changed_properties)``
                where ``event_type`` is one of ``"added"``, ``"removed"``,
                or ``"properties_changed"``.
        """
        assert self._bus is not None and self._object_manager is not None

        def _on_interfaces_added(path: str, interfaces: dict) -> None:
            unwrapped = _unwrap(interfaces)
            for iface_name, props in unwrapped.items():
                self._bus.loop.create_task(  # type: ignore[union-attr]
                    callback("added", path, iface_name, props)
                )

        def _on_interfaces_removed(path: str, interfaces: list) -> None:
            for iface_name in interfaces:
                self._bus.loop.create_task(  # type: ignore[union-attr]
                    callback("removed", path, iface_name, {})
                )

        self._object_manager.on_interfaces_added(_on_interfaces_added)
        self._object_manager.on_interfaces_removed(_on_interfaces_removed)

        # PropertiesChanged is emitted per-object, not centrally, so we
        # subscribe via a match rule on the Properties interface for any
        # object under org.bluez rather than opening a proxy per device.
        def _message_handler(message) -> None:  # type: ignore[no-untyped-def]
            if (
                message.interface == PROPERTIES_IFACE
                and message.member == "PropertiesChanged"
                and message.path
                and message.path.startswith("/org/bluez")
            ):
                iface_name, changed, _invalidated = message.body
                unwrapped = _unwrap(changed)
                self._bus.loop.create_task(  # type: ignore[union-attr]
                    callback("properties_changed", message.path, iface_name, unwrapped)
                )

        self._bus.add_message_handler(_message_handler)

        # Ask the bus daemon to route PropertiesChanged signals from
        # org.bluez to us; without this match rule, most bus
        # implementations won't forward the signal to our connection.
        rule = (
            "type='signal',sender='org.bluez',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged'"
        )
        dbus_iface = await self._get_dbus_daemon_interface()
        await dbus_iface.call_add_match(rule)

    async def _get_dbus_daemon_interface(self):  # type: ignore[no-untyped-def]
        """Return a proxy interface for the bus daemon's AddMatch method."""
        assert self._bus is not None
        introspection = await self._bus.introspect(
            "org.freedesktop.DBus", "/org/freedesktop/DBus"
        )
        proxy = self._bus.get_proxy_object(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", introspection
        )
        return proxy.get_interface("org.freedesktop.DBus")

"""Async BlueZ D-Bus client."""

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
EventCallback = Callable[[str, str, str, dict], Awaitable[None]]


def _unwrap(value: Any) -> Any:
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


class BlueZClient:
    def __init__(self) -> None:
        self._bus: Optional[MessageBus] = None
        self._bluez_root: Optional[ProxyObject] = None
        self._object_manager = None

    async def connect(self) -> None:
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:  # noqa: BLE001
            raise DBusConnectionError(f"Could not connect to the D-Bus system bus: {exc}") from exc

        try:
            introspection = await self._bus.introspect(BLUEZ_SERVICE, "/")
            self._bluez_root = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)
            self._object_manager = self._bluez_root.get_interface(OBJECT_MANAGER_IFACE)
        except Exception as exc:  # noqa: BLE001 - convert all BlueZ availability errors
            raise BlueZNotAvailableError(
                "org.bluez is not available on the system bus. Is the bluetooth.service running and BlueZ installed?"
            ) from exc

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def get_managed_objects(self) -> dict:
        assert self._object_manager is not None, "call connect() first"
        objects = await self._object_manager.call_get_managed_objects()
        return _unwrap(objects)

    async def get_adapters(self) -> list[Adapter]:
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
        assert self._bus is not None, "call connect() first"
        introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, introspection)
        props_iface = proxy.get_interface(PROPERTIES_IFACE)
        await props_iface.call_set(ADAPTER_IFACE, "Powered", Variant("b", powered))

    async def connect_device(self, device_path: str) -> None:
        assert self._bus is not None, "call connect() first"
        introspection = await self._bus.introspect(BLUEZ_SERVICE, device_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, device_path, introspection)
        device_iface = proxy.get_interface(DEVICE_IFACE)
        await device_iface.call_connect()

    async def subscribe(self, callback: EventCallback) -> None:
        assert self._bus is not None and self._object_manager is not None

        def _on_interfaces_added(path: str, interfaces: dict) -> None:
            unwrapped = _unwrap(interfaces)
            for iface_name, props in unwrapped.items():
                self._bus.loop.create_task(callback("added", path, iface_name, props))

        def _on_interfaces_removed(path: str, interfaces: list) -> None:
            for iface_name in interfaces:
                self._bus.loop.create_task(callback("removed", path, iface_name, {}))

        self._object_manager.on_interfaces_added(_on_interfaces_added)
        self._object_manager.on_interfaces_removed(_on_interfaces_removed)

        def _message_handler(message) -> None:
            if (
                message.interface == PROPERTIES_IFACE
                and message.member == "PropertiesChanged"
                and message.path
                and message.path.startswith("/org/bluez")
            ):
                iface_name, changed, _invalidated = message.body
                unwrapped = _unwrap(changed)
                self._bus.loop.create_task(callback("properties_changed", message.path, iface_name, unwrapped))

        self._bus.add_message_handler(_message_handler)

        rule = (
            "type='signal',sender='org.bluez',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged'"
        )
        dbus_iface = await self._get_dbus_daemon_interface()
        await dbus_iface.call_add_match(rule)

    async def _get_dbus_daemon_interface(self):
        assert self._bus is not None
        introspection = await self._bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        proxy = self._bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspection)
        return proxy.get_interface("org.freedesktop.DBus")

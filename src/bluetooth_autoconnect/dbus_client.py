"""Async BlueZ D-Bus client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus, ProxyInterface, ProxyObject

from .exceptions import BlueZNotAvailableError, DBusConnectionError
from .models import Adapter, Device

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
DBUS_SERVICE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

logger = logging.getLogger("bluetooth_autoconnect.dbus_client")
EventCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
_NOT_CONNECTED = "BlueZClient is not connected; call connect() first."


def _unwrap(value: Any) -> Any:  # noqa: ANN401
    """Recursively unwrap dbus_next Variant wrappers into plain Python types."""
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def _schedule(coro: Any) -> None:  # noqa: ANN401
    """Schedule a coroutine on the running event loop.

    dbus-next calls synchronous signal callbacks from inside its own message
    dispatch loop, which already runs on the asyncio event loop.  The correct
    way to hand off work to that loop from a sync callback is
    ``asyncio.get_event_loop().create_task()``.

    ``MessageBus.loop`` was removed in newer dbus-next / dbus-fast releases,
    so we never touch it.
    """
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop — happens only in unit tests that call sync helpers
        # outside an async context.  Log and discard.
        logger.debug("_schedule: no running event loop, coroutine discarded.")


class BlueZClient:
    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._bluez_root: ProxyObject | None = None
        # dbus-next returns a ProxyInterface; declared as such so mypy
        # accepts the assignment in connect().
        self._object_manager: ProxyInterface | None = None

    async def connect(self) -> None:
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
                "org.bluez is not available on the system bus."
                " Is the bluetooth.service running and BlueZ installed?"
            ) from exc

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def get_managed_objects(self) -> dict[str, Any]:
        if self._object_manager is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        # call_get_managed_objects() is generated at runtime by dbus-next's
        # introspection machinery and is not present in the ProxyInterface
        # stub.  cast(Any, ...) accurately models the opaque dynamic dispatch.
        objects = await cast(Any, self._object_manager).call_get_managed_objects()
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

    async def get_devices(self, adapter_path: str | None = None) -> list[Device]:
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
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, introspection)
        props_iface = proxy.get_interface(PROPERTIES_IFACE)
        # call_set is generated dynamically by dbus-next introspection.
        await cast(Any, props_iface).call_set(
            ADAPTER_IFACE, "Powered", Variant("b", powered)
        )

    async def connect_device(self, device_path: str) -> None:
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        introspection = await self._bus.introspect(BLUEZ_SERVICE, device_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, device_path, introspection)
        device_iface = proxy.get_interface(DEVICE_IFACE)
        # call_connect is generated dynamically by dbus-next introspection.
        await cast(Any, device_iface).call_connect()

    async def subscribe(self, callback: EventCallback) -> None:
        """Register callbacks for BlueZ object-manager and property signals.

        Three signal sources are wired up:
          1. InterfacesAdded   — new adapter or device object appeared.
          2. InterfacesRemoved — adapter or device object removed.
          3. PropertiesChanged — any property on a BlueZ object changed
             (e.g. Adapter.Powered, Device.Connected, Device.RSSI).

        All three dispatch to *callback* with the signature::

            callback(event_type, object_path, interface_name, changed_props)

        where *event_type* is one of ``"added"``, ``"removed"``, or
        ``"properties_changed"``.
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

        # on_interfaces_added / on_interfaces_removed are also dynamically
        # generated; cast to Any to avoid attr-defined noise.
        cast(Any, self._object_manager).on_interfaces_added(_on_interfaces_added)
        cast(Any, self._object_manager).on_interfaces_removed(_on_interfaces_removed)

        # ── PropertiesChanged (match rule on the system bus) ──────────────
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
        # call_add_match is dynamically generated.
        await cast(Any, dbus_iface).call_add_match(rule)
        logger.debug("Subscribed to BlueZ D-Bus signals.")

    async def _get_dbus_daemon_interface(self) -> ProxyInterface:
        if self._bus is None:
            raise DBusConnectionError(_NOT_CONNECTED)
        introspection = await self._bus.introspect(DBUS_SERVICE, DBUS_PATH)
        proxy = self._bus.get_proxy_object(DBUS_SERVICE, DBUS_PATH, introspection)
        return proxy.get_interface(DBUS_SERVICE)

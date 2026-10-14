"""Tests for the platform backend abstraction layer.

Coverage targets
----------------
backends/__init__.py   BluetoothBackend protocol structural checking
                       create_backend() — linux path, unsupported platform,
                                          unknown name, BackendNotAvailableError
                       get_platform_name() — linux, windows, darwin, other

backends/linux.py      LinuxBackend construction (no I/O)
                       get_managed_objects — not-connected guard
                       get_adapters, get_devices — mapping from raw objects
                       set_adapter_powered — not-connected guard
                       connect_device — not-connected guard
                       subscribe — not-connected guard
                       _get_dbus_daemon_interface — not-connected guard
                       _unwrap — Variant, dict, list, scalar
                       _schedule — running loop, no loop

daemon.py              AutoConnectDaemon accepts backend kwarg
                       backend kwarg is stored as self.client
                       AutoConnectDaemon() with no backend calls create_backend

exceptions.py          BackendError hierarchy
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bluetooth_autoconnect.backends import (
    BluetoothBackend,
    create_backend,
    get_platform_name,
)
from bluetooth_autoconnect.backends.linux import (
    ADAPTER_IFACE,
    DEVICE_IFACE,
    LinuxBackend,
    _schedule,
    _unwrap,
)
from bluetooth_autoconnect.exceptions import (
    BackendError,
    BackendNotAvailableError,
    BackendUnsupportedError,
    DBusConnectionError,
)
from bluetooth_autoconnect.models import Adapter

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class _FakeVariant:
    """Minimal stand-in for dbus_next.Variant."""

    def __init__(self, value: object) -> None:
        self.value = value


class _FakeObjectManager:
    def __init__(self, objects: dict) -> None:
        self._objects = objects

    async def call_get_managed_objects(self) -> dict:
        return self._objects

    def on_interfaces_added(self, cb: object) -> None:
        self._added_cb = cb

    def on_interfaces_removed(self, cb: object) -> None:
        self._removed_cb = cb


class _FakeProxyObj:
    def __init__(self, om: _FakeObjectManager) -> None:
        self._om = om

    def get_interface(self, name: str) -> object:
        if name == "org.freedesktop.DBus.ObjectManager":
            return self._om
        if name == "org.freedesktop.DBus":
            return SimpleNamespace(call_add_match=AsyncMock())
        return SimpleNamespace()


class _FakeBus:
    def __init__(self, proxy: _FakeProxyObj) -> None:
        self._proxy = proxy
        self.message_handlers: list = []

    async def connect(self) -> _FakeBus:
        return self

    async def introspect(self, service: str, path: str) -> dict:
        return {}

    def get_proxy_object(self, service: str, path: str, intro: object) -> _FakeProxyObj:
        return self._proxy

    def add_message_handler(self, handler: object) -> None:
        self.message_handlers.append(handler)

    def disconnect(self) -> None:
        pass


def _make_connected_backend(objects: dict | None = None) -> LinuxBackend:
    """Return a LinuxBackend whose connect() has already been called (mocked)."""
    om = _FakeObjectManager(objects or {})
    proxy = _FakeProxyObj(om)
    bus = _FakeBus(proxy)

    backend = LinuxBackend()
    backend._bus = bus  # type: ignore[assignment]
    backend._bluez_root = proxy  # type: ignore[assignment]
    backend._object_manager = om  # type: ignore[assignment]
    return backend


# ─────────────────────────────────────────────────────────────────────────────
# get_platform_name
# ─────────────────────────────────────────────────────────────────────────────


class TestGetPlatformName:
    def test_linux(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert get_platform_name() == "linux"

    def test_linux2(self) -> None:
        with patch.object(sys, "platform", "linux2"):
            assert get_platform_name() == "linux"

    def test_windows(self) -> None:
        with patch.object(sys, "platform", "win32"):
            assert get_platform_name() == "windows"

    def test_darwin(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert get_platform_name() == "darwin"

    def test_unknown(self) -> None:
        with patch.object(sys, "platform", "freebsd13"):
            assert get_platform_name() == "freebsd13"


# ─────────────────────────────────────────────────────────────────────────────
# create_backend
# ─────────────────────────────────────────────────────────────────────────────


class TestCreateBackend:
    def test_returns_linux_backend_on_linux(self) -> None:
        with patch.object(sys, "platform", "linux"):
            b = create_backend()
        assert isinstance(b, LinuxBackend)

    def test_explicit_linux_on_linux(self) -> None:
        with patch.object(sys, "platform", "linux"):
            b = create_backend(backend="linux")
        assert isinstance(b, LinuxBackend)

    def test_linux_backend_on_windows_raises(self) -> None:
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(BackendNotAvailableError, match="Linux"):
                create_backend(backend="linux")

    def test_windows_backend_on_linux_raises(self) -> None:
        with patch.object(sys, "platform", "linux"):
            with pytest.raises(BackendNotAvailableError, match="Windows"):
                create_backend(backend="windows")

    def test_darwin_raises_not_implemented(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            with pytest.raises(BackendNotAvailableError, match="macOS"):
                create_backend()

    def test_unknown_backend_name_raises(self) -> None:
        with pytest.raises(BackendUnsupportedError, match="haiku"):
            create_backend(backend="haiku")

    def test_backend_errors_are_backend_error_subclass(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            with pytest.raises(BackendError):
                create_backend()

    def test_case_insensitive_backend_name(self) -> None:
        with patch.object(sys, "platform", "linux"):
            b = create_backend(backend="Linux")
        assert isinstance(b, LinuxBackend)

    def test_windows_backend_missing_winrt_raises(self) -> None:
        with patch.object(sys, "platform", "win32"):
            with patch.dict(sys.modules, {"winrt": None}):
                with pytest.raises((BackendNotAvailableError, ImportError)):
                    create_backend(backend="windows")


# ─────────────────────────────────────────────────────────────────────────────
# BluetoothBackend protocol
# ─────────────────────────────────────────────────────────────────────────────


class TestBluetoothBackendProtocol:
    """LinuxBackend must satisfy the BluetoothBackend Protocol."""

    def test_linux_backend_satisfies_protocol(self) -> None:
        b = LinuxBackend()
        assert isinstance(b, BluetoothBackend)

    def test_fake_backend_satisfies_protocol(self) -> None:
        """Any object with the right async methods satisfies the protocol."""

        class _Fake:
            async def connect(self) -> None: ...
            async def close(self) -> None: ...
            async def get_adapters(self) -> list:
                return []

            async def get_devices(self, adapter_path=None) -> list:
                return []

            async def connect_device(self, device_path: str) -> None: ...
            async def subscribe(self, callback: object) -> None: ...

        assert isinstance(_Fake(), BluetoothBackend)

    def test_object_missing_method_fails_protocol(self) -> None:
        """An object missing connect() does not satisfy the protocol."""

        class _Incomplete:
            async def close(self) -> None: ...
            async def get_adapters(self) -> list:
                return []

            async def get_devices(self, adapter_path=None) -> list:
                return []

            async def connect_device(self, device_path: str) -> None: ...
            async def subscribe(self, callback: object) -> None: ...

        assert not isinstance(_Incomplete(), BluetoothBackend)


# ─────────────────────────────────────────────────────────────────────────────
# _unwrap
# ─────────────────────────────────────────────────────────────────────────────


class TestUnwrap:
    def test_scalar_passthrough(self) -> None:
        assert _unwrap(42) == 42
        assert _unwrap("hello") == "hello"
        assert _unwrap(None) is None

    def test_variant_unwrapped(self) -> None:
        with patch("bluetooth_autoconnect.backends.linux.Variant", _FakeVariant):
            result = _unwrap(_FakeVariant(99))
        assert result == 99

    def test_nested_variant_unwrapped(self) -> None:
        with patch("bluetooth_autoconnect.backends.linux.Variant", _FakeVariant):
            result = _unwrap(_FakeVariant(_FakeVariant(7)))
        assert result == 7

    def test_dict_recursed(self) -> None:
        with patch("bluetooth_autoconnect.backends.linux.Variant", _FakeVariant):
            result = _unwrap({"a": _FakeVariant(1), "b": 2})
        assert result == {"a": 1, "b": 2}

    def test_list_recursed(self) -> None:
        with patch("bluetooth_autoconnect.backends.linux.Variant", _FakeVariant):
            result = _unwrap([_FakeVariant(1), _FakeVariant(2)])
        assert result == [1, 2]

    def test_empty_dict(self) -> None:
        assert _unwrap({}) == {}

    def test_empty_list(self) -> None:
        assert _unwrap([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# _schedule
# ─────────────────────────────────────────────────────────────────────────────


class TestSchedule:
    def test_schedules_on_running_loop(self) -> None:
        ran: list[str] = []

        async def _coro() -> None:
            ran.append("ran")

        async def _runner() -> None:
            _schedule(_coro())
            await asyncio.sleep(0)

        asyncio.run(_runner())
        assert ran == ["ran"]

    def test_silent_when_no_loop(self) -> None:
        async def _coro() -> None:
            pass

        coro = _coro()
        try:
            with patch(
                "bluetooth_autoconnect.backends.linux.asyncio.get_event_loop"
            ) as m:
                m.side_effect = RuntimeError("no loop")
                _schedule(coro)  # must not raise
        finally:
            coro.close()


# ─────────────────────────────────────────────────────────────────────────────
# LinuxBackend — not-connected guards
# ─────────────────────────────────────────────────────────────────────────────


class TestLinuxBackendNotConnectedGuards:
    def test_get_managed_objects_raises(self) -> None:
        b = LinuxBackend()
        with pytest.raises(DBusConnectionError, match="call connect"):
            asyncio.run(b.get_managed_objects())

    def test_set_adapter_powered_raises(self) -> None:
        b = LinuxBackend()
        with pytest.raises(DBusConnectionError, match="call connect"):
            asyncio.run(b.set_adapter_powered("/org/bluez/hci0", True))

    def test_connect_device_raises(self) -> None:
        b = LinuxBackend()
        with pytest.raises(DBusConnectionError, match="call connect"):
            asyncio.run(b.connect_device("/org/bluez/hci0/dev_AA"))

    def test_subscribe_raises(self) -> None:
        b = LinuxBackend()

        async def noop(*a: object) -> None:
            pass

        with pytest.raises(DBusConnectionError, match="call connect"):
            asyncio.run(b.subscribe(noop))

    def test_get_dbus_daemon_interface_raises(self) -> None:
        b = LinuxBackend()
        with pytest.raises(DBusConnectionError, match="call connect"):
            asyncio.run(b._get_dbus_daemon_interface())


# ─────────────────────────────────────────────────────────────────────────────
# LinuxBackend — get_adapters / get_devices mapping
# ─────────────────────────────────────────────────────────────────────────────


class TestLinuxBackendEnumeration:
    _OBJECTS = {
        "/org/bluez/hci0": {
            ADAPTER_IFACE: {
                "Alias": "hci0",
                "Address": "AA:BB:CC:DD:EE:FF",
                "Powered": True,
            }
        },
        "/org/bluez/hci0/dev_11_22_33_44_55_66": {
            DEVICE_IFACE: {
                "Address": "11:22:33:44:55:66",
                "Name": "Headset",
                "Adapter": "/org/bluez/hci0",
                "Paired": True,
                "Trusted": True,
                "Connected": False,
                "RSSI": -60,
            }
        },
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
            DEVICE_IFACE: {
                "Address": "AA:BB:CC:DD:EE:FF",
                "Alias": "Mouse",  # no Name → fall back to Alias
                "Adapter": "/org/bluez/hci0",
                "Paired": True,
                "Trusted": True,
                "Connected": True,
            }
        },
    }

    def test_get_adapters_returns_correct_adapter(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        adapters = asyncio.run(b.get_adapters())
        assert len(adapters) == 1
        assert isinstance(adapters[0], Adapter)
        assert adapters[0].name == "hci0"
        assert adapters[0].address == "AA:BB:CC:DD:EE:FF"
        assert adapters[0].powered is True

    def test_get_devices_returns_all_devices(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        devices = asyncio.run(b.get_devices())
        assert len(devices) == 2
        macs = {d.address for d in devices}
        assert macs == {"11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"}

    def test_get_devices_name_fallback_to_alias(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        devices = asyncio.run(b.get_devices())
        mouse = next(d for d in devices if d.address == "AA:BB:CC:DD:EE:FF")
        assert mouse.name == "Mouse"

    def test_get_devices_name_fallback_to_address(self) -> None:
        objects = {
            "/org/bluez/hci0/dev_FF_EE_DD_CC_BB_AA": {
                DEVICE_IFACE: {
                    "Address": "FF:EE:DD:CC:BB:AA",
                    # No Name, no Alias
                    "Adapter": "/org/bluez/hci0",
                    "Paired": False,
                    "Trusted": False,
                    "Connected": False,
                }
            }
        }
        b = _make_connected_backend(objects)
        devices = asyncio.run(b.get_devices())
        assert devices[0].name == "FF:EE:DD:CC:BB:AA"

    def test_get_devices_filtered_by_adapter_path(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        devices = asyncio.run(b.get_devices(adapter_path="/org/bluez/hci0"))
        assert len(devices) == 2

    def test_get_devices_wrong_adapter_path_returns_empty(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        devices = asyncio.run(b.get_devices(adapter_path="/org/bluez/hci99"))
        assert devices == []

    def test_get_devices_rssi_mapped(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        devices = asyncio.run(b.get_devices())
        headset = next(d for d in devices if d.address == "11:22:33:44:55:66")
        assert headset.rssi == -60

    def test_get_adapters_empty_when_no_adapter_iface(self) -> None:
        b = _make_connected_backend({"/org/bluez/hci0": {"org.bluez.GattManager1": {}}})
        assert asyncio.run(b.get_adapters()) == []

    def test_device_is_autoconnect_eligible(self) -> None:
        b = _make_connected_backend(self._OBJECTS)
        devices = asyncio.run(b.get_devices())
        headset = next(d for d in devices if d.address == "11:22:33:44:55:66")
        assert headset.is_autoconnect_eligible is True


# ─────────────────────────────────────────────────────────────────────────────
# LinuxBackend — connect / close
# ─────────────────────────────────────────────────────────────────────────────


class TestLinuxBackendConnectClose:
    def test_close_when_not_connected_is_noop(self) -> None:
        b = LinuxBackend()
        asyncio.run(b.close())  # must not raise

    def test_close_disconnects_bus(self) -> None:
        b = _make_connected_backend()
        disconnected: list = []
        b._bus.disconnect = lambda: disconnected.append(True)  # type: ignore[union-attr]
        asyncio.run(b.close())
        assert disconnected == [True]
        assert b._bus is None

    def test_connect_raises_dbus_connection_error_on_bus_failure(self) -> None:
        b = LinuxBackend()

        class _BoomBus:
            async def connect(self) -> None:
                raise RuntimeError("no bus")

        with patch(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda **kw: _BoomBus(),
        ):
            with pytest.raises(DBusConnectionError, match="D-Bus system bus"):
                asyncio.run(b.connect())

    def test_connect_raises_bluez_not_available_on_introspect_failure(self) -> None:
        from bluetooth_autoconnect.exceptions import BlueZNotAvailableError

        b = LinuxBackend()

        class _NoBlueZBus:
            async def connect(self) -> _NoBlueZBus:
                return self

            async def introspect(self, service: str, path: str) -> None:
                raise RuntimeError("org.bluez not found")

            def get_proxy_object(self, *a: object, **kw: object) -> None:
                raise AssertionError("not reached")

            def disconnect(self) -> None:
                pass

        with patch(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda **kw: _NoBlueZBus(),
        ):
            with pytest.raises(BlueZNotAvailableError):
                asyncio.run(b.connect())


# ─────────────────────────────────────────────────────────────────────────────
# LinuxBackend — subscribe signal callbacks
# ─────────────────────────────────────────────────────────────────────────────


class TestLinuxBackendSubscribe:
    def test_interfaces_added_fires_callback(self) -> None:
        b = _make_connected_backend()
        seen: list = []

        async def cb(event: str, path: str, iface: str, props: dict) -> None:
            seen.append((event, iface))

        async def runner() -> None:
            await b.subscribe(cb)
            om = b._object_manager
            om._added_cb(  # type: ignore[union-attr]
                "/org/bluez/hci0",
                {ADAPTER_IFACE: {"Powered": True}},
            )
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert ("added", ADAPTER_IFACE) in seen

    def test_interfaces_removed_fires_callback(self) -> None:
        b = _make_connected_backend()
        seen: list = []

        async def cb(event: str, path: str, iface: str, props: dict) -> None:
            seen.append(event)

        async def runner() -> None:
            await b.subscribe(cb)
            om = b._object_manager
            om._removed_cb(  # type: ignore[union-attr]
                "/org/bluez/hci0/dev_AA",
                [DEVICE_IFACE],
            )
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert "removed" in seen

    def test_properties_changed_fires_callback(self) -> None:
        b = _make_connected_backend()
        seen: list = []

        async def cb(event: str, path: str, iface: str, props: dict) -> None:
            seen.append((event, props))

        async def runner() -> None:
            await b.subscribe(cb)
            msg = SimpleNamespace(
                interface="org.freedesktop.DBus.Properties",
                member="PropertiesChanged",
                path="/org/bluez/hci0/dev_AA",
                body=(DEVICE_IFACE, {"Connected": False}, []),
            )
            for handler in b._bus.message_handlers:
                handler(msg)
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert any(e[0] == "properties_changed" for e in seen)

    def test_non_bluez_message_ignored(self) -> None:
        b = _make_connected_backend()
        seen: list = []

        async def cb(event: str, path: str, iface: str, props: dict) -> None:
            seen.append(event)

        async def runner() -> None:
            await b.subscribe(cb)
            msg = SimpleNamespace(
                interface="org.freedesktop.DBus.Properties",
                member="PropertiesChanged",
                path="/org/something/else",
                body=(DEVICE_IFACE, {}, []),
            )
            for handler in b._bus.message_handlers:
                handler(msg)
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert seen == []


# ─────────────────────────────────────────────────────────────────────────────
# AutoConnectDaemon — backend injection
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoConnectDaemonBackendInjection:
    def test_injected_backend_stored_as_client(self) -> None:
        from bluetooth_autoconnect.daemon import AutoConnectDaemon

        class _FakeBackend:
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def get_adapters(self) -> list:
                return []

            async def get_devices(self, adapter_path=None) -> list:
                return []

            async def connect_device(self, path: str) -> None:
                pass

            async def subscribe(self, cb: object) -> None:
                pass

        fake = _FakeBackend()
        daemon = AutoConnectDaemon(backend=fake)
        assert daemon.client is fake

    def test_no_backend_auto_detects_on_linux(self) -> None:
        """On Linux, AutoConnectDaemon() without backend arg gets LinuxBackend."""
        from bluetooth_autoconnect.daemon import AutoConnectDaemon

        with patch.object(sys, "platform", "linux"):
            daemon = AutoConnectDaemon()
        assert isinstance(daemon.client, LinuxBackend)

    def test_explicit_backend_bypasses_create_backend(self) -> None:
        """Passing backend= never calls create_backend()."""
        from bluetooth_autoconnect.daemon import AutoConnectDaemon

        class _MockBackend:
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def get_adapters(self) -> list:
                return []

            async def get_devices(self, adapter_path=None) -> list:
                return []

            async def connect_device(self, path: str) -> None:
                pass

            async def subscribe(self, cb: object) -> None:
                pass

        mock = _MockBackend()
        with patch(
            "bluetooth_autoconnect.daemon.create_backend",
            side_effect=AssertionError("should not be called"),
        ):
            daemon = AutoConnectDaemon(backend=mock)

        assert daemon.client is mock


# ─────────────────────────────────────────────────────────────────────────────
# Exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────


class TestExceptionHierarchy:
    def test_backend_error_is_bluetooth_error(self) -> None:
        from bluetooth_autoconnect.exceptions import BluetoothAutoConnectError

        assert issubclass(BackendError, BluetoothAutoConnectError)

    def test_dbus_connection_error_is_backend_error(self) -> None:
        assert issubclass(DBusConnectionError, BackendError)

    def test_bluez_not_available_is_backend_error(self) -> None:
        from bluetooth_autoconnect.exceptions import BlueZNotAvailableError

        assert issubclass(BlueZNotAvailableError, BackendError)

    def test_backend_not_available_is_backend_error(self) -> None:
        assert issubclass(BackendNotAvailableError, BackendError)

    def test_backend_unsupported_is_backend_error(self) -> None:
        assert issubclass(BackendUnsupportedError, BackendError)

    def test_backend_not_available_message(self) -> None:
        exc = BackendNotAvailableError("requires Linux")
        assert "requires Linux" in str(exc)

    def test_backend_unsupported_message(self) -> None:
        exc = BackendUnsupportedError("unknown backend 'haiku'")
        assert "haiku" in str(exc)

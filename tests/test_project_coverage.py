from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from bluetooth_autoconnect.cli import main
from bluetooth_autoconnect.config import (
    AutoConnectConfig,
    DaemonConfig,
    LoggingConfig,
    RetryConfig,
)
from bluetooth_autoconnect.daemon import AutoConnectDaemon
from bluetooth_autoconnect.dbus_client import BlueZClient
from bluetooth_autoconnect.exceptions import BlueZNotAvailableError, DBusConnectionError
from bluetooth_autoconnect.logging_setup import configure_logging
from bluetooth_autoconnect.models import Adapter, Device


class FakeBus:
    def __init__(self) -> None:
        self.loop = SimpleNamespace(create_task=lambda coro: asyncio.create_task(coro))
        self.message_handlers: list = []
        self._introspection = {}

    async def connect(self):
        return self

    async def introspect(self, service: str, path: str):
        return {"service": service, "path": path}

    def get_proxy_object(self, service: str, path: str, introspection):
        return FakeProxyObject(service, path)

    def add_message_handler(self, handler):
        self.message_handlers.append(handler)

    def disconnect(self):
        return None


class FakeProxyObject:
    def __init__(self, service: str, path: str) -> None:
        self.service = service
        self.path = path
        self._interfaces = {}

    def get_interface(self, iface_name: str):
        if iface_name == "org.freedesktop.DBus.ObjectManager":
            return FakeObjectManager()
        if iface_name == "org.freedesktop.DBus":
            return FakeDBusInterface()
        if iface_name == "org.bluez.Device1":
            return FakeDeviceInterface()
        if iface_name == "org.freedesktop.DBus.Properties":
            return FakePropertiesInterface()
        return SimpleNamespace()


class FakeObjectManager:
    def __init__(self) -> None:
        self.objects = {
            "/org/bluez/hci0": {
                "org.bluez.Adapter1": {"Alias": "hci0", "Address": "AA:BB:CC:DD:EE:FF", "Powered": True},
                "org.bluez.Device1": {"Address": "11:22:33:44:55:66", "Name": "Headset", "Adapter": "/org/bluez/hci0", "Paired": True, "Trusted": True, "Connected": False},
            },
            "/org/bluez/hci0/dev_01": {
                "org.bluez.Device1": {"Address": "00:00:00:00:00:01", "Name": "Mouse", "Adapter": "/org/bluez/hci0", "Paired": True, "Trusted": True, "Connected": True},
            },
        }

    async def call_get_managed_objects(self):
        return self.objects

    def on_interfaces_added(self, callback):
        self._added = callback

    def on_interfaces_removed(self, callback):
        self._removed = callback


class FakeDBusInterface:
    async def call_add_match(self, _rule: str) -> None:
        return None


class FakePropertiesInterface:
    async def call_set(self, _iface: str, _prop: str, value):
        self.value = value
        return None


class FakeDeviceInterface:
    async def call_connect(self) -> None:
        self.connected = True


class FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.subscribed = False
        self.adapters = [Adapter(path="/org/bluez/hci0", name="hci0", address="AA:BB:CC:DD:EE:FF", powered=True)]
        self.devices = [Device(path="/org/bluez/hci0/dev_01", address="11:22:33:44:55:66", name="Headset", adapter_path="/org/bluez/hci0", paired=True, trusted=True, connected=False)]

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def subscribe(self, callback) -> None:
        self.subscribed = True

    async def get_adapters(self):
        return self.adapters

    async def get_devices(self, adapter_path: str | None = None):
        return self.devices

    async def connect_device(self, device_path: str) -> None:
        return None


def test_config_dataclasses_round_trip() -> None:
    cfg = AutoConnectConfig(
        retry=RetryConfig(max_attempts=7, base_delay=0.5),
        logging=LoggingConfig(level="DEBUG", structured=False),
        daemon=DaemonConfig(scan_interval=15, max_concurrency=3),
        adapter="hci1",
        device_priorities={"AA:BB:CC:DD:EE:FF": 300},
    )
    data = cfg.to_dict()
    assert data["retry"]["max_attempts"] == 7
    assert data["logging"]["level"] == "DEBUG"
    assert data["daemon"]["scan_interval"] == 15
    assert data["device_priorities"]["AA:BB:CC:DD:EE:FF"] == 300


def test_config_handles_dict_values() -> None:
    cfg = AutoConnectConfig(
        retry={"max_attempts": 9, "base_delay": 0.25},
        logging={"level": "WARNING", "structured": False},
        daemon={"scan_interval": 20, "max_concurrency": 8},
    )
    assert cfg.retry.max_attempts == 9
    assert cfg.logging.level == "WARNING"
    assert cfg.daemon.max_concurrency == 8


def test_daemon_run_once_handles_adapters_and_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = AutoConnectDaemon(max_concurrency=2)
    daemon.client = FakeClient()

    async def fake_connect_all(devices, connect_fn, policy=None, max_concurrency=5):
        return {devices[0].address: True}

    monkeypatch.setattr("bluetooth_autoconnect.daemon.connect_all", fake_connect_all)

    async def _runner() -> None:
        results = await daemon.run_once()
        assert results == {"11:22:33:44:55:66": True}

    asyncio.run(_runner())


def test_daemon_event_callbacks_set_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = AutoConnectDaemon()
    events = [
        ("properties_changed", "/org/bluez/hci0", "org.bluez.Adapter1", {"Powered": True}),
        ("added", "/org/bluez/hci0/dev_2", "org.bluez.Device1", {}),
        ("properties_changed", "/org/bluez/hci0/dev_2", "org.bluez.Device1", {"Connected": False}),
        ("properties_changed", "/org/bluez/hci0/dev_2", "org.bluez.Device1", {"RSSI": -30}),
    ]
    for event in events:
        asyncio.run(daemon._on_dbus_event(*event))
        assert daemon._rescan_event.is_set()
        daemon._rescan_event.clear()


def test_daemon_install_signal_handlers_records_registration() -> None:
    daemon = AutoConnectDaemon()
    seen: list[tuple[int, object]] = []

    class FakeLoop:
        def add_signal_handler(self, sig, func):
            seen.append((sig, func))

    daemon._install_signal_handlers(FakeLoop())
    assert len(seen) == 3


def test_daemon_run_forever_executes_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = AutoConnectDaemon()
    daemon.client = FakeClient()
    daemon._stop_event.set()

    async def fake_run_once():
        return {"11:22:33:44:55:66": True}

    monkeypatch.setattr(daemon, "_install_signal_handlers", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "run_once", fake_run_once)

    async def _runner() -> None:
        await daemon.run_forever()

    asyncio.run(_runner())
    assert daemon.client.closed is True


def test_logging_setup_uses_verbose_and_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_systemd = SimpleNamespace(JournalHandler=lambda **kwargs: SimpleNamespace(setLevel=lambda level: None))
    monkeypatch.setitem(__import__("sys").modules, "systemd", SimpleNamespace())
    monkeypatch.setitem(__import__("sys").modules, "systemd.journal", fake_systemd)

    logger = configure_logging(verbose=True)
    assert logger.level == logging.DEBUG
    assert logger.handlers

    logger2 = configure_logging(verbose=False)
    assert logger2.level == logging.INFO


def test_bluez_client_getters_and_connect_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bluetooth_autoconnect.dbus_client.MessageBus", lambda *args, **kwargs: FakeBus())
    client = BlueZClient()
    asyncio.run(client.connect())
    adapters = asyncio.run(client.get_adapters())
    assert adapters[0].name == "hci0"
    devices = asyncio.run(client.get_devices("/org/bluez/hci0"))
    assert devices[0].address == "11:22:33:44:55:66"

    async def set_adapter_powered():
        await client.set_adapter_powered("/org/bluez/hci0", True)

    asyncio.run(set_adapter_powered())

    async def connect_device():
        await client.connect_device("/org/bluez/hci0/dev_01")

    asyncio.run(connect_device())


def test_bluez_client_subscribe_runs_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bluetooth_autoconnect.dbus_client.MessageBus", lambda *args, **kwargs: FakeBus())
    client = BlueZClient()
    asyncio.run(client.connect())
    seen: list[tuple[str, str, str, dict]] = []

    async def cb(event_type, path, interface, changed):
        seen.append((event_type, path, interface, changed))

    async def _runner() -> None:
        await client.subscribe(cb)
        client._bus.message_handlers[0](
            SimpleNamespace(
                interface="org.freedesktop.DBus.Properties",
                member="PropertiesChanged",
                path="/org/bluez/hci0",
                body=("org.bluez.Adapter1", {"Powered": True}, []),
            )
        )

    asyncio.run(_runner())
    assert seen[0][0] == "properties_changed"


def test_cli_main_handles_unhappy_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def connect(self):
            return None

        async def close(self):
            return None

    class FakeDaemon:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        async def run_forever(self):
            return None

        async def run_once(self):
            return {"AA:BB": True}

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", FakeDaemon)
    assert main([]) == 0
    assert main(["--daemon"]) == 0

    class FailClient:
        async def connect(self):
            raise DBusConnectionError("fail")

        async def close(self):
            return None

    class FailDaemon:
        def __init__(self, *args, **kwargs):
            self.client = FailClient()

        async def run_once(self):
            return {}

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", FailDaemon)
    assert main([]) == 2

    class InterruptDaemon:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        async def run_once(self):
            raise KeyboardInterrupt

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", InterruptDaemon)
    assert main([]) == 130

    class MissingBlueZDaemon:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        async def run_once(self):
            raise BlueZNotAvailableError("missing")

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", MissingBlueZDaemon)
    assert main([]) == 2


def test_cli_main_handles_bluetooth_error() -> None:
    class FakeClient:
        async def connect(self):
            return None

        async def close(self):
            return None

    class FakeDaemon:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        async def run_once(self):
            return {"AA:BB": False}

    import bluetooth_autoconnect.cli as cli_module

    cli_module.AutoConnectDaemon = FakeDaemon
    assert main([]) == 1


def test_daemon_run_once_handles_empty_and_unpowered_states() -> None:
    daemon = AutoConnectDaemon()

    async def _empty_adapters():
        return []

    daemon.client = SimpleNamespace(
        get_adapters=_empty_adapters,
        get_devices=lambda adapter_path=None: [],
        connect_device=lambda device_path: None,
    )
    assert asyncio.run(daemon.run_once()) == {}

    async def _unpowered_adapters():
        return [Adapter(path="/org/bluez/hci0", name="hci0", address="AA", powered=False)]

    daemon.client = SimpleNamespace(
        get_adapters=_unpowered_adapters,
        get_devices=lambda adapter_path=None: [],
        connect_device=lambda device_path: None,
    )
    assert asyncio.run(daemon.run_once()) == {}


def test_dbus_client_connect_errors_are_translated() -> None:
    class BoomBus:
        async def connect(self):
            raise RuntimeError("boom")

    monkeypatcher = pytest.MonkeyPatch()
    monkeypatcher.setattr("bluetooth_autoconnect.dbus_client.MessageBus", lambda *args, **kwargs: BoomBus())
    try:
        client = BlueZClient()
        with pytest.raises(DBusConnectionError):
            asyncio.run(client.connect())
    finally:
        monkeypatcher.undo()

    class MissingBlueZBus:
        async def connect(self):
            return self

        async def introspect(self, service, path):
            raise RuntimeError("missing")

        def get_proxy_object(self, *args, **kwargs):
            raise AssertionError("not used")

        def disconnect(self) -> None:
            return None

    monkeypatcher = pytest.MonkeyPatch()
    monkeypatcher.setattr("bluetooth_autoconnect.dbus_client.MessageBus", lambda *args, **kwargs: MissingBlueZBus())
    try:
        client = BlueZClient()
        with pytest.raises(BlueZNotAvailableError):
            asyncio.run(client.connect())
    finally:
        monkeypatcher.undo()

"""Targeted tests to cover the remaining ~0.34 % gap and push total to ≥ 90 %.

Gaps addressed (by module and line):
  cli.py          50  – return 0 after daemon run_forever completes
                  65  – partial-failure branch: not all(results.values())
                  82-84 – KeyboardInterrupt handler in main()
                  88  – BluetoothAutoConnectError catch in main()
  daemon.py       74-75 – _shutdown() inner closure body
                  78-79 – _rescan() inner closure body
                  88-90 – run_forever fatal connect error path
                  101-116 – rescan-event loop branch (rescan fires, exception swallowed)
  dbus_client.py  27  – _unwrap: Variant branch
                  31  – _unwrap: list branch
                  57-59, 63 – get_managed_objects: not-connected guard
                  90, 93 – set_adapter_powered: not-connected guard
                  111  – connect_device: not-connected guard
                  119  – subscribe: not-connected guard
                  127, 130-132, 135-136 – subscribe: on_interfaces_removed callback
                  164  – _get_dbus_daemon_interface: not-connected guard
"""

from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace

import pytest

from bluetooth_autoconnect.cli import main
from bluetooth_autoconnect.daemon import AutoConnectDaemon
from bluetooth_autoconnect.dbus_client import BlueZClient
from bluetooth_autoconnect.exceptions import (
    BluetoothAutoConnectError,
    BlueZNotAvailableError,
    DBusConnectionError,
)

# ── _unwrap helpers ───────────────────────────────────────────────────────────

class FakeVariant:
    """Minimal stand-in for dbus_next.Variant so we can test the Variant branch."""
    def __init__(self, value):
        self.value = value


def test_unwrap_variant() -> None:
    # Patch isinstance check for Variant
    from unittest.mock import patch as _patch

    import bluetooth_autoconnect.dbus_client as dc
    with _patch("bluetooth_autoconnect.dbus_client.Variant", FakeVariant):
        assert dc._unwrap(FakeVariant(99)) == 99
        assert dc._unwrap(FakeVariant(FakeVariant(7))) == 7


def test_unwrap_list() -> None:
    from unittest.mock import patch as _patch

    import bluetooth_autoconnect.dbus_client as dc
    with _patch("bluetooth_autoconnect.dbus_client.Variant", FakeVariant):
        result = dc._unwrap([FakeVariant(1), FakeVariant(2)])
        assert result == [1, 2]


def test_unwrap_nested_dict() -> None:
    from unittest.mock import patch as _patch

    import bluetooth_autoconnect.dbus_client as dc
    with _patch("bluetooth_autoconnect.dbus_client.Variant", FakeVariant):
        result = dc._unwrap({"a": FakeVariant(10), "b": [FakeVariant(20)]})
        assert result == {"a": 10, "b": [20]}


# ── dbus_client: not-connected guards ────────────────────────────────────────

def test_get_managed_objects_raises_when_not_connected() -> None:
    client = BlueZClient()
    # _object_manager is None by default
    with pytest.raises(DBusConnectionError, match="call connect\\(\\) first"):
        asyncio.run(client.get_managed_objects())


def test_set_adapter_powered_raises_when_not_connected() -> None:
    client = BlueZClient()
    with pytest.raises(DBusConnectionError, match="call connect\\(\\) first"):
        asyncio.run(client.set_adapter_powered("/org/bluez/hci0", True))


def test_connect_device_raises_when_not_connected() -> None:
    client = BlueZClient()
    with pytest.raises(DBusConnectionError, match="call connect\\(\\) first"):
        asyncio.run(client.connect_device("/org/bluez/hci0/dev_AA"))


def test_subscribe_raises_when_not_connected() -> None:
    client = BlueZClient()
    async def noop(*a): pass
    with pytest.raises(DBusConnectionError, match="call connect\\(\\) first"):
        asyncio.run(client.subscribe(noop))


def test_get_dbus_daemon_interface_raises_when_not_connected() -> None:
    client = BlueZClient()
    with pytest.raises(DBusConnectionError, match="call connect\\(\\) first"):
        asyncio.run(client._get_dbus_daemon_interface())


# ── dbus_client: subscribe on_interfaces_removed callback ────────────────────

def test_subscribe_interfaces_removed_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover the on_interfaces_removed inner function in subscribe()."""

    class FakeDBusIface:
        async def call_add_match(self, rule): pass

    class FakeObjectManager:
        def __init__(self):
            self._added_cb = None
            self._removed_cb = None

        async def call_get_managed_objects(self):
            return {}

        def on_interfaces_added(self, cb):
            self._added_cb = cb

        def on_interfaces_removed(self, cb):
            self._removed_cb = cb

    class FakeProxyObj:
        def __init__(self):
            self._om = FakeObjectManager()

        def get_interface(self, name):
            if name == "org.freedesktop.DBus.ObjectManager":
                return self._om
            if name == "org.freedesktop.DBus":
                return FakeDBusIface()
            return SimpleNamespace()

    class FakeBus:
        def __init__(self):
            self._proxy = FakeProxyObj()
            self.loop = SimpleNamespace(
                create_task=lambda coro: asyncio.ensure_future(coro)
            )
            self.message_handlers = []

        async def connect(self): return self

        async def introspect(self, service, path):
            return {}

        def get_proxy_object(self, service, path, intro):
            return self._proxy

        def add_message_handler(self, h):
            self.message_handlers.append(h)

        def disconnect(self): pass

    seen: list = []

    async def cb(event_type, path, iface, changed):
        seen.append((event_type, iface))

    async def runner():
        monkeypatch.setattr(
            "bluetooth_autoconnect.dbus_client.MessageBus",
            lambda *a, **kw: FakeBus(),
        )
        client = BlueZClient()
        await client.connect()
        await client.subscribe(cb)

        # Fire the on_interfaces_removed callback
        om = client._bluez_root.get_interface("org.freedesktop.DBus.ObjectManager")
        om._removed_cb("/org/bluez/hci0/dev_01", ["org.bluez.Device1"])
        await asyncio.sleep(0)  # let create_task run

    asyncio.run(runner())
    assert any(e[0] == "removed" for e in seen)


# ── daemon: _shutdown and _rescan inner closures ──────────────────────────────

def test_signal_handler_shutdown_closure() -> None:
    daemon = AutoConnectDaemon()
    captured: list = []

    class FakeLoop:
        def add_signal_handler(self, sig, func):
            captured.append((sig, func))

    daemon._install_signal_handlers(FakeLoop())

    # SIGTERM handler → calls _shutdown()
    sigterm_handler = next(fn for sig, fn in captured if sig == signal.SIGTERM)
    sigterm_handler()
    assert daemon._stop_event.is_set()


def test_signal_handler_rescan_closure() -> None:
    daemon = AutoConnectDaemon()
    captured: list = []

    class FakeLoop:
        def add_signal_handler(self, sig, func):
            captured.append((sig, func))

    daemon._install_signal_handlers(FakeLoop())

    # SIGHUP handler → calls _rescan()
    sighup_handler = next(fn for sig, fn in captured if sig == signal.SIGHUP)
    sighup_handler()
    assert daemon._rescan_event.is_set()


# ── daemon: run_forever fatal connect error ───────────────────────────────────

def test_run_forever_propagates_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = AutoConnectDaemon()

    class FailClient:
        async def connect(self):
            raise DBusConnectionError("cannot connect")

        async def close(self): pass

    daemon.client = FailClient()

    with pytest.raises(DBusConnectionError):
        asyncio.run(daemon.run_forever())


def test_run_forever_propagates_bluez_error(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = AutoConnectDaemon()

    class FailClient:
        async def connect(self):
            raise BlueZNotAvailableError("no bluez")

        async def close(self): pass

    daemon.client = FailClient()

    with pytest.raises(BlueZNotAvailableError):
        asyncio.run(daemon.run_forever())


# ── daemon: rescan-event branch inside run_forever loop ──────────────────────

def test_run_forever_executes_rescan_and_exception_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover daemon.py lines 101-116: the rescan-event branch and the exception-swallow path.

    Strategy:
      - Initial run_once (call 1) sets the rescan event → loop picks it up.
      - Second run_once (call 2, inside rescan branch) raises → exception is swallowed.
      - After the exception the loop continues; we set _stop_event so it exits cleanly.
    We patch asyncio.sleep to a no-op (just `return`) so the 1 s debounce is instant,
    but we must NOT call asyncio.sleep inside the replacement (infinite recursion).
    """
    daemon = AutoConnectDaemon()
    call_count = 0

    class ControlledClient:
        async def connect(self): pass
        async def close(self): pass
        async def subscribe(self, cb): pass

    daemon.client = ControlledClient()

    # Capture the real sleep before patching so we can avoid calling the patched version.
    async def instant_sleep(_delay):
        return  # do not call asyncio.sleep here — that would recurse

    monkeypatch.setattr("bluetooth_autoconnect.daemon.asyncio.sleep", instant_sleep)
    monkeypatch.setattr(daemon, "_install_signal_handlers", lambda *a, **kw: None)

    async def fake_run_once():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Trigger one rescan event so the loop branch executes.
            daemon._rescan_event.set()
            return {}
        # call_count >= 2: raise once (exception-swallow path), then stop.
        daemon._stop_event.set()   # ensure we exit the while loop after this
        if call_count == 2:
            raise RuntimeError("transient rescan error")
        return {}

    monkeypatch.setattr(daemon, "run_once", fake_run_once)

    asyncio.run(daemon.run_forever())
    assert call_count >= 2


# ── cli.py: remaining branches ───────────────────────────────────────────────

def test_cli_daemon_mode_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover cli.py:50 — return 0 after run_forever() returns normally."""

    class QuickDaemon:
        def __init__(self, *a, **kw):
            self.client = SimpleNamespace(
                connect=lambda: asyncio.coroutine(lambda: None)(),
                close=lambda: asyncio.coroutine(lambda: None)(),
            )

        async def run_forever(self):
            return  # completes immediately

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", QuickDaemon)
    assert main(["--daemon"]) == 0


def test_cli_partial_failure_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover cli.py:65 — not all(results.values()) → return 1."""

    class PartialClient:
        async def connect(self): pass
        async def close(self): pass

    class PartialDaemon:
        def __init__(self, *a, **kw):
            self.client = PartialClient()

        async def run_once(self):
            return {"AA:BB": True, "CC:DD": False}  # one failure

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", PartialDaemon)
    assert main([]) == 1


def test_cli_keyboard_interrupt_returns_130(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover cli.py:82-84 — KeyboardInterrupt propagated out of asyncio.run()."""

    class InterruptClient:
        async def connect(self): raise KeyboardInterrupt
        async def close(self): pass

    class InterruptDaemon:
        def __init__(self, *a, **kw):
            self.client = InterruptClient()

        async def run_once(self):
            return {}

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", InterruptDaemon)
    assert main([]) == 130


def test_cli_generic_bluetooth_error_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover cli.py:88 — BluetoothAutoConnectError (base class, not DBus subclass)."""

    class GenericErrorClient:
        async def connect(self): raise BluetoothAutoConnectError("something unexpected")
        async def close(self): pass

    class GenericErrorDaemon:
        def __init__(self, *a, **kw):
            self.client = GenericErrorClient()

        async def run_once(self):
            return {}

    monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", GenericErrorDaemon)
    assert main([]) == 1

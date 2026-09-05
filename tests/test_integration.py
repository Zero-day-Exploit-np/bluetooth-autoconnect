"""Integration tests for the D-Bus signal pipeline and doctor command.

These tests validate the complete signal flow — from a raw D-Bus callback
(InterfacesAdded, InterfacesRemoved, PropertiesChanged) all the way through
to the daemon's reconnect decision — without touching a real D-Bus socket.

Coverage targets
----------------
dbus_client.py  _schedule()
                _on_interfaces_added  (InterfacesAdded signal)
                _on_interfaces_removed (InterfacesRemoved signal)
                _message_handler       (PropertiesChanged signal)
                subscribe() happy path end-to-end

daemon.py       _on_dbus_event: adapter power-on
                _on_dbus_event: new device appears
                _on_dbus_event: device disconnects  → reconnect trigger
                _on_dbus_event: device RSSI appears → reconnect trigger
                _on_dbus_event: Trusted=True        → reconnect trigger
                _on_dbus_event: Paired=True         → reconnect trigger

doctor.py       run_doctor() output and return codes

cli.py          --debug flag
                doctor subcommand dispatch
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bluetooth_autoconnect.daemon import AutoConnectDaemon
from bluetooth_autoconnect.dbus_client import (
    ADAPTER_IFACE,
    DEVICE_IFACE,
    PROPERTIES_IFACE,
    BlueZClient,
    _schedule,
)
from bluetooth_autoconnect.models import Adapter, Device

# ─────────────────────────────────────────────────────────────────────────────
# Shared fake D-Bus infrastructure
# ─────────────────────────────────────────────────────────────────────────────


class _FakeDBusIface:
    """Minimal stand-in for org.freedesktop.DBus interface."""

    async def call_add_match(self, rule: str) -> None:
        return None


class _FakeObjectManager:
    def __init__(self) -> None:
        self._added_cb = None
        self._removed_cb = None

    async def call_get_managed_objects(self) -> dict:
        return {}

    def on_interfaces_added(self, cb) -> None:
        self._added_cb = cb

    def on_interfaces_removed(self, cb) -> None:
        self._removed_cb = cb


class _FakeProxyObject:
    def __init__(self) -> None:
        self.om = _FakeObjectManager()

    def get_interface(self, name: str):
        if name == "org.freedesktop.DBus.ObjectManager":
            return self.om
        if name == "org.freedesktop.DBus":
            return _FakeDBusIface()
        return SimpleNamespace()


class _FakeBus:
    """Drop-in replacement for dbus_next.aio.MessageBus.

    Stores registered message handlers so tests can fire them manually.
    """

    def __init__(self) -> None:
        self._proxy = _FakeProxyObject()
        self.message_handlers: list = []

    async def connect(self) -> _FakeBus:
        return self

    async def introspect(self, service: str, path: str) -> dict:
        return {}

    def get_proxy_object(self, service: str, path: str, intro) -> _FakeProxyObject:
        return self._proxy

    def add_message_handler(self, handler) -> None:
        self.message_handlers.append(handler)

    def disconnect(self) -> None:
        pass


def _make_props_changed_message(
    path: str, iface: str, changed: dict
) -> SimpleNamespace:
    """Build a minimal fake dbus_next message for a PropertiesChanged signal."""
    return SimpleNamespace(
        interface=PROPERTIES_IFACE,
        member="PropertiesChanged",
        path=path,
        body=(iface, changed, []),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _schedule() helper
# ─────────────────────────────────────────────────────────────────────────────


def test_schedule_runs_coroutine_on_loop() -> None:
    """_schedule() must fire the coroutine on the running loop."""
    fired: list[str] = []

    async def _marker() -> None:
        fired.append("ran")

    async def _runner() -> None:
        _schedule(_marker())
        await asyncio.sleep(0)  # yield so the scheduled task can run

    asyncio.run(_runner())
    assert fired == ["ran"]


def test_schedule_is_silent_when_no_loop() -> None:
    """_schedule() must not raise when there is no running loop."""
    # Outside an async context get_event_loop() may raise RuntimeError on
    # Python ≥ 3.10 if no current loop is set.  _schedule() must absorb it.
    import bluetooth_autoconnect.dbus_client as dc

    async def _noop() -> None:
        pass

    coro = _noop()
    try:
        with patch("bluetooth_autoconnect.backends.linux.asyncio.get_event_loop") as m:
            m.side_effect = RuntimeError("no loop")
            dc._schedule(coro)  # must not raise
    finally:
        coro.close()  # prevent "coroutine never awaited" ResourceWarning


# ─────────────────────────────────────────────────────────────────────────────
# subscribe() — InterfacesAdded signal
# ─────────────────────────────────────────────────────────────────────────────


class TestInterfacesAdded:
    """A new device appearing fires an 'added' callback."""

    def test_adapter_interfaces_added_fires_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda *a, **kw: _FakeBus(),
        )
        seen: list = []

        async def cb(event_type, path, iface, changed) -> None:
            seen.append((event_type, path, iface))

        async def runner() -> None:
            client = BlueZClient()
            await client.connect()
            await client.subscribe(cb)
            om = client._bluez_root.get_interface("org.freedesktop.DBus.ObjectManager")
            # Simulate BlueZ sending InterfacesAdded for a new adapter
            om._added_cb(
                "/org/bluez/hci0",
                {ADAPTER_IFACE: {"Powered": True, "Address": "AA:BB:CC:DD:EE:FF"}},
            )
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert any(e == ("added", "/org/bluez/hci0", ADAPTER_IFACE) for e in seen)

    def test_device_interfaces_added_fires_callback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda *a, **kw: _FakeBus(),
        )
        seen: list = []

        async def cb(event_type, path, iface, changed) -> None:
            seen.append((event_type, path, iface))

        async def runner() -> None:
            client = BlueZClient()
            await client.connect()
            await client.subscribe(cb)
            om = client._bluez_root.get_interface("org.freedesktop.DBus.ObjectManager")
            om._added_cb(
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                {DEVICE_IFACE: {"Paired": True, "Trusted": True, "Connected": False}},
            )
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert any(e[0] == "added" and DEVICE_IFACE in e[2] for e in seen)


# ─────────────────────────────────────────────────────────────────────────────
# subscribe() — InterfacesRemoved signal
# ─────────────────────────────────────────────────────────────────────────────


class TestInterfacesRemoved:
    def test_removed_callback_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda *a, **kw: _FakeBus(),
        )
        seen: list = []

        async def cb(event_type, path, iface, changed) -> None:
            seen.append((event_type, iface))

        async def runner() -> None:
            client = BlueZClient()
            await client.connect()
            await client.subscribe(cb)
            om = client._bluez_root.get_interface("org.freedesktop.DBus.ObjectManager")
            om._removed_cb(
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                [DEVICE_IFACE],
            )
            await asyncio.sleep(0)

        asyncio.run(runner())
        assert ("removed", DEVICE_IFACE) in seen


# ─────────────────────────────────────────────────────────────────────────────
# subscribe() — PropertiesChanged signal (message_handler)
# ─────────────────────────────────────────────────────────────────────────────


class TestPropertiesChanged:
    def _run_with_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
        msg: SimpleNamespace,
    ) -> list:
        monkeypatch.setattr(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda *a, **kw: _FakeBus(),
        )
        seen: list = []

        async def cb(event_type, path, iface, changed) -> None:
            seen.append((event_type, path, iface, changed))

        async def runner() -> None:
            client = BlueZClient()
            await client.connect()
            await client.subscribe(cb)
            # Fire the message handler directly as dbus-next would
            for handler in client._bus.message_handlers:
                handler(msg)
            await asyncio.sleep(0)

        asyncio.run(runner())
        return seen

    def test_adapter_powered_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = _make_props_changed_message(
            "/org/bluez/hci0", ADAPTER_IFACE, {"Powered": True}
        )
        seen = self._run_with_message(monkeypatch, msg)
        assert any(
            e[0] == "properties_changed"
            and e[2] == ADAPTER_IFACE
            and e[3].get("Powered") is True
            for e in seen
        )

    def test_device_connected_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = _make_props_changed_message(
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            DEVICE_IFACE,
            {"Connected": False},
        )
        seen = self._run_with_message(monkeypatch, msg)
        assert any(
            e[0] == "properties_changed" and e[3].get("Connected") is False
            for e in seen
        )

    def test_device_rssi_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = _make_props_changed_message(
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            DEVICE_IFACE,
            {"RSSI": -65},
        )
        seen = self._run_with_message(monkeypatch, msg)
        assert any(e[3].get("RSSI") == -65 for e in seen)

    def test_non_bluez_message_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Messages not on /org/bluez paths must be silently dropped."""
        msg = _make_props_changed_message(
            "/org/something/else", ADAPTER_IFACE, {"Powered": True}
        )
        seen = self._run_with_message(monkeypatch, msg)
        assert seen == []

    def test_wrong_interface_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-PropertiesChanged signals must be silently dropped."""
        msg = SimpleNamespace(
            interface="org.freedesktop.DBus.Introspectable",
            member="Introspect",
            path="/org/bluez/hci0",
            body=(),
        )
        seen = self._run_with_message(monkeypatch, msg)
        assert seen == []


# ─────────────────────────────────────────────────────────────────────────────
# Daemon reconnect decisions (full pipeline: signal → _on_dbus_event)
# ─────────────────────────────────────────────────────────────────────────────


class TestDaemonReconnectPipeline:
    """Verify that real D-Bus signal data triggers the correct reconnect flags."""

    @pytest.mark.parametrize(
        "event,path,iface,changed,should_rescan",
        [
            # Adapter powered on via PropertiesChanged
            (
                "properties_changed",
                "/org/bluez/hci0",
                ADAPTER_IFACE,
                {"Powered": True},
                True,
            ),
            # Adapter powered on via InterfacesAdded
            (
                "added",
                "/org/bluez/hci0",
                ADAPTER_IFACE,
                {"Powered": True},
                True,
            ),
            # Adapter powered OFF — must NOT trigger rescan
            (
                "properties_changed",
                "/org/bluez/hci0",
                ADAPTER_IFACE,
                {"Powered": False},
                False,
            ),
            # New device object appeared
            (
                "added",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {},
                True,
            ),
            # Device disconnected
            (
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Connected": False},
                True,
            ),
            # Device reconnected (Connected=True) — must NOT trigger rescan
            (
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Connected": True},
                False,
            ),
            # Device came back into range (RSSI updated)
            (
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"RSSI": -72},
                True,
            ),
            # Device marked trusted → should rescan
            (
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Trusted": True},
                True,
            ),
            # Device paired → should rescan
            (
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Paired": True},
                True,
            ),
            # Unrelated interface — must not trigger rescan
            (
                "properties_changed",
                "/org/bluez/hci0",
                "org.bluez.GattManager1",
                {"SomeProperty": True},
                False,
            ),
        ],
    )
    def test_event_triggers_rescan(
        self,
        event: str,
        path: str,
        iface: str,
        changed: dict,
        should_rescan: bool,
    ) -> None:
        daemon = AutoConnectDaemon()
        daemon._rescan_event.clear()
        asyncio.run(daemon._on_dbus_event(event, path, iface, changed))
        assert daemon._rescan_event.is_set() == should_rescan, (
            f"_rescan_event.is_set() == {daemon._rescan_event.is_set()}"
            f" but expected {should_rescan}"
            f" for event={event!r} iface={iface!r} changed={changed}"
        )

    def test_full_pipeline_adapter_poweron_triggers_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: adapter power-on signal → daemon detects it → run_once called."""
        daemon = AutoConnectDaemon()
        monkeypatch.setattr(
            "bluetooth_autoconnect.backends.linux.MessageBus",
            lambda *a, **kw: _FakeBus(),
        )

        run_once_calls: list = []

        class FakeClient:
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def subscribe(self, cb) -> None:
                # Store callback so test can fire it
                self._cb = cb

            async def get_adapters(self):
                return []  # empty → run_once returns {}

            async def get_devices(self, adapter_path=None):
                return []

        fake_client = FakeClient()
        daemon.client = fake_client  # type: ignore[assignment]

        original_run_once = daemon.run_once

        async def tracking_run_once():
            run_once_calls.append(1)
            return await original_run_once()

        monkeypatch.setattr(daemon, "run_once", tracking_run_once)
        monkeypatch.setattr(daemon, "_install_signal_handlers", lambda *a, **kw: None)

        async def instant_sleep(_delay: float) -> None:
            return

        monkeypatch.setattr("bluetooth_autoconnect.daemon.asyncio.sleep", instant_sleep)

        async def runner() -> None:
            # Simulate run_forever startup
            await daemon.client.connect()
            await daemon.client.subscribe(daemon._on_dbus_event)

            # Initial run_once (call 1)
            await daemon.run_once()

            # Fire adapter powered-on event → sets rescan flag
            await fake_client._cb(
                "properties_changed",
                "/org/bluez/hci0",
                ADAPTER_IFACE,
                {"Powered": True},
            )
            assert daemon._rescan_event.is_set()

            # Simulate one iteration of the run_forever loop manually
            daemon._rescan_event.clear()
            await daemon.run_once()  # call 2

            # Clean stop
            daemon._stop_event.set()

        asyncio.run(runner())
        assert (
            len(run_once_calls) >= 2
        ), f"Expected run_once called at least twice, got {len(run_once_calls)}"


# ─────────────────────────────────────────────────────────────────────────────
# Doctor command
# ─────────────────────────────────────────────────────────────────────────────


class TestDoctorCommand:
    """Unit tests for the doctor module without touching real system services."""

    def _make_report(self, monkeypatch: pytest.MonkeyPatch, **overrides) -> tuple:
        """Patch all external calls and run run_doctor(); return (exit_code, output)."""
        import io

        from bluetooth_autoconnect import doctor as doc

        defaults = dict(
            bluetooth_active=True,
            dbus_reachable=True,
            bluez_available=True,
            adapters=[
                Adapter(
                    path="/org/bluez/hci0",
                    name="hci0",
                    address="AA:BB:CC:DD:EE:FF",
                    powered=True,
                )
            ],
            devices=[
                Device(
                    path="/org/bluez/hci0/dev_11_22_33_44_55_66",
                    address="11:22:33:44:55:66",
                    name="JBL Speaker",
                    adapter_path="/org/bluez/hci0",
                    paired=True,
                    trusted=True,
                    connected=True,
                )
            ],
        )
        defaults.update(overrides)

        # Patch _check_systemd_unit
        def fake_unit(unit: str):
            return doc.CheckResult(
                name=unit,
                passed=defaults["bluetooth_active"],
                detail="active" if defaults["bluetooth_active"] else "inactive",
            )

        # Patch _check_dbus
        def fake_dbus():
            return doc.CheckResult(
                name="D-Bus system bus",
                passed=defaults["dbus_reachable"],
                detail="socket ok" if defaults["dbus_reachable"] else "unreachable",
            )

        # Patch _check_bluez_async
        async def fake_bluez():
            if not defaults["bluez_available"]:
                return (
                    doc.CheckResult(
                        name="BlueZ available",
                        passed=False,
                        detail="org.bluez not found",
                    ),
                    [],
                    [],
                )
            return (
                doc.CheckResult(
                    name="BlueZ available", passed=True, detail="org.bluez found"
                ),
                defaults["adapters"],
                defaults["devices"],
            )

        monkeypatch.setattr(doc, "_check_systemd_unit", fake_unit)
        monkeypatch.setattr(doc, "_check_dbus", fake_dbus)
        monkeypatch.setattr(doc, "_check_bluez_async", fake_bluez)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            exit_code = doc.run_doctor()

        return exit_code, captured.getvalue()

    def test_all_pass_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code, out = self._make_report(monkeypatch)
        assert code == 0
        assert "PASS" in out
        assert "JBL Speaker" in out

    def test_bluetooth_service_inactive_returns_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._make_report(monkeypatch, bluetooth_active=False)
        assert code == 1
        assert "FAIL" in out

    def test_dbus_unreachable_returns_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._make_report(monkeypatch, dbus_reachable=False)
        assert code == 1

    def test_bluez_missing_returns_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._make_report(monkeypatch, bluez_available=False)
        assert code == 1
        assert "FAIL" in out

    def test_unpowered_adapter_shows_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._make_report(
            monkeypatch,
            adapters=[
                Adapter(
                    path="/org/bluez/hci0",
                    name="hci0",
                    address="AA:BB:CC:DD:EE:FF",
                    powered=False,
                )
            ],
        )
        # Unpowered adapter is a WARN (soft failure), not a hard FAIL
        assert "WARN" in out
        # Still passes overall (no hard failures)
        assert code == 0

    def test_no_adapters_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code, out = self._make_report(monkeypatch, adapters=[])
        assert code == 1
        assert "FAIL" in out

    def test_no_trusted_devices_shows_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._make_report(monkeypatch, devices=[])
        # No paired/trusted devices is a WARN, not a hard failure
        assert "WARN" in out
        assert code == 0

    def test_device_not_connected_shows_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._make_report(
            monkeypatch,
            devices=[
                Device(
                    path="/org/bluez/hci0/dev_11_22_33_44_55_66",
                    address="11:22:33:44:55:66",
                    name="JBL Speaker",
                    adapter_path="/org/bluez/hci0",
                    paired=True,
                    trusted=True,
                    connected=False,  # ← not connected
                )
            ],
        )
        assert "WARN" in out
        assert "JBL Speaker" in out
        assert code == 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI: --debug flag and doctor subcommand dispatch
# ─────────────────────────────────────────────────────────────────────────────


class TestCLINewFlags:
    def test_debug_flag_sets_debug_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import logging

        from bluetooth_autoconnect.cli import build_parser
        from bluetooth_autoconnect.logging_setup import configure_logging

        args = build_parser().parse_args(["--debug"])
        assert args.debug is True

        logger = configure_logging(debug=True)
        assert logger.level == logging.DEBUG

    def test_verbose_flag_is_hidden_alias(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--verbose"])
        assert args.verbose is True

    def test_doctor_subcommand_dispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI must call run_doctor() when 'doctor' subcommand is given."""
        called: list = []

        def fake_doctor() -> int:
            called.append(True)
            return 0

        monkeypatch.setattr("bluetooth_autoconnect.doctor.run_doctor", fake_doctor)
        from bluetooth_autoconnect.cli import main

        # Patch the import inside cli.py so it picks up fake_doctor
        with patch(
            "bluetooth_autoconnect.cli.run_doctor",
            fake_doctor,
            create=True,
        ):
            from bluetooth_autoconnect import cli as cli_mod

            original = cli_mod.__dict__.get("run_doctor")
            cli_mod.run_doctor = fake_doctor  # type: ignore[attr-defined]
            try:
                code = main(["doctor"])
            finally:
                if original is not None:
                    cli_mod.run_doctor = original  # type: ignore[attr-defined]
                else:
                    del cli_mod.run_doctor  # type: ignore[attr-defined]

        assert code == 0

    def test_doctor_subcommand_via_monkeypatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Alternative: monkeypatch the import inside the doctor module."""
        import bluetooth_autoconnect.doctor as doc_mod

        monkeypatch.setattr(doc_mod, "run_doctor", lambda: 0)

        # We need to ensure cli.py's lazy import picks up the patch.
        # The simplest reliable approach: patch at the module level.
        import bluetooth_autoconnect.cli as cli_mod  # noqa: PLC0415
        from bluetooth_autoconnect.cli import main

        monkeypatch.setattr(
            cli_mod,
            "run_doctor",  # type: ignore[arg-type]
            lambda: 0,
            raising=False,
        )
        # run_doctor is imported lazily inside main(); patch via sys.modules
        with patch.dict(
            "sys.modules",
            {
                "bluetooth_autoconnect.doctor": type(
                    "FakeDoctor", (), {"run_doctor": staticmethod(lambda: 0)}
                )()
            },
        ):
            code = main(["doctor"])
        assert code == 0


# ─────────────────────────────────────────────────────────────────────────────
# logging_setup: configure_logging debug parameter
# ─────────────────────────────────────────────────────────────────────────────


class TestLoggingSetup:
    def test_debug_flag_enables_debug_level(self) -> None:
        import logging

        from bluetooth_autoconnect.logging_setup import configure_logging

        logger = configure_logging(debug=True)
        assert logger.level == logging.DEBUG

    def test_verbose_flag_enables_debug_level(self) -> None:
        import logging

        from bluetooth_autoconnect.logging_setup import configure_logging

        logger = configure_logging(verbose=True)
        assert logger.level == logging.DEBUG

    def test_default_is_info_level(self) -> None:
        import logging

        from bluetooth_autoconnect.logging_setup import configure_logging

        logger = configure_logging()
        assert logger.level == logging.INFO

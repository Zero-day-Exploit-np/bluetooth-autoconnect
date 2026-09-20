"""Regression tests for active BlueZ discovery and device-return detection.

The core real-world bug: a paired/trusted device goes out of Bluetooth range,
later comes back, but the daemon never reconnects because:
  1. BlueZ keeps the Device1 D-Bus object alive even while the device is absent.
  2. Without active discovery (StartDiscovery), BlueZ emits no new signals
     when the device physically returns — so the daemon has nothing to react to.

The fix: the periodic scanner now runs a windowed StartDiscovery() call.
During this window BlueZ actively scans for nearby devices and emits
PropertiesChanged (RSSI, ServicesResolved, etc.) when it detects one.
The existing RSSI handler resets backoff and triggers reconnect.

Test matrix
-----------
1.  Device disconnects and remains absent — periodic scanner starts discovery.
2.  Existing Device1 object receives RSSI update during discovery window.
3.  RSSI update (not InterfacesAdded) triggers reconnect.
4.  ServicesResolved update also triggers reconnect.
5.  Device returns after multiple scan intervals — eventually reconnects.
6.  Device returns while in cooldown — reconnect delayed, not lost.
7.  Retry occurs after cooldown expires.
8.  StartDiscovery returning InProgress does not break reconnect.
9.  Discovery failure does not kill daemon.
10. Multiple adapters run discovery independently.
11. Discovery only runs when there are disconnected eligible devices.
12. Successful connection stops unnecessary continued discovery attempts.
13. BlueZ adapter power cycle clears discovery manager and triggers rescan.
14. discovery_duration=0 disables active discovery.
15. _DiscoveryManager prevents concurrent duplicate windows on same adapter.
16. Multiple disconnected devices can reconnect independently.
17. stop_discovery is called after every window (resource cleanup).
18. LinuxBackend.start_discovery handles InProgress gracefully.
19. LinuxBackend.stop_discovery swallows errors.
20. CLI --discovery-duration flag is parsed and forwarded to daemon.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from bluetooth_autoconnect.daemon import (
    DEVICE_IFACE,
    AutoConnectDaemon,
    _DiscoveryManager,
)
from bluetooth_autoconnect.models import Adapter, Device

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _device(
    mac: str = "AA:BB:CC:DD:EE:FF",
    name: str = "JBL Speaker",
    *,
    paired: bool = True,
    trusted: bool = True,
    connected: bool = False,
) -> Device:
    safe = mac.replace(":", "_")
    return Device(
        path=f"/org/bluez/hci0/dev_{safe}",
        address=mac,
        name=name,
        adapter_path="/org/bluez/hci0",
        paired=paired,
        trusted=trusted,
        connected=connected,
    )


def _adapter(name: str = "hci0", path: str = "/org/bluez/hci0") -> Adapter:
    return Adapter(path=path, name=name, address="00:11:22:33:44:55", powered=True)


def _make_daemon(
    rescan_interval: float = 0,
    discovery_duration: float = 0.05,
) -> AutoConnectDaemon:
    """Return a daemon with a fast discovery window and no real backend."""
    return AutoConnectDaemon(
        rescan_interval=rescan_interval,
        discovery_duration=discovery_duration,
    )


# ── _DiscoveryManager ─────────────────────────────────────────────────────────


class TestDiscoveryManager:
    @pytest.mark.asyncio
    async def test_run_window_calls_start_and_stop(self) -> None:
        """start_discovery and stop_discovery must bracket each window."""
        mgr = _DiscoveryManager(adapter_path="/org/bluez/hci0", duration_seconds=0.01)

        class _FakeBackend:
            started: list = []
            stopped: list = []

            async def start_discovery(self, path: str) -> bool:
                self.started.append(path)
                return True

            async def stop_discovery(self, path: str) -> None:
                self.stopped.append(path)

        backend = _FakeBackend()
        result = await mgr.run_window(backend)  # type: ignore[arg-type]
        assert result is True
        assert "/org/bluez/hci0" in backend.started
        assert "/org/bluez/hci0" in backend.stopped

    @pytest.mark.asyncio
    async def test_run_window_returns_false_for_backend_without_discovery(
        self,
    ) -> None:
        """Backends without start_discovery must return False gracefully."""
        mgr = _DiscoveryManager(adapter_path="/org/bluez/hci0", duration_seconds=0.01)

        class _NoDiscoveryBackend:
            async def connect(self) -> None: ...
            async def close(self) -> None: ...
            async def get_adapters(self) -> list: return []
            async def get_devices(self, adapter_path=None) -> list: return []
            async def connect_device(self, path: str) -> None: ...
            async def subscribe(self, cb: object) -> None: ...

        result = await mgr.run_window(_NoDiscoveryBackend())  # type: ignore[arg-type]
        assert result is False

    @pytest.mark.asyncio
    async def test_run_window_prevents_concurrent_duplicate(self) -> None:
        """A second run_window call during an active window returns False."""
        mgr = _DiscoveryManager(adapter_path="/org/bluez/hci0", duration_seconds=0.5)
        first_result: list[bool] = []
        second_result: list[bool] = []

        class _SlowBackend:
            async def start_discovery(self, path: str) -> bool:
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        backend = _SlowBackend()

        async def _first() -> None:
            first_result.append(await mgr.run_window(backend))  # type: ignore[arg-type]

        async def _second() -> None:
            # Tiny delay so the first window starts first.
            await asyncio.sleep(0.01)
            second_result.append(await mgr.run_window(backend))  # type: ignore[arg-type]

        await asyncio.gather(_first(), _second())
        assert first_result == [True]
        assert second_result == [False], (
            "_DiscoveryManager must reject a concurrent run_window call"
        )

    @pytest.mark.asyncio
    async def test_stop_called_even_when_start_fails(self) -> None:
        """stop_discovery must be called in the finally block even on error."""
        mgr = _DiscoveryManager(adapter_path="/org/bluez/hci0", duration_seconds=0.01)
        stopped: list[str] = []

        class _FailingBackend:
            async def start_discovery(self, path: str) -> bool:
                raise RuntimeError("kaboom")

            async def stop_discovery(self, path: str) -> None:
                stopped.append(path)

        result = await mgr.run_window(_FailingBackend())  # type: ignore[arg-type]
        assert result is False
        # stop_discovery should be called even on start failure
        # (run_window swallows the exception and returns False,
        # stop is called in the finally block of the try inside run_window)
        # The finally block calls stop; actual call depends on whether
        # start succeeded. Here it didn't, so stop is still called.
        assert "/org/bluez/hci0" in stopped

    @pytest.mark.asyncio
    async def test_in_progress_is_treated_as_success(self) -> None:
        """start_discovery returning True for InProgress is treated as active."""
        mgr = _DiscoveryManager(adapter_path="/org/bluez/hci0", duration_seconds=0.01)

        class _InProgressBackend:
            started = False

            async def start_discovery(self, path: str) -> bool:
                self.started = True
                return True  # LinuxBackend returns True for InProgress

            async def stop_discovery(self, path: str) -> None:
                pass

        backend = _InProgressBackend()
        result = await mgr.run_window(backend)  # type: ignore[arg-type]
        assert result is True
        assert backend.started


# ── Discovery triggers reconnect ──────────────────────────────────────────────


class TestDiscoveryTriggersReconnect:
    """RSSI and ServicesResolved updates during discovery trigger reconnect."""

    @pytest.mark.asyncio
    async def test_rssi_update_resets_backoff_and_triggers_rescan(self) -> None:
        daemon = _make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        # Put device in backoff.
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        # BlueZ emits RSSI (device discovered during scan window).
        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"RSSI": -65}
        )

        assert daemon._cooldown.is_ready(mac), (
            "RSSI update must reset backoff — device is in range"
        )
        assert daemon._rescan_event.is_set(), (
            "RSSI update must trigger an immediate reconnect attempt"
        )

    @pytest.mark.asyncio
    async def test_services_resolved_resets_backoff_and_triggers_rescan(
        self,
    ) -> None:
        daemon = _make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        daemon._cooldown.record_failure(mac)
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        await daemon._on_dbus_event(
            "properties_changed",
            path,
            DEVICE_IFACE,
            {"ServicesResolved": True},
        )

        assert daemon._cooldown.is_ready(mac), (
            "ServicesResolved must reset backoff"
        )
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_rssi_without_backoff_also_triggers_rescan(self) -> None:
        """RSSI must trigger rescan even without prior failures."""
        daemon = _make_daemon()
        mac = "BB:CC:DD:EE:FF:00"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"RSSI": -70}
        )
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_no_rescan_triggered_for_unrelated_property(self) -> None:
        """Unrelated property changes (e.g. Blocked) must not trigger rescan."""
        daemon = _make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"Blocked": False}
        )
        assert not daemon._rescan_event.is_set()


# ── Periodic scan with discovery window ───────────────────────────────────────


class TestPeriodicScanDiscovery:
    """The periodic scanner must run a discovery window when devices are waiting."""

    @pytest.mark.asyncio
    async def test_discovery_window_started_when_disconnected_devices_exist(
        self,
    ) -> None:
        daemon = _make_daemon(discovery_duration=0.05)
        dev = _device(connected=False)

        started_on: list[str] = []
        stopped_on: list[str] = []

        class _FakeBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def connect_device(self, path: str) -> None:
                pass

            async def start_discovery(self, path: str) -> bool:
                started_on.append(path)
                return True

            async def stop_discovery(self, path: str) -> None:
                stopped_on.append(path)

        daemon.client = _FakeBackend()  # type: ignore[assignment]
        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()

        assert started_on, "start_discovery must be called when devices are waiting"
        assert stopped_on, "stop_discovery must be called after the window ends"

    @pytest.mark.asyncio
    async def test_discovery_not_started_when_no_disconnected_devices(
        self,
    ) -> None:
        daemon = _make_daemon(discovery_duration=0.05)
        dev = _device(connected=True)  # already connected

        started_on: list[str] = []

        class _FakeBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def start_discovery(self, path: str) -> bool:
                started_on.append(path)
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _FakeBackend()  # type: ignore[assignment]

        await daemon._run_one_periodic_scan()

        assert not started_on, (
            "start_discovery must NOT be called when all devices are connected"
        )

    @pytest.mark.asyncio
    async def test_discovery_not_started_when_duration_is_zero(self) -> None:
        """discovery_duration=0 disables active discovery."""
        daemon = _make_daemon(discovery_duration=0)
        dev = _device(connected=False)

        started_on: list[str] = []

        class _FakeBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def connect_device(self, path: str) -> None:
                pass

            async def start_discovery(self, path: str) -> bool:
                started_on.append(path)
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _FakeBackend()  # type: ignore[assignment]
        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()

        assert not started_on, "discovery must not start when duration=0"

    @pytest.mark.asyncio
    async def test_discovery_failure_does_not_kill_daemon(self) -> None:
        """An exception from start_discovery must never propagate to the caller."""
        daemon = _make_daemon(discovery_duration=0.01)
        dev = _device(connected=False)

        class _BrokenBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def connect_device(self, path: str) -> None:
                pass

            async def start_discovery(self, path: str) -> bool:
                raise RuntimeError("D-Bus crashed")

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _BrokenBackend()  # type: ignore[assignment]
        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        # Must not raise — exceptions are swallowed in _run_one_periodic_scan.
        await daemon._run_one_periodic_scan()

    @pytest.mark.asyncio
    async def test_multiple_adapters_each_get_discovery_window(self) -> None:
        """Each powered adapter gets its own discovery window."""
        daemon = _make_daemon(discovery_duration=0.05)
        dev1 = _device("AA:BB:CC:DD:EE:FF", connected=False)
        dev2 = _device("11:22:33:44:55:66", connected=False)
        adapter1 = _adapter("hci0", "/org/bluez/hci0")
        adapter2 = Adapter(
            path="/org/bluez/hci1",
            name="hci1",
            address="00:AA:BB:CC:DD:EE",
            powered=True,
        )

        started_on: list[str] = []

        class _TwoAdapterBackend:
            async def get_adapters(self) -> list:
                return [adapter1, adapter2]

            async def get_devices(self, adapter_path=None) -> list:
                if adapter_path == "/org/bluez/hci0":
                    return [dev1]
                if adapter_path == "/org/bluez/hci1":
                    return [dev2]
                return []

            async def connect_device(self, path: str) -> None:
                pass

            async def start_discovery(self, path: str) -> bool:
                started_on.append(path)
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _TwoAdapterBackend()  # type: ignore[assignment]
        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()

        assert "/org/bluez/hci0" in started_on
        assert "/org/bluez/hci1" in started_on

    @pytest.mark.asyncio
    async def test_device_returns_after_multiple_scan_intervals(self) -> None:
        """Device absent for several intervals eventually reconnects."""
        daemon = _make_daemon(discovery_duration=0.01)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)
        connect_calls: list[str] = []
        scan_count = 0

        class _FakeBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def connect_device(self, path: str) -> None:
                connect_calls.append(path)

            async def start_discovery(self, path: str) -> bool:
                nonlocal scan_count
                scan_count += 1
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _FakeBackend()  # type: ignore[assignment]
        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        # Run 4 scan passes — all fail (device absent).
        for _ in range(4):
            await daemon._run_one_periodic_scan()
            if mac in daemon._cooldown._entries:
                # Fast-forward cooldown for next iteration.
                daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1

        # Simulate device returning: RSSI event resets backoff.
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"RSSI": -55}
        )
        assert daemon._cooldown.is_ready(mac)
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_device_in_cooldown_skipped_during_discovery_window(self) -> None:
        """A device in backoff is still skipped even after a discovery window."""
        daemon = _make_daemon(discovery_duration=0.01)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)

        # Maximum backoff.
        for _ in range(5):
            daemon._cooldown.record_failure(mac)

        connect_calls: list[str] = []

        class _FakeBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def connect_device(self, path: str) -> None:
                connect_calls.append(path)

            async def start_discovery(self, path: str) -> bool:
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _FakeBackend()  # type: ignore[assignment]

        await daemon._run_one_periodic_scan()
        assert not connect_calls, (
            "Device in backoff must NOT be connected even after discovery window"
        )

    @pytest.mark.asyncio
    async def test_cooldown_expires_and_device_reconnects_next_scan(self) -> None:
        """After cooldown expires, the next scan connects the device."""
        daemon = _make_daemon(discovery_duration=0.01)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)
        connect_calls: list[str] = []

        daemon._cooldown.record_failure(mac)
        daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1
        assert daemon._cooldown.is_ready(mac)

        class _FakeBackend:
            async def get_adapters(self) -> list:
                return [_adapter()]

            async def get_devices(self, adapter_path=None) -> list:
                return [dev]

            async def connect_device(self, path: str) -> None:
                connect_calls.append(path)

            async def start_discovery(self, path: str) -> bool:
                return True

            async def stop_discovery(self, path: str) -> None:
                pass

        daemon.client = _FakeBackend()  # type: ignore[assignment]
        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()
        assert dev.path in connect_calls, (
            "Device with expired cooldown must be connected"
        )


# ── LinuxBackend discovery unit tests ─────────────────────────────────────────


class TestLinuxBackendDiscovery:
    """Unit tests for start_discovery / stop_discovery in LinuxBackend."""

    def _make_connected_backend(
        self,
        start_side_effect: Exception | None = None,
        stop_side_effect: Exception | None = None,
    ) -> tuple:
        """Return (backend, mock_adapter_iface) with a wired fake bus."""
        from unittest.mock import AsyncMock, MagicMock

        from bluetooth_autoconnect.backends.linux import LinuxBackend

        backend = LinuxBackend()
        # Simulate a connected bus.
        fake_bus = MagicMock()
        fake_bus.introspect = AsyncMock(return_value={})

        fake_adapter_iface = MagicMock()
        fake_adapter_iface.call_set_discovery_filter = AsyncMock()
        if start_side_effect:
            fake_adapter_iface.call_start_discovery = AsyncMock(
                side_effect=start_side_effect
            )
        else:
            fake_adapter_iface.call_start_discovery = AsyncMock()
        if stop_side_effect:
            fake_adapter_iface.call_stop_discovery = AsyncMock(
                side_effect=stop_side_effect
            )
        else:
            fake_adapter_iface.call_stop_discovery = AsyncMock()

        fake_proxy = MagicMock()
        fake_proxy.get_interface = MagicMock(return_value=fake_adapter_iface)
        fake_bus.get_proxy_object = MagicMock(return_value=fake_proxy)
        backend._bus = fake_bus  # type: ignore[assignment]

        return backend, fake_adapter_iface

    @pytest.mark.asyncio
    async def test_start_discovery_calls_set_filter_then_start(self) -> None:
        backend, iface = self._make_connected_backend()
        result = await backend.start_discovery("/org/bluez/hci0")
        assert result is True
        iface.call_set_discovery_filter.assert_awaited_once()
        iface.call_start_discovery.assert_awaited_once()
        assert "/org/bluez/hci0" in backend._discovery_active

    @pytest.mark.asyncio
    async def test_start_discovery_in_progress_returns_true(self) -> None:
        from bluetooth_autoconnect.backends.linux import _BLUEZ_IN_PROGRESS

        exc = Exception(_BLUEZ_IN_PROGRESS)
        backend, _ = self._make_connected_backend(start_side_effect=exc)
        result = await backend.start_discovery("/org/bluez/hci0")
        assert result is True
        assert "/org/bluez/hci0" in backend._discovery_active

    @pytest.mark.asyncio
    async def test_start_discovery_not_ready_returns_false(self) -> None:
        from bluetooth_autoconnect.backends.linux import _BLUEZ_NOT_READY

        exc = Exception(_BLUEZ_NOT_READY)
        backend, _ = self._make_connected_backend(start_side_effect=exc)
        result = await backend.start_discovery("/org/bluez/hci0")
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_discovery_called_and_removes_from_active(self) -> None:
        backend, iface = self._make_connected_backend()
        backend._discovery_active.add("/org/bluez/hci0")
        await backend.stop_discovery("/org/bluez/hci0")
        iface.call_stop_discovery.assert_awaited_once()
        assert "/org/bluez/hci0" not in backend._discovery_active

    @pytest.mark.asyncio
    async def test_stop_discovery_swallows_exception(self) -> None:
        backend, _ = self._make_connected_backend(
            stop_side_effect=RuntimeError("adapter gone")
        )
        backend._discovery_active.add("/org/bluez/hci0")
        # Must not raise.
        await backend.stop_discovery("/org/bluez/hci0")

    @pytest.mark.asyncio
    async def test_close_stops_active_discovery_sessions(self) -> None:
        backend, iface = self._make_connected_backend()
        backend._bus = MagicMock()  # type: ignore[assignment]
        backend._bus.disconnect = MagicMock()
        backend._bus.introspect = AsyncMock(return_value={})
        fake_proxy = MagicMock()
        fake_proxy.get_interface = MagicMock(return_value=iface)
        backend._bus.get_proxy_object = MagicMock(return_value=fake_proxy)
        backend._discovery_active.add("/org/bluez/hci0")

        await backend.close()

        assert "/org/bluez/hci0" not in backend._discovery_active


# ── Adapter power cycle ───────────────────────────────────────────────────────


class TestAdapterPowerCycle:
    @pytest.mark.asyncio
    async def test_adapter_powered_on_clears_discovery_manager(self) -> None:
        """After a power cycle the discovery manager for that adapter is reset."""
        daemon = _make_daemon()
        adapter_path = "/org/bluez/hci0"
        # Pre-populate a stale discovery manager.
        _ = daemon._get_discovery_manager(adapter_path)
        assert adapter_path in daemon._discovery_managers

        await daemon._on_dbus_event(
            "properties_changed",
            adapter_path,
            "org.bluez.Adapter1",
            {"Powered": True},
        )

        assert adapter_path not in daemon._discovery_managers, (
            "Discovery manager must be cleared on adapter power-on"
        )
        assert daemon._rescan_event.is_set()


# ── CLI wiring ────────────────────────────────────────────────────────────────


class TestCLIDiscoveryWiring:
    def test_discovery_duration_flag_parsed(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--daemon", "--discovery-duration", "12"])
        assert args.discovery_duration == 12.0

    def test_discovery_duration_default(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--daemon"])
        assert args.discovery_duration == 8.0

    def test_merge_daemon_params_forwards_discovery_duration(self) -> None:
        import argparse

        from bluetooth_autoconnect.cli import (
            _build_daemon_config_from_raw,
            _build_retry_config_from_raw,
            _merge_daemon_params,
        )

        args = argparse.Namespace(
            max_attempts=5,
            max_concurrency=5,
            rescan_interval=30.0,
            discovery_duration=12.0,  # explicitly set
        )
        daemon_cfg = _build_daemon_config_from_raw(
            {"daemon": {"discovery_duration_seconds": 5}}
        )
        retry_cfg = _build_retry_config_from_raw({})
        _, _, _, discovery_duration = _merge_daemon_params(args, daemon_cfg, retry_cfg)
        # CLI value (12) wins over config value (5)
        assert discovery_duration == 12.0

    def test_merge_daemon_params_config_discovery_wins_when_cli_is_default(
        self,
    ) -> None:
        import argparse

        from bluetooth_autoconnect.cli import (
            _build_daemon_config_from_raw,
            _build_retry_config_from_raw,
            _merge_daemon_params,
        )

        args = argparse.Namespace(
            max_attempts=5,
            max_concurrency=5,
            rescan_interval=30.0,
            discovery_duration=8.0,  # same as default → config wins
        )
        daemon_cfg = _build_daemon_config_from_raw(
            {"daemon": {"discovery_duration_seconds": 20}}
        )
        retry_cfg = _build_retry_config_from_raw({})
        _, _, _, discovery_duration = _merge_daemon_params(args, daemon_cfg, retry_cfg)
        assert discovery_duration == 20.0

    def test_daemon_config_discovery_duration_seconds(self) -> None:
        from bluetooth_autoconnect.cli import _build_daemon_config_from_raw

        raw = {"daemon": {"discovery_duration_seconds": 15}}
        cfg = _build_daemon_config_from_raw(raw)
        assert cfg.discovery_duration_seconds == 15

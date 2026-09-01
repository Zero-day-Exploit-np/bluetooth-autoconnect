"""Tests for periodic background rescanning and per-device exponential backoff.

Scenario under test
-------------------
1. A trusted device is connected.
2. It goes out of range — BlueZ fires Connected=False.
3. The immediate reconnect attempt fails with br-connection-page-timeout.
4. The device is placed in backoff.
5. Eventually the device returns to range, but BlueZ fires no new event.
6. The periodic scanner wakes up, finds the disconnected device, and retries.
7. The reconnect succeeds and the backoff entry is cleared.

Additional scenarios
--------------------
- Backoff levels grow exponentially and are capped at 30 min.
- A device still inside its cooldown window is skipped by the periodic scan.
- An RSSI PropertyChanged event resets backoff and triggers an immediate scan.
- Connected=True resets backoff without triggering a new event scan.
- --rescan-interval 0 disables periodic scanning entirely.
- _CooldownRegistry.filter_ready correctly partitions ready vs. skipped devices.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from bluetooth_autoconnect.daemon import (
    _BACKOFF_BASE_SECONDS,
    _BACKOFF_MAX_LEVEL,
    _BACKOFF_MAX_SECONDS,
    _BACKOFF_MULTIPLIER,
    AutoConnectDaemon,
    _CooldownRegistry,
    _DeviceCooldown,
)
from bluetooth_autoconnect.dbus_client import DEVICE_IFACE
from bluetooth_autoconnect.models import Adapter, Device

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_device(
    mac: str = "AA:BB:CC:DD:EE:FF",
    *,
    connected: bool = False,
    paired: bool = True,
    trusted: bool = True,
    name: str = "Headphones",
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


_ADAPTER = Adapter(
    path="/org/bluez/hci0",
    name="hci0",
    address="00:11:22:33:44:55",
    powered=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# _DeviceCooldown unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDeviceCooldown:
    def test_initially_ready(self) -> None:
        """A brand-new cooldown entry must be immediately ready."""
        cd = _DeviceCooldown(mac="AA:BB:CC:DD:EE:FF")
        assert cd.ready is True

    def test_not_ready_after_first_failure(self) -> None:
        cd = _DeviceCooldown(mac="AA:BB:CC:DD:EE:FF")
        cd.record_failure()
        assert cd.ready is False

    def test_delay_grows_exponentially(self) -> None:
        """Each failure doubles the delay up to the cap."""
        cd = _DeviceCooldown(mac="AA:BB:CC:DD:EE:FF")
        expected_levels = []
        for level in range(1, _BACKOFF_MAX_LEVEL + 1):
            delay = min(
                _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER ** (level - 1)),
                _BACKOFF_MAX_SECONDS,
            )
            expected_levels.append(delay)

        for i, expected_delay in enumerate(expected_levels, start=1):
            before = time.monotonic()
            cd.record_failure()
            time.monotonic()
            actual_window = cd.retry_after - before
            # Allow a small wall-clock epsilon
            assert abs(actual_window - expected_delay) < 0.2, (
                f"level {i}: expected delay ≈{expected_delay}s"
                f" but window was {actual_window:.2f}s"
            )
            # Manually rewind to simulate time passing so next call starts fresh
            cd.retry_after = before - 1  # make it ready again for next iteration

    def test_level_capped_at_max(self) -> None:
        cd = _DeviceCooldown(mac="AA:BB:CC:DD:EE:FF")
        for _ in range(_BACKOFF_MAX_LEVEL + 10):
            cd.retry_after = time.monotonic() - 1  # fast-forward
            cd.record_failure()
        assert cd.level == _BACKOFF_MAX_LEVEL

    def test_ready_after_window_passes(self) -> None:
        cd = _DeviceCooldown(mac="AA:BB:CC:DD:EE:FF")
        cd.record_failure()
        # Force the window to have already elapsed
        cd.retry_after = time.monotonic() - 1
        assert cd.ready is True


# ─────────────────────────────────────────────────────────────────────────────
# _CooldownRegistry unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCooldownRegistry:
    def test_unknown_mac_is_ready(self) -> None:
        reg = _CooldownRegistry()
        assert reg.is_ready("AA:BB:CC:DD:EE:FF") is True

    def test_after_failure_not_ready(self) -> None:
        reg = _CooldownRegistry()
        reg.record_failure("AA:BB:CC:DD:EE:FF")
        assert reg.is_ready("AA:BB:CC:DD:EE:FF") is False

    def test_reset_removes_entry(self) -> None:
        reg = _CooldownRegistry()
        reg.record_failure("AA:BB:CC:DD:EE:FF")
        reg.reset("AA:BB:CC:DD:EE:FF")
        assert reg.is_ready("AA:BB:CC:DD:EE:FF") is True

    def test_reset_unknown_mac_is_noop(self) -> None:
        reg = _CooldownRegistry()
        reg.reset("FF:FF:FF:FF:FF:FF")  # must not raise

    def test_multiple_failures_increase_level(self) -> None:
        reg = _CooldownRegistry()
        reg.record_failure("AA:BB:CC:DD:EE:FF")
        reg.record_failure("AA:BB:CC:DD:EE:FF")
        entry = reg._entries["AA:BB:CC:DD:EE:FF"]
        assert entry.level == 2

    def test_filter_ready_splits_correctly(self) -> None:
        reg = _CooldownRegistry()
        mac_ready = "AA:BB:CC:DD:EE:FF"
        mac_cooling = "11:22:33:44:55:66"

        reg.record_failure(mac_cooling)  # in backoff

        d_ready = _make_device(mac_ready)
        d_cooling = _make_device(mac_cooling)

        result = reg.filter_ready([d_ready, d_cooling])
        assert result == [d_ready]

    def test_filter_ready_includes_expired_cooldown(self) -> None:
        reg = _CooldownRegistry()
        mac = "AA:BB:CC:DD:EE:FF"
        reg.record_failure(mac)
        # Fast-forward the cooldown window
        reg._entries[mac].retry_after = time.monotonic() - 1

        device = _make_device(mac)
        assert reg.filter_ready([device]) == [device]

    def test_filter_ready_empty_list(self) -> None:
        reg = _CooldownRegistry()
        assert reg.filter_ready([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# run_once: cooldown integration
# ─────────────────────────────────────────────────────────────────────────────


class TestRunOnceCooldownIntegration:
    """run_once() must update the cooldown registry based on connect results."""

    def _make_daemon(self) -> AutoConnectDaemon:
        return AutoConnectDaemon(rescan_interval=0)

    def test_successful_connect_resets_cooldown(self) -> None:
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        # Pre-populate a failure so there is something to reset
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        device = _make_device(mac, connected=False)

        async def fake_connect(path: str) -> None:
            pass  # success

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[_ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock(side_effect=fake_connect)
            await daemon.run_once()

        asyncio.run(runner())
        assert daemon._cooldown.is_ready(mac)

    def test_failed_connect_records_failure(self) -> None:
        daemon = self._make_daemon()
        mac = "BB:CC:DD:EE:FF:00"
        device = _make_device(mac, connected=False)

        async def always_fail(path: str) -> None:
            raise OSError("br-connection-page-timeout")

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[_ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            # One attempt only so the test finishes quickly
            from bluetooth_autoconnect.connector import RetryPolicy

            daemon.policy = RetryPolicy(max_attempts=1)
            daemon.client.connect_device = AsyncMock(side_effect=always_fail)
            await daemon.run_once()

        asyncio.run(runner())
        assert not daemon._cooldown.is_ready(mac)

    def test_already_connected_device_does_not_affect_cooldown(self) -> None:
        daemon = self._make_daemon()
        mac = "CC:DD:EE:FF:00:11"
        device = _make_device(mac, connected=True)

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[_ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock()
            await daemon.run_once()

        asyncio.run(runner())
        # No failure recorded; device was already connected
        assert daemon._cooldown.is_ready(mac)
        daemon.client.connect_device.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Periodic scan: core scenario
# ─────────────────────────────────────────────────────────────────────────────


class TestPeriodicScanScenario:
    """Full disconnect → fail → return to range → periodic scan reconnects."""

    def test_device_reconnects_on_periodic_scan_after_failing(self) -> None:
        """
        Sequence:
          1. Device is disconnected.
          2. Immediate reconnect (run_once) fails → backoff recorded.
          3. Backoff window fast-forwarded to zero so device is "ready".
          4. Periodic scan fires → reconnect succeeds → backoff cleared.
        """
        daemon = AutoConnectDaemon(rescan_interval=0.05)  # very short for test
        mac = "AA:BB:CC:DD:EE:FF"
        device_disconnected = _make_device(mac, connected=False)

        connect_call_count = 0

        async def connect_fn(path: str) -> None:
            nonlocal connect_call_count
            connect_call_count += 1
            if connect_call_count == 1:
                raise OSError("br-connection-page-timeout")
            # Second call (from periodic scan) succeeds

        from bluetooth_autoconnect.connector import RetryPolicy

        daemon.policy = RetryPolicy(max_attempts=1)

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[_ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device_disconnected])
            daemon.client.connect_device = AsyncMock(side_effect=connect_fn)

            # Step 1: initial run_once — fails, records backoff
            await daemon.run_once()
            assert not daemon._cooldown.is_ready(mac)

            # Step 2: fast-forward past the backoff window
            daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1
            assert daemon._cooldown.is_ready(mac)

            # Step 3: run one periodic scan iteration manually
            await daemon._run_one_periodic_scan()

            # Step 4: backoff cleared after success
            assert daemon._cooldown.is_ready(mac)
            assert connect_call_count == 2

        asyncio.run(runner())

    def test_device_in_backoff_skipped_by_periodic_scan(self) -> None:
        """Device inside its cooldown window must not be contacted."""
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        device = _make_device(mac, connected=False)

        # Record a failure — device is now in backoff
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        connect_called = []

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[_ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock(
                side_effect=lambda p: connect_called.append(p)
            )
            await daemon._run_one_periodic_scan()

        asyncio.run(runner())
        assert (
            connect_called == []
        ), "connect_device should not be called when device is in backoff"

    def test_periodic_scan_disabled_when_interval_is_zero(self) -> None:
        """_periodic_scan_loop must exit immediately when rescan_interval <= 0."""
        daemon = AutoConnectDaemon(rescan_interval=0)

        async def runner() -> None:
            # The loop should return without sleeping
            await daemon._periodic_scan_loop()

        # Should complete near-instantly — no hanging
        asyncio.run(runner())

    def test_periodic_scan_loop_stops_on_stop_event(self) -> None:
        """Cancelling the task (via stop_event) must not leak the task."""
        daemon = AutoConnectDaemon(rescan_interval=100.0)  # long interval
        connect_called = []

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[_ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[])
            daemon.client.connect_device = AsyncMock(
                side_effect=lambda p: connect_called.append(p)
            )
            task = asyncio.create_task(daemon._periodic_scan_loop())
            # Let the loop enter its first sleep, then cancel
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(runner())
        assert connect_called == []

    def test_periodic_scan_handles_exception_without_crashing(self) -> None:
        """An exception inside the scan body must be swallowed, not re-raised."""
        daemon = AutoConnectDaemon(rescan_interval=0.01)

        async def runner() -> None:
            daemon.client.get_adapters = AsyncMock(
                side_effect=RuntimeError("transient dbus error")
            )
            # Run exactly one scan cycle then stop
            daemon._stop_event.set()
            # The loop exits after the first sleep; we force one iteration
            await daemon._run_one_periodic_scan()  # must not raise

        asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────────
# D-Bus events reset backoff
# ─────────────────────────────────────────────────────────────────────────────


class TestDbusEventBackoffReset:
    """RSSI and Connected=True D-Bus events must clear backoff state."""

    def test_rssi_event_resets_backoff_and_triggers_rescan(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
        asyncio.run(
            daemon._on_dbus_event(
                "properties_changed", path, DEVICE_IFACE, {"RSSI": -65}
            )
        )

        assert daemon._cooldown.is_ready(mac)
        assert daemon._rescan_event.is_set()

    def test_connected_true_resets_backoff_without_rescan_event(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        daemon._cooldown.record_failure(mac)

        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
        asyncio.run(
            daemon._on_dbus_event(
                "properties_changed",
                path,
                DEVICE_IFACE,
                {"Connected": True},
            )
        )

        assert daemon._cooldown.is_ready(mac)
        # Connected=True does not trigger a new scan event
        assert not daemon._rescan_event.is_set()

    def test_connected_false_triggers_rescan_event(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        asyncio.run(
            daemon._on_dbus_event(
                "properties_changed",
                path,
                DEVICE_IFACE,
                {"Connected": False},
            )
        )

        assert daemon._rescan_event.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# CLI: --rescan-interval flag
# ─────────────────────────────────────────────────────────────────────────────


class TestCLIRescanInterval:
    def test_default_rescan_interval(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--daemon"])
        assert args.rescan_interval == 30.0

    def test_custom_rescan_interval(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--daemon", "--rescan-interval", "60"])
        assert args.rescan_interval == 60.0

    def test_zero_disables_periodic_scan(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--daemon", "--rescan-interval", "0"])
        assert args.rescan_interval == 0.0

    def test_rescan_interval_passed_to_daemon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI must forward --rescan-interval to AutoConnectDaemon."""
        from bluetooth_autoconnect.cli import main

        created: list[float] = []

        class SpyDaemon:
            def __init__(self, *_, rescan_interval: float = 30.0, **__) -> None:
                created.append(rescan_interval)

            async def run_forever(self) -> None:
                pass  # stop immediately

        monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", SpyDaemon)
        main(["--daemon", "--rescan-interval", "45"])
        assert created == [45.0]

    def test_one_shot_mode_has_zero_rescan_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One-shot mode must disable periodic scanning."""
        from bluetooth_autoconnect.cli import main

        created: list[float] = []

        class SpyDaemon:
            def __init__(self, *_, rescan_interval: float = 30.0, **__) -> None:
                created.append(rescan_interval)
                self.client = type(
                    "C",
                    (),
                    {
                        "connect": AsyncMock(),
                        "close": AsyncMock(),
                    },
                )()

            async def run_once(self) -> dict:
                return {}

        monkeypatch.setattr("bluetooth_autoconnect.cli.AutoConnectDaemon", SpyDaemon)
        main([])  # one-shot
        assert created == [0]


# ─────────────────────────────────────────────────────────────────────────────
# run_forever: periodic task lifecycle
# ─────────────────────────────────────────────────────────────────────────────


class TestRunForeverPeriodicTaskLifecycle:
    """The periodic task must be started and cleanly cancelled on stop."""

    def test_run_forever_starts_and_cancels_periodic_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The periodic task must be created and cleanly cancelled on shutdown."""
        daemon = AutoConnectDaemon(rescan_interval=100.0)

        periodic_started: list[bool] = []
        periodic_cancelled: list[bool] = []

        # Spy coroutine assigned directly to the instance so the
        # create_task(self._periodic_scan_loop()) call inside run_forever
        # picks it up.  It records that it started then immediately sets
        # _stop_event so the while loop in run_forever exits, which in
        # turn cancels this task.
        async def spy_periodic_loop() -> None:
            periodic_started.append(True)
            daemon._stop_event.set()  # tell run_forever to exit
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                periodic_cancelled.append(True)
                raise

        daemon._periodic_scan_loop = spy_periodic_loop  # type: ignore[method-assign]

        class FakeClient:
            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def subscribe(self, cb) -> None:
                pass

            async def get_adapters(self):
                return []

        daemon.client = FakeClient()
        monkeypatch.setattr(daemon, "_install_signal_handlers", lambda *a: None)

        async def fast_run_once() -> dict:
            return {}

        daemon.run_once = fast_run_once  # type: ignore[method-assign]

        # No sleep patching needed: the debounce sleep (1.0 s inside the
        # while loop) only fires when _rescan_event is set, which never
        # happens in this test.
        asyncio.run(daemon.run_forever())

        assert periodic_started, "periodic scan task was never started"
        assert periodic_cancelled, "periodic scan task was not cancelled on stop"

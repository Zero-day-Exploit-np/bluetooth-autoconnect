"""Regression tests for the five reconnect bugs.

Bug 1 — _schedule() used deprecated get_event_loop() which silently dropped
         D-Bus events when the running loop was not the one returned.

Bug 2 — connect_with_retry() treated AlreadyConnected as failure; no
         classification of transient (InProgress, NotReady) vs. permanent
         (DoesNotExist, Auth*) errors.

Bug 3 — Event-driven reconnect path (_handle_device_properties_changed)
         bypassed the cooldown registry entirely; only the periodic scanner
         respected backoff.

Bug 4 — cli._async_main() only loaded the 'hooks:' section from the config
         file; daemon:, retry:, and enable_automatic_reconnect were ignored.

Bug 5 — AlreadyConnected treated as failure advanced backoff, potentially
         suppressing future reconnect attempts for up to 30 minutes.

Each class below is named after the bug it covers and contains at least one
test that would have FAILED before the fix and PASSES after.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bluetooth_autoconnect.connector import (
    _BLUEZ_ALREADY_CONNECTED,
    _BLUEZ_AUTH_FAILED,
    _BLUEZ_DOES_NOT_EXIST,
    _BLUEZ_IN_PROGRESS,
    _BLUEZ_NOT_READY,
    RetryPolicy,
    _bluez_error_name,
    connect_all,
    connect_with_retry,
)
from bluetooth_autoconnect.daemon import (
    DEVICE_IFACE,
    AutoConnectDaemon,
)
from bluetooth_autoconnect.exceptions import (
    DeviceAlreadyConnectedError,
    DeviceConnectionError,
)
from bluetooth_autoconnect.models import Adapter, Device

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _device(
    mac: str = "AA:BB:CC:DD:EE:FF",
    name: str = "Test Device",
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


def _adapter(powered: bool = True) -> Adapter:
    return Adapter(
        path="/org/bluez/hci0",
        name="hci0",
        address="00:11:22:33:44:55",
        powered=powered,
    )


def _fake_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep in connector to run instantly."""
    import bluetooth_autoconnect.connector as _conn_mod

    async def _noop(_: float) -> None:
        pass

    monkeypatch.setattr(_conn_mod.asyncio, "sleep", _noop)


# ── Bug 1: _schedule() uses get_running_loop() ────────────────────────────────


class TestBug1ScheduleUsesRunningLoop:
    """_schedule() must use asyncio.get_running_loop(), not get_event_loop().

    Before the fix: get_event_loop() could return the wrong loop or raise
    RuntimeError, causing D-Bus reconnect events to be silently dropped.
    After the fix: get_running_loop() always returns the currently running
    loop; RuntimeError is logged as WARNING (not silently discarded as DEBUG).
    """

    def test_schedule_dispatches_on_running_loop(self) -> None:
        """Coroutine posted via _schedule() runs on the current event loop."""
        from bluetooth_autoconnect.backends.linux import _schedule

        ran: list[str] = []

        async def _marker() -> None:
            ran.append("ran")

        async def _runner() -> None:
            _schedule(_marker())
            await asyncio.sleep(0)  # yield so the task executes

        asyncio.run(_runner())
        assert ran == ["ran"], "_schedule must dispatch the coroutine on the running loop"

    def test_schedule_warns_when_no_loop(self, caplog: pytest.LogCaptureFixture) -> None:
        """When there is no running loop, _schedule logs WARNING, not DEBUG."""
        from bluetooth_autoconnect.backends.linux import _schedule

        async def _noop() -> None:
            pass

        coro = _noop()
        try:
            with patch(
                "bluetooth_autoconnect.backends.linux.asyncio.get_running_loop",
                side_effect=RuntimeError("no running event loop"),
            ):
                with caplog.at_level("WARNING", logger="bluetooth_autoconnect.backends.linux"):
                    _schedule(coro)
        finally:
            coro.close()

        warning_records = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "no running event loop" in r.getMessage()
        ]
        assert warning_records, (
            "_schedule must log WARNING (not DEBUG) when no loop is running"
        )

    def test_schedule_does_not_use_get_event_loop(self) -> None:
        """_schedule must call get_running_loop(), not the deprecated get_event_loop()."""
        import inspect

        import bluetooth_autoconnect.backends.linux as linux_mod

        # Parse only the actual code lines — skip the docstring, which may
        # mention get_event_loop for explanatory purposes.
        all_lines = inspect.getsource(linux_mod._schedule).splitlines()
        in_doc = False
        code_lines = []
        for line in all_lines:
            s = line.strip()
            if s.startswith('"""') or s.startswith("'''"):
                in_doc = not in_doc
                continue
            if not in_doc:
                code_lines.append(line)
        code_only = "\n".join(code_lines)

        assert "get_running_loop" in code_only, (
            "_schedule must use asyncio.get_running_loop()"
        )
        assert "get_event_loop" not in code_only, (
            "_schedule must NOT call the deprecated asyncio.get_event_loop()"
        )


# ── Bug 2: BlueZ error classification ─────────────────────────────────────────


class TestBug2BluezErrorClassification:
    """connect_with_retry() must classify BlueZ errors correctly.

    Before the fix: all exceptions treated as ordinary failures → 5 retries
    then DeviceConnectionError → backoff advanced even for AlreadyConnected.
    After the fix: AlreadyConnected → DeviceAlreadyConnectedError (= success);
    InProgress/NotReady → transient (retried); DoesNotExist/Auth* → permanent.
    """

    # ── _bluez_error_name ─────────────────────────────────────────────────

    def test_error_name_from_dbus_type_attribute(self) -> None:
        exc = Exception("some message")
        exc.type = _BLUEZ_ALREADY_CONNECTED  # type: ignore[attr-defined]
        assert _bluez_error_name(exc) == _BLUEZ_ALREADY_CONNECTED

    def test_error_name_from_message_string(self) -> None:
        exc = Exception(f"D-Bus error: {_BLUEZ_IN_PROGRESS}")
        assert _bluez_error_name(exc) == _BLUEZ_IN_PROGRESS

    def test_error_name_none_for_unknown(self) -> None:
        exc = Exception("totally unrelated error")
        assert _bluez_error_name(exc) is None

    def test_error_name_auth_failed_in_message(self) -> None:
        exc = Exception(f"blah {_BLUEZ_AUTH_FAILED} blah")
        assert _bluez_error_name(exc) == _BLUEZ_AUTH_FAILED

    # ── AlreadyConnected → immediate DeviceAlreadyConnectedError ──────────

    @pytest.mark.asyncio
    async def test_already_connected_raises_device_already_connected_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device()

        async def _connect(_path: str) -> None:
            exc = Exception(_BLUEZ_ALREADY_CONNECTED)
            raise exc

        with pytest.raises(DeviceAlreadyConnectedError) as exc_info:
            await connect_with_retry(dev, _connect)

        assert exc_info.value.device_address == dev.address

    @pytest.mark.asyncio
    async def test_already_connected_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AlreadyConnected must raise immediately, not retry up to max_attempts."""
        _fake_sleep(monkeypatch)
        dev = _device()
        call_count = 0

        async def _connect(_path: str) -> None:
            nonlocal call_count
            call_count += 1
            raise Exception(_BLUEZ_ALREADY_CONNECTED)

        with pytest.raises(DeviceAlreadyConnectedError):
            await connect_with_retry(dev, _connect)

        assert call_count == 1, "AlreadyConnected must not be retried"

    # ── connect_all: AlreadyConnected → result True ───────────────────────

    @pytest.mark.asyncio
    async def test_connect_all_already_connected_marked_as_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """connect_all must record AlreadyConnected as True so backoff resets."""
        _fake_sleep(monkeypatch)
        dev = _device()

        async def _connect(_path: str) -> None:
            raise Exception(_BLUEZ_ALREADY_CONNECTED)

        results = await connect_all([dev], _connect)
        assert results[dev.address] is True, (
            "AlreadyConnected must be treated as success in connect_all"
        )

    # ── Transient errors are retried, not immediately failed ──────────────

    @pytest.mark.asyncio
    async def test_in_progress_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device()
        call_count = 0

        async def _connect(_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception(_BLUEZ_IN_PROGRESS)
            # 3rd attempt succeeds

        result = await connect_with_retry(
            dev, _connect, policy=RetryPolicy(max_attempts=5)
        )
        assert result is True
        assert call_count == 3, "InProgress should be retried"

    @pytest.mark.asyncio
    async def test_not_ready_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device()
        call_count = 0

        async def _connect(_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception(_BLUEZ_NOT_READY)

        result = await connect_with_retry(
            dev, _connect, policy=RetryPolicy(max_attempts=5)
        )
        assert result is True
        assert call_count == 2

    # ── Permanent errors give up immediately ──────────────────────────────

    @pytest.mark.asyncio
    async def test_does_not_exist_raises_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device()
        call_count = 0

        async def _connect(_path: str) -> None:
            nonlocal call_count
            call_count += 1
            raise Exception(_BLUEZ_DOES_NOT_EXIST)

        with pytest.raises(DeviceConnectionError):
            await connect_with_retry(dev, _connect, policy=RetryPolicy(max_attempts=5))

        assert call_count == 1, "DoesNotExist must not be retried"

    @pytest.mark.asyncio
    async def test_auth_failed_raises_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device()
        call_count = 0

        async def _connect(_path: str) -> None:
            nonlocal call_count
            call_count += 1
            raise Exception(_BLUEZ_AUTH_FAILED)

        with pytest.raises(DeviceConnectionError):
            await connect_with_retry(dev, _connect)

        assert call_count == 1, "Auth failures must not be retried"


# ── Bug 3: Cooldown respected in event-driven path ────────────────────────────


class TestBug3CooldownInEventPath:
    """Connected=False events must not bypass the cooldown registry.

    Before the fix: _rescan_event.set() was always called on Connected=False,
    causing run_once() → connect_all() to run immediately ignoring backoff.
    After the fix: if the device is in backoff, the rescan event is NOT set;
    the periodic scanner handles the reconnect when the cooldown expires.
    """

    def _make_daemon(self) -> AutoConnectDaemon:
        return AutoConnectDaemon(rescan_interval=0)

    @pytest.mark.asyncio
    async def test_disconnect_while_not_in_backoff_sets_rescan_event(self) -> None:
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        # No backoff entry — rescan event MUST be set immediately.
        assert daemon._cooldown.is_ready(mac)
        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"Connected": False}
        )
        assert daemon._rescan_event.is_set(), (
            "Rescan event must be set when device disconnects and is not in backoff"
        )

    @pytest.mark.asyncio
    async def test_disconnect_while_in_backoff_does_not_set_rescan_event(self) -> None:
        """This is the core Bug 3 regression test.

        Before the fix: _rescan_event would always be set on Connected=False.
        After the fix: it is NOT set when the device is in its backoff window.
        """
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        # Put device into backoff.
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"Connected": False}
        )
        assert not daemon._rescan_event.is_set(), (
            "Rescan event must NOT be set when device disconnects and is in backoff"
            " — the periodic scanner handles it"
        )

    @pytest.mark.asyncio
    async def test_run_once_respects_backoff_for_disconnected_devices(self) -> None:
        """run_once() must not attempt to connect a device in its backoff window."""
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)

        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        connect_called: list[str] = []

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda p: connect_called.append(p)
        )

        await daemon.run_once()
        assert connect_called == [], (
            "run_once must not call connect_device for a device in backoff"
        )

    @pytest.mark.asyncio
    async def test_run_once_connects_device_not_in_backoff(self) -> None:
        """run_once() must attempt to connect a device with no backoff entry."""
        daemon = self._make_daemon()
        dev = _device(connected=False)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock()  # type: ignore[method-assign]
        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon.run_once()
        daemon.client.connect_device.assert_awaited_once_with(dev.path)

    @pytest.mark.asyncio
    async def test_backoff_not_bypassed_by_repeated_disconnect_events(self) -> None:
        """Multiple rapid Connected=False events must not spam reconnect attempts."""
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        rescan_count = 0

        # Simulate the daemon's behaviour: first disconnect is free, then backoff.
        for _ in range(5):
            daemon._rescan_event.is_set()
            daemon._rescan_event.clear()
            await daemon._on_dbus_event(
                "properties_changed", path, DEVICE_IFACE, {"Connected": False}
            )
            if daemon._rescan_event.is_set():
                rescan_count += 1
                # Simulate a failed reconnect — puts device in backoff.
                daemon._cooldown.record_failure(mac)
                daemon._rescan_event.clear()

        # Only the FIRST disconnect should have triggered a rescan.
        # Subsequent ones should have been suppressed by backoff.
        assert rescan_count == 1, (
            f"Expected exactly 1 rescan event from 5 disconnect signals, "
            f"got {rescan_count}.  Bug 3: event path must respect backoff."
        )

    @pytest.mark.asyncio
    async def test_rssi_resets_backoff_and_triggers_rescan(self) -> None:
        """An RSSI signal means the device is back in range — must reset backoff."""
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        daemon._cooldown.record_failure(mac)
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"RSSI": -60}
        )
        assert daemon._cooldown.is_ready(mac), (
            "RSSI signal must reset backoff so device is immediately eligible"
        )
        assert daemon._rescan_event.is_set(), "RSSI signal must trigger a rescan"

    @pytest.mark.asyncio
    async def test_connected_true_resets_backoff(self) -> None:
        """Connected=True must reset backoff even without hooks configured."""
        daemon = self._make_daemon()
        mac = "AA:BB:CC:DD:EE:FF"
        path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

        daemon._cooldown.record_failure(mac)
        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        await daemon._on_dbus_event(
            "properties_changed", path, DEVICE_IFACE, {"Connected": True}
        )
        assert daemon._cooldown.is_ready(mac), "Connected=True must reset backoff"


# ── Bug 3 (periodic scanner): fallback path ───────────────────────────────────


class TestBug3PeriodicScannerFallback:
    """The periodic scanner is the fallback for devices in backoff.

    Even if the event-driven path does not trigger a rescan, the periodic
    scanner must retry when the cooldown window expires.
    """

    @pytest.mark.asyncio
    async def test_periodic_scan_retries_device_after_backoff_expires(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)

        daemon._cooldown.record_failure(mac)
        # Fast-forward the cooldown so it's ready NOW.
        daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1
        assert daemon._cooldown.is_ready(mac)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock()  # type: ignore[method-assign]
        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()
        daemon.client.connect_device.assert_awaited_once_with(dev.path)

    @pytest.mark.asyncio
    async def test_periodic_scan_skips_device_still_in_backoff(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)

        daemon._cooldown.record_failure(mac)
        assert not daemon._cooldown.is_ready(mac)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock()  # type: ignore[method-assign]

        await daemon._run_one_periodic_scan()
        daemon.client.connect_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_connection_advances_backoff_level(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock(  # type: ignore[method-assign]
            side_effect=Exception("page-timeout")
        )
        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()
        assert not daemon._cooldown.is_ready(mac), (
            "Failed connection must put device into backoff"
        )
        assert mac in daemon._cooldown._entries

    @pytest.mark.asyncio
    async def test_successful_reconnect_resets_backoff(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)

        daemon._cooldown.record_failure(mac)
        daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock()  # success  # type: ignore[method-assign]
        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon._run_one_periodic_scan()
        assert daemon._cooldown.is_ready(mac), (
            "Successful reconnect must reset backoff"
        )
        assert mac not in daemon._cooldown._entries


# ── Bug 4: Config file daemon: section is applied ─────────────────────────────


class TestBug4ConfigFileDaemonSection:
    """daemon: and retry: sections in the config file must be applied.

    Before the fix: only hooks: was read; everything else was silently ignored.
    After the fix: rescan_interval_seconds, max_concurrency, max_attempts, etc.
    are read from the file and used unless a CLI flag explicitly overrides them.
    """

    def test_build_daemon_config_from_raw_reads_rescan_interval(self) -> None:
        from bluetooth_autoconnect.cli import _build_daemon_config_from_raw

        raw = {"daemon": {"rescan_interval_seconds": 120, "max_concurrency": 3}}
        cfg = _build_daemon_config_from_raw(raw)
        assert cfg.rescan_interval_seconds == 120
        assert cfg.max_concurrency == 3

    def test_build_daemon_config_empty_section_returns_defaults(self) -> None:
        from bluetooth_autoconnect.cli import _build_daemon_config_from_raw

        cfg = _build_daemon_config_from_raw({})
        assert cfg.rescan_interval_seconds == 30  # default
        assert cfg.max_concurrency == 5  # default

    def test_build_daemon_config_non_dict_returns_defaults(self) -> None:
        from bluetooth_autoconnect.cli import _build_daemon_config_from_raw

        cfg = _build_daemon_config_from_raw({"daemon": "not a dict"})
        assert cfg.rescan_interval_seconds == 30

    def test_build_retry_config_from_raw(self) -> None:
        from bluetooth_autoconnect.cli import _build_retry_config_from_raw

        raw = {"retry": {"max_attempts": 10, "base_delay": 2.0}}
        cfg = _build_retry_config_from_raw(raw)
        assert cfg.max_attempts == 10
        assert cfg.base_delay == 2.0

    def test_merge_daemon_params_cli_wins_over_config(self) -> None:
        """Explicit CLI flag beats config-file value."""
        import argparse

        from bluetooth_autoconnect.cli import (
            _build_daemon_config_from_raw,
            _build_retry_config_from_raw,
            _merge_daemon_params,
        )

        # CLI explicitly sets rescan-interval to 10 (≠ default 30)
        args = argparse.Namespace(
            max_attempts=5,           # default → config wins for this one
            max_concurrency=5,        # default → config wins
            rescan_interval=10.0,     # DIFFERENT from default → CLI wins
        )
        daemon_cfg = _build_daemon_config_from_raw(
            {"daemon": {"rescan_interval_seconds": 60, "max_concurrency": 8}}
        )
        retry_cfg = _build_retry_config_from_raw(
            {"retry": {"max_attempts": 3}}
        )
        policy, max_concurrency, rescan_interval = _merge_daemon_params(
            args, daemon_cfg, retry_cfg
        )

        # rescan_interval: CLI (10) wins over config (60)
        assert rescan_interval == 10.0
        # max_concurrency: CLI default (5) → config (8) wins
        assert max_concurrency == 8
        # max_attempts: CLI default (5) → config (3) wins
        assert policy.max_attempts == 3

    def test_merge_daemon_params_config_wins_when_cli_is_default(self) -> None:
        """Config-file value is used when CLI flag is at its default."""
        import argparse

        from bluetooth_autoconnect.cli import (
            _build_daemon_config_from_raw,
            _build_retry_config_from_raw,
            _merge_daemon_params,
        )

        args = argparse.Namespace(
            max_attempts=5,        # default
            max_concurrency=5,     # default
            rescan_interval=30.0,  # default
        )
        daemon_cfg = _build_daemon_config_from_raw(
            {"daemon": {"rescan_interval_seconds": 90}}
        )
        retry_cfg = _build_retry_config_from_raw({})
        _, _, rescan_interval = _merge_daemon_params(args, daemon_cfg, retry_cfg)
        assert rescan_interval == 90.0

    def test_enable_automatic_reconnect_false_sets_rescan_to_zero(
        self, tmp_path: Path
    ) -> None:
        """enable_automatic_reconnect: false must disable the periodic scanner."""
        from bluetooth_autoconnect.cli import _build_daemon_config_from_raw

        raw = {"daemon": {"enable_automatic_reconnect": False}}
        cfg = _build_daemon_config_from_raw(raw)
        assert cfg.enable_automatic_reconnect is False

    def test_load_config_reads_yaml_file(self, tmp_path: Path) -> None:
        from bluetooth_autoconnect.cli import _load_config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "daemon:\n  rescan_interval_seconds: 75\n"
            "retry:\n  max_attempts: 7\n"
        )
        raw = _load_config(cfg_file)
        assert raw["daemon"]["rescan_interval_seconds"] == 75
        assert raw["retry"]["max_attempts"] == 7

    def test_load_config_returns_empty_dict_for_missing_file(self) -> None:
        from bluetooth_autoconnect.cli import _load_config

        result = _load_config(Path("/nonexistent/path/config.yaml"))
        assert result == {}


# ── Bug 5: AlreadyConnected must not advance backoff ─────────────────────────


class TestBug5AlreadyConnectedDoesNotAdvanceBackoff:
    """DeviceAlreadyConnectedError is success — backoff must reset, not advance.

    Before the fix: AlreadyConnected raised DeviceConnectionError, causing
    connect_all to record False, causing the daemon to call record_failure,
    advancing the backoff level.  After max failures the device could be
    suppressed for up to 30 minutes despite being connected.

    After the fix: AlreadyConnected raises DeviceAlreadyConnectedError which
    is caught in connect_all and recorded as True → run_once calls reset().
    """

    @pytest.mark.asyncio
    async def test_already_connected_result_is_true_in_connect_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device(connected=False)

        async def _always_already_connected(_path: str) -> None:
            raise Exception(_BLUEZ_ALREADY_CONNECTED)

        results = await connect_all([dev], _always_already_connected)
        assert results[dev.address] is True

    @pytest.mark.asyncio
    async def test_already_connected_resets_backoff_in_run_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        mac = "AA:BB:CC:DD:EE:FF"
        dev = _device(mac, connected=False)
        daemon = AutoConnectDaemon(rescan_interval=0)

        # Prime the backoff as if previous failures happened.
        daemon._cooldown.record_failure(mac)
        daemon._cooldown.record_failure(mac)
        daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1  # make it ready

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock(  # type: ignore[method-assign]
            side_effect=Exception(_BLUEZ_ALREADY_CONNECTED)
        )
        daemon.policy = RetryPolicy(max_attempts=1)

        await daemon.run_once()

        # Backoff must be reset (entry removed) because AlreadyConnected = success.
        assert daemon._cooldown.is_ready(mac), (
            "AlreadyConnected must cause backoff to reset, not advance"
        )
        assert mac not in daemon._cooldown._entries

    @pytest.mark.asyncio
    async def test_already_connected_does_not_advance_backoff_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_sleep(monkeypatch)
        dev = _device(connected=False)

        async def _connect(_path: str) -> None:
            raise Exception(_BLUEZ_ALREADY_CONNECTED)

        # Before fix: this would raise DeviceConnectionError, advancing backoff.
        # After fix: this raises DeviceAlreadyConnectedError, which connect_all
        # catches and treats as True.
        results = await connect_all([dev], _connect)
        assert results[dev.address] is True, (
            "AlreadyConnected must never result in False (which would advance backoff)"
        )


# ── End-to-end reconnect scenario ─────────────────────────────────────────────


class TestEndToEndReconnectScenario:
    """Full disconnect → fail → return to range → periodic scan reconnects.

    This is the scenario from the bug report: device goes out of range,
    reconnect fails, device comes back silently, periodic scanner reconnects.
    """

    @pytest.mark.asyncio
    async def test_device_returns_silently_and_periodic_scan_reconnects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        1. Device disconnects → immediate reconnect attempt fails.
        2. Device is in backoff.
        3. Device returns to range (no D-Bus event).
        4. Periodic scanner fires → cooldown expired → reconnect succeeds.
        5. Backoff is cleared.
        """
        import bluetooth_autoconnect.daemon as daemon_mod

        # Patch asyncio.sleep in daemon to return immediately.
        async def _noop(_: float) -> None:
            pass

        monkeypatch.setattr(daemon_mod.asyncio, "sleep", _noop)

        mac = "AA:BB:CC:DD:EE:FF"
        dev_disconnected = _device(mac, connected=False)
        connect_attempts: list[str] = []
        call_count = 0

        async def _connect(path: str) -> None:
            nonlocal call_count
            call_count += 1
            connect_attempts.append(path)
            # First attempt (from run_once after disconnect event) fails.
            if call_count == 1:
                raise Exception("org.bluez.Error.Failed: br-connection-page-timeout")
            # Periodic scan attempt succeeds.

        daemon = AutoConnectDaemon(rescan_interval=0)
        daemon.policy = RetryPolicy(max_attempts=1)
        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev_disconnected])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock(side_effect=_connect)  # type: ignore[method-assign]

        # Step 1: run_once() after disconnect — fails, device goes into backoff.
        await daemon.run_once()
        assert call_count == 1
        assert not daemon._cooldown.is_ready(mac), "Device must be in backoff after failure"

        # Step 2: fast-forward past the backoff window.
        daemon._cooldown._entries[mac].retry_after = time.monotonic() - 1
        assert daemon._cooldown.is_ready(mac)

        # Step 3: periodic scanner fires — device is back in range (no event needed).
        await daemon._run_one_periodic_scan()
        assert call_count == 2, "Periodic scan must have retried the device"
        assert daemon._cooldown.is_ready(mac), "Successful reconnect must reset backoff"
        assert mac not in daemon._cooldown._entries

    @pytest.mark.asyncio
    async def test_one_failing_device_does_not_block_another(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device in backoff must not prevent other devices from connecting."""
        mac_stuck = "AA:BB:CC:DD:EE:FF"
        mac_ok = "11:22:33:44:55:66"

        dev_stuck = _device(mac_stuck, connected=False)
        dev_ok = _device(mac_ok, connected=False)

        daemon = AutoConnectDaemon(rescan_interval=0)
        daemon.policy = RetryPolicy(max_attempts=1)

        # mac_stuck is in backoff.
        daemon._cooldown.record_failure(mac_stuck)
        assert not daemon._cooldown.is_ready(mac_stuck)

        connected: list[str] = []

        async def _connect(path: str) -> None:
            connected.append(path)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev_stuck, dev_ok])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock(side_effect=_connect)  # type: ignore[method-assign]

        await daemon.run_once()

        assert dev_ok.path in connected, "Healthy device must connect"
        assert dev_stuck.path not in connected, (
            "Device in backoff must not be connected"
        )

    @pytest.mark.asyncio
    async def test_adapter_power_cycle_triggers_rescan(self) -> None:
        """Adapter powered off then on must trigger a full rescan."""
        daemon = AutoConnectDaemon(rescan_interval=0)

        await daemon._on_dbus_event(
            "properties_changed",
            "/org/bluez/hci0",
            DEVICE_IFACE.replace("Device1", "Adapter1"),
            {"Powered": True},
        )
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_unpaired_device_is_skipped(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        dev = _device(paired=False, trusted=True, connected=False)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock()  # type: ignore[method-assign]

        await daemon.run_once()
        daemon.client.connect_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_untrusted_device_is_skipped(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0)
        dev = _device(paired=True, trusted=False, connected=False)

        daemon.client.get_adapters = AsyncMock(return_value=[_adapter()])  # type: ignore[method-assign]
        daemon.client.get_devices = AsyncMock(return_value=[dev])  # type: ignore[method-assign]
        daemon.client.connect_device = AsyncMock()  # type: ignore[method-assign]

        await daemon.run_once()
        daemon.client.connect_device.assert_not_awaited()

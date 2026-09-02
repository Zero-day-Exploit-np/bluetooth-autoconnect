"""Tests for the hook execution system (bluetooth_autoconnect.hooks).

Coverage targets
----------------
hooks.py    HookEvent enum
            validate_hook_paths   — all validation branches
            HookRunner.fire       — CONNECTED / DISCONNECTED dispatch
            HookRunner._run_one   — success, non-zero exit, launch failure,
                                    timeout + kill, unexpected communicate error
            _build_env            — env var presence and values
            _decode_output        — normal, empty, truncated
            _log_output           — exit-0 (DEBUG) vs non-zero (WARNING)
            build_hook_runner     — both lists empty → None, mixed valid/invalid

daemon.py   _fire_disconnect_hook — real device found, synthetic fallback
            run_once hook firing  — on_connect triggered after success
            _on_dbus_event        — Connected=False triggers _fire_disconnect_hook

config.py   HooksConfig           — to_dict, post_init dict coercion
            AutoConnectConfig     — hooks field wired correctly

cli.py      _load_config          — file absent, YAML absent, bad YAML, good YAML
            _build_hook_runner_from_config — no hooks key, valid hooks
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bluetooth_autoconnect.config import AutoConnectConfig, HooksConfig
from bluetooth_autoconnect.daemon import AutoConnectDaemon
from bluetooth_autoconnect.dbus_client import DEVICE_IFACE
from bluetooth_autoconnect.exceptions import HookError
from bluetooth_autoconnect.hooks import (
    _MAX_OUTPUT_BYTES,
    HookEvent,
    HookRunner,
    _build_env,
    _decode_output,
    _log_output,
    build_hook_runner,
    validate_hook_paths,
)
from bluetooth_autoconnect.models import Adapter, Device

# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def device() -> Device:
    """A paired, trusted, disconnected device."""
    return Device(
        path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        address="AA:BB:CC:DD:EE:FF",
        name="JBL Speaker",
        adapter_path="/org/bluez/hci0",
        paired=True,
        trusted=True,
        connected=False,
    )


@pytest.fixture
def exec_script(tmp_path: Path) -> str:
    """Create a minimal executable shell script; return its absolute path."""
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def nonexec_script(tmp_path: Path) -> str:
    """Create a non-executable file; return its absolute path."""
    script = tmp_path / "nope.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)
    return str(script)


# ─────────────────────────────────────────────────────────────────────────────
# HookEvent
# ─────────────────────────────────────────────────────────────────────────────


class TestHookEvent:
    def test_values(self) -> None:
        assert HookEvent.CONNECTED.value == "connected"
        assert HookEvent.DISCONNECTED.value == "disconnected"

    def test_is_str_enum(self) -> None:
        assert isinstance(HookEvent.CONNECTED, str)
        assert HookEvent.CONNECTED == "connected"


# ─────────────────────────────────────────────────────────────────────────────
# validate_hook_paths
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateHookPaths:
    def test_valid_path_passes(self, exec_script: str) -> None:
        result = validate_hook_paths([exec_script])
        assert result == [exec_script]

    def test_relative_path_rejected(self, tmp_path: Path) -> None:
        """Relative paths must be rejected with a warning."""
        result = validate_hook_paths(["relative/path.sh"])
        assert result == []

    def test_nonexistent_path_rejected(self) -> None:
        result = validate_hook_paths(["/nonexistent/hook.sh"])
        assert result == []

    def test_nonexecutable_file_rejected(self, nonexec_script: str) -> None:
        result = validate_hook_paths([nonexec_script])
        assert result == []

    def test_mixed_paths_returns_only_valid(
        self, exec_script: str, tmp_path: Path
    ) -> None:
        result = validate_hook_paths(
            [
                exec_script,
                "not/absolute",
                "/does/not/exist.sh",
            ]
        )
        assert result == [exec_script]

    def test_empty_list_returns_empty(self) -> None:
        assert validate_hook_paths([]) == []

    def test_custom_label_in_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="bluetooth_autoconnect.hooks"):
            validate_hook_paths(["relative.sh"], label="on_connect hook")
        assert "on_connect hook" in caplog.text

    def test_preserves_order(self, tmp_path: Path) -> None:
        scripts = []
        for i in range(3):
            s = tmp_path / f"hook{i}.sh"
            s.write_text("#!/bin/sh\n")
            s.chmod(0o755)
            scripts.append(str(s))
        assert validate_hook_paths(scripts) == scripts


# ─────────────────────────────────────────────────────────────────────────────
# _build_env
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildEnv:
    def test_bt_variables_present(self, device: Device) -> None:
        env = _build_env(HookEvent.CONNECTED, device)
        assert env["BT_EVENT"] == "connected"
        assert env["BT_DEVICE_MAC"] == "AA:BB:CC:DD:EE:FF"
        assert env["BT_DEVICE_NAME"] == "JBL Speaker"
        assert env["BT_DEVICE_PATH"] == "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
        assert env["BT_ADAPTER_PATH"] == "/org/bluez/hci0"

    def test_disconnect_event_value(self, device: Device) -> None:
        env = _build_env(HookEvent.DISCONNECTED, device)
        assert env["BT_EVENT"] == "disconnected"

    def test_inherits_process_environment(self, device: Device) -> None:
        """BT_* vars are injected on top of the existing environment."""
        env = _build_env(HookEvent.CONNECTED, device)
        # At minimum the PATH variable from the process env must be present.
        assert "PATH" in env

    def test_does_not_mutate_os_environ(self, device: Device) -> None:
        before = set(os.environ.keys())
        _build_env(HookEvent.CONNECTED, device)
        after = set(os.environ.keys())
        assert before == after


# ─────────────────────────────────────────────────────────────────────────────
# _decode_output
# ─────────────────────────────────────────────────────────────────────────────


class TestDecodeOutput:
    def test_normal_utf8(self) -> None:
        text, truncated = _decode_output(b"hello world\n")
        assert text == "hello world"
        assert truncated is False

    def test_empty_bytes(self) -> None:
        text, truncated = _decode_output(b"")
        assert text == ""
        assert truncated is False

    def test_trailing_whitespace_stripped(self) -> None:
        text, _ = _decode_output(b"output   \n\n")
        assert text == "output"

    def test_truncated_when_over_limit(self) -> None:
        big = b"x" * (_MAX_OUTPUT_BYTES + 1)
        text, truncated = _decode_output(big)
        assert truncated is True
        assert len(text) <= _MAX_OUTPUT_BYTES

    def test_exact_limit_not_truncated(self) -> None:
        exact = b"a" * _MAX_OUTPUT_BYTES
        _, truncated = _decode_output(exact)
        assert truncated is False

    def test_invalid_utf8_replaced(self) -> None:
        bad = b"\xff\xfe invalid utf-8"
        text, _ = _decode_output(bad)
        assert isinstance(text, str)  # did not raise


# ─────────────────────────────────────────────────────────────────────────────
# _log_output
# ─────────────────────────────────────────────────────────────────────────────


class TestLogOutput:
    def test_exit_zero_logs_at_debug(
        self, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG", logger="bluetooth_autoconnect.hooks"):
            _log_output(
                script="/hook.sh",
                event=HookEvent.CONNECTED,
                device=device,
                returncode=0,
                stdout_bytes=b"ok\n",
                stderr_bytes=b"",
            )
        records = [r for r in caplog.records if "exit=0" in r.getMessage()]
        assert records
        assert records[0].levelname == "DEBUG"

    def test_nonzero_exit_logs_at_warning(
        self, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="bluetooth_autoconnect.hooks"):
            _log_output(
                script="/hook.sh",
                event=HookEvent.CONNECTED,
                device=device,
                returncode=1,
                stdout_bytes=b"",
                stderr_bytes=b"something went wrong\n",
            )
        assert any("exit=1" in r.getMessage() for r in caplog.records)

    def test_truncated_stdout_noted_in_log(
        self, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        big = b"a" * (_MAX_OUTPUT_BYTES + 1)
        with caplog.at_level("DEBUG", logger="bluetooth_autoconnect.hooks"):
            _log_output(
                script="/hook.sh",
                event=HookEvent.CONNECTED,
                device=device,
                returncode=0,
                stdout_bytes=big,
                stderr_bytes=b"",
            )
        assert any("[truncated]" in r.getMessage() for r in caplog.records)

    def test_empty_output_produces_no_output_log(
        self, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG", logger="bluetooth_autoconnect.hooks"):
            _log_output(
                script="/hook.sh",
                event=HookEvent.CONNECTED,
                device=device,
                returncode=0,
                stdout_bytes=b"",
                stderr_bytes=b"",
            )
        # The exit-code log line is present; but no stdout/stderr lines.
        assert not any(
            "hook stdout" in r.getMessage() or "hook stderr" in r.getMessage()
            for r in caplog.records
        )


# ─────────────────────────────────────────────────────────────────────────────
# build_hook_runner
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildHookRunner:
    def test_returns_none_when_no_valid_paths(self) -> None:
        runner = build_hook_runner(
            on_connect=["relative.sh"],
            on_disconnect=["/nonexistent.sh"],
        )
        assert runner is None

    def test_returns_runner_with_valid_connect_script(
        self, exec_script: str
    ) -> None:
        runner = build_hook_runner(on_connect=[exec_script], on_disconnect=[])
        assert runner is not None
        assert runner.on_connect == [exec_script]
        assert runner.on_disconnect == []

    def test_returns_runner_with_valid_disconnect_script(
        self, exec_script: str
    ) -> None:
        runner = build_hook_runner(on_connect=[], on_disconnect=[exec_script])
        assert runner is not None
        assert runner.on_disconnect == [exec_script]

    def test_custom_timeout_passed_through(self, exec_script: str) -> None:
        runner = build_hook_runner(
            on_connect=[exec_script],
            on_disconnect=[],
            timeout_seconds=60.0,
        )
        assert runner is not None
        assert runner.timeout_seconds == 60.0

    def test_invalid_paths_filtered_out(
        self, exec_script: str
    ) -> None:
        runner = build_hook_runner(
            on_connect=[exec_script, "not/absolute", "/no/exist.sh"],
            on_disconnect=[],
        )
        assert runner is not None
        assert runner.on_connect == [exec_script]

    def test_both_empty_lists_returns_none(self) -> None:
        assert build_hook_runner(on_connect=[], on_disconnect=[]) is None


# ─────────────────────────────────────────────────────────────────────────────
# HookRunner.fire — dispatch logic
# ─────────────────────────────────────────────────────────────────────────────


class TestHookRunnerFire:
    def test_fire_connect_schedules_task(self, device: Device) -> None:
        runner = HookRunner(on_connect=["/a.sh"], on_disconnect=[])

        async def _runner() -> None:
            with patch.object(
                runner,
                "_run_one",
                new_callable=AsyncMock,
            ) as mock_run:
                runner.fire(HookEvent.CONNECTED, device)
                await asyncio.sleep(0)
            # _run_one must have been called for /a.sh
            assert mock_run.call_count == 1
            kw = mock_run.call_args.kwargs
            assert kw["script"] == "/a.sh"
            assert kw["event"] == HookEvent.CONNECTED

        asyncio.run(_runner())

    def test_fire_disconnect_schedules_task(self, device: Device) -> None:
        runner = HookRunner(on_connect=[], on_disconnect=["/b.sh"])

        async def _runner() -> None:
            with patch.object(
                runner, "_run_one", new_callable=AsyncMock
            ) as mock_run:
                runner.fire(HookEvent.DISCONNECTED, device)
                await asyncio.sleep(0)
            assert mock_run.call_count == 1
            assert mock_run.call_args.kwargs["event"] == HookEvent.DISCONNECTED

        asyncio.run(_runner())

    def test_fire_no_scripts_does_nothing(self, device: Device) -> None:
        """fire() with no scripts must not schedule any task."""
        runner = HookRunner(on_connect=[], on_disconnect=[])

        async def _runner() -> None:
            with patch.object(
                runner, "_run_one", new_callable=AsyncMock
            ) as mock_run:
                runner.fire(HookEvent.CONNECTED, device)
                await asyncio.sleep(0)
            assert mock_run.call_count == 0

        asyncio.run(_runner())

    def test_fire_multiple_scripts_schedules_all(self, device: Device) -> None:
        runner = HookRunner(on_connect=["/a.sh", "/b.sh", "/c.sh"], on_disconnect=[])

        async def _runner() -> None:
            with patch.object(
                runner, "_run_one", new_callable=AsyncMock
            ) as mock_run:
                runner.fire(HookEvent.CONNECTED, device)
                await asyncio.sleep(0)
            assert mock_run.call_count == 3

        asyncio.run(_runner())

    def test_fire_env_contains_bt_variables(self, device: Device) -> None:
        runner = HookRunner(on_connect=["/hook.sh"], on_disconnect=[])
        captured_env: list[dict] = []

        async def fake_run_one(**kwargs) -> None:  # type: ignore[misc]
            captured_env.append(kwargs["env"])

        async def _runner() -> None:
            with patch.object(runner, "_run_one", side_effect=fake_run_one):
                runner.fire(HookEvent.CONNECTED, device)
                await asyncio.sleep(0)

        asyncio.run(_runner())
        assert captured_env
        assert captured_env[0]["BT_DEVICE_MAC"] == "AA:BB:CC:DD:EE:FF"
        assert captured_env[0]["BT_EVENT"] == "connected"


# ─────────────────────────────────────────────────────────────────────────────
# HookRunner._run_one — subprocess integration
# ─────────────────────────────────────────────────────────────────────────────


class TestHookRunnerRunOne:
    """These tests exercise _run_one with real or mocked subprocesses."""

    def _make_runner(self, timeout: float = 30.0) -> HookRunner:
        return HookRunner(timeout_seconds=timeout)

    def _env(self, device: Device) -> dict[str, str]:
        return _build_env(HookEvent.CONNECTED, device)

    # ── Real subprocess — success path ────────────────────────────────────

    def test_successful_script_exit_zero(
        self, exec_script: str, device: Device
    ) -> None:
        """A script that exits 0 is logged at DEBUG level, no exception."""
        runner = self._make_runner()

        async def _runner() -> None:
            await runner._run_one(
                script=exec_script,
                env=self._env(device),
                event=HookEvent.CONNECTED,
                device=device,
            )

        asyncio.run(_runner())  # must not raise

    def test_nonzero_exit_logged_as_warning(
        self,
        tmp_path: Path,
        device: Device,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fail_script = tmp_path / "fail.sh"
        fail_script.write_text("#!/bin/sh\nexit 1\n")
        fail_script.chmod(0o755)
        runner = self._make_runner()

        async def _runner() -> None:
            with caplog.at_level("WARNING", logger="bluetooth_autoconnect.hooks"):
                await runner._run_one(
                    script=str(fail_script),
                    env=self._env(device),
                    event=HookEvent.CONNECTED,
                    device=device,
                )

        asyncio.run(_runner())
        assert any("exit=1" in r.getMessage() for r in caplog.records)

    def test_stdout_captured(
        self, tmp_path: Path, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        script = tmp_path / "print.sh"
        script.write_text("#!/bin/sh\necho 'hook ran successfully'\n")
        script.chmod(0o755)
        runner = self._make_runner()

        async def _runner() -> None:
            with caplog.at_level("DEBUG", logger="bluetooth_autoconnect.hooks"):
                await runner._run_one(
                    script=str(script),
                    env=self._env(device),
                    event=HookEvent.CONNECTED,
                    device=device,
                )

        asyncio.run(_runner())
        assert any("hook ran successfully" in r.getMessage() for r in caplog.records)

    def test_stderr_captured(
        self, tmp_path: Path, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        script = tmp_path / "err.sh"
        script.write_text("#!/bin/sh\necho 'error output' >&2\nexit 1\n")
        script.chmod(0o755)
        runner = self._make_runner()

        async def _runner() -> None:
            with caplog.at_level("WARNING", logger="bluetooth_autoconnect.hooks"):
                await runner._run_one(
                    script=str(script),
                    env=self._env(device),
                    event=HookEvent.CONNECTED,
                    device=device,
                )

        asyncio.run(_runner())
        assert any("error output" in r.getMessage() for r in caplog.records)

    # ── Launch failure ────────────────────────────────────────────────────

    def test_launch_failure_logged_not_raised(
        self, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A script that cannot be launched (OSError) must not propagate."""
        runner = self._make_runner()

        async def _runner() -> None:
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=OSError("no such file"),
            ):
                with caplog.at_level("ERROR", logger="bluetooth_autoconnect.hooks"):
                    await runner._run_one(
                        script="/nonexistent/hook.sh",
                        env=self._env(device),
                        event=HookEvent.CONNECTED,
                        device=device,
                    )

        asyncio.run(_runner())
        assert any(
            "failed to launch" in r.getMessage() for r in caplog.records
        )

    def test_permission_error_logged_not_raised(
        self, device: Device
    ) -> None:
        runner = self._make_runner()

        async def _runner() -> None:
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=PermissionError("denied"),
            ):
                await runner._run_one(
                    script="/secret/hook.sh",
                    env=self._env(device),
                    event=HookEvent.CONNECTED,
                    device=device,
                )

        asyncio.run(_runner())  # must not raise

    # ── Timeout path ──────────────────────────────────────────────────────

    def test_timeout_kills_process(
        self,
        tmp_path: Path,
        device: Device,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A script that runs too long is killed and a warning is logged."""
        # Write a script that sleeps longer than our 0.1 s timeout.
        slow_script = tmp_path / "slow.sh"
        slow_script.write_text("#!/bin/sh\nsleep 60\n")
        slow_script.chmod(0o755)
        runner = self._make_runner(timeout=0.1)

        async def _runner() -> None:
            with caplog.at_level("WARNING", logger="bluetooth_autoconnect.hooks"):
                await runner._run_one(
                    script=str(slow_script),
                    env=self._env(device),
                    event=HookEvent.CONNECTED,
                    device=device,
                )

        asyncio.run(_runner())
        assert any("timed out" in r.getMessage() for r in caplog.records)

    def test_no_timeout_when_zero(
        self, exec_script: str, device: Device
    ) -> None:
        """timeout_seconds=0 means no timeout — script runs to completion."""
        runner = self._make_runner(timeout=0)

        async def _runner() -> None:
            await runner._run_one(
                script=exec_script,
                env=self._env(device),
                event=HookEvent.CONNECTED,
                device=device,
            )

        asyncio.run(_runner())  # must not raise

    # ── Unexpected communicate() error ────────────────────────────────────

    def test_communicate_exception_swallowed(
        self, device: Device, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unexpected error from proc.communicate() must be caught."""
        runner = self._make_runner()

        fake_proc = MagicMock()
        fake_proc.communicate = AsyncMock(side_effect=RuntimeError("pipe broken"))
        fake_proc.kill = MagicMock()
        fake_proc.wait = AsyncMock()

        async def _runner() -> None:
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=fake_proc,
            ):
                with caplog.at_level("ERROR", logger="bluetooth_autoconnect.hooks"):
                    await runner._run_one(
                        script="/hook.sh",
                        env=self._env(device),
                        event=HookEvent.CONNECTED,
                        device=device,
                    )

        asyncio.run(_runner())
        assert any("unexpected error" in r.getMessage() for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# HooksConfig
# ─────────────────────────────────────────────────────────────────────────────


class TestHooksConfig:
    def test_default_values(self) -> None:
        cfg = HooksConfig()
        assert cfg.on_connect == []
        assert cfg.on_disconnect == []
        assert cfg.timeout_seconds == 30.0

    def test_to_dict(self) -> None:
        cfg = HooksConfig(
            on_connect=["/a.sh"],
            on_disconnect=["/b.sh"],
            timeout_seconds=15.0,
        )
        d = cfg.to_dict()
        assert d["on_connect"] == ["/a.sh"]
        assert d["on_disconnect"] == ["/b.sh"]
        assert d["timeout_seconds"] == 15.0

    def test_to_dict_returns_copies(self) -> None:
        cfg = HooksConfig(on_connect=["/a.sh"], on_disconnect=[])
        d = cfg.to_dict()
        d["on_connect"].append("/mutated.sh")
        assert cfg.on_connect == ["/a.sh"]


class TestAutoConnectConfigHooks:
    def test_hooks_defaults_to_empty(self) -> None:
        cfg = AutoConnectConfig()
        assert isinstance(cfg.hooks, HooksConfig)
        assert cfg.hooks.on_connect == []

    def test_dict_coerced_to_hooks_config(self) -> None:
        cfg = AutoConnectConfig(
            hooks={"on_connect": ["/a.sh"], "timeout_seconds": 10.0}
        )
        assert isinstance(cfg.hooks, HooksConfig)
        assert cfg.hooks.on_connect == ["/a.sh"]
        assert cfg.hooks.timeout_seconds == 10.0

    def test_to_dict_includes_hooks(self) -> None:
        cfg = AutoConnectConfig(
            hooks=HooksConfig(on_connect=["/a.sh"], on_disconnect=[])
        )
        d = cfg.to_dict()
        assert "hooks" in d
        assert d["hooks"]["on_connect"] == ["/a.sh"]


# ─────────────────────────────────────────────────────────────────────────────
# HookError
# ─────────────────────────────────────────────────────────────────────────────


class TestHookError:
    def test_attributes(self) -> None:
        err = HookError(script="/bad.sh", reason="not executable")
        assert err.script == "/bad.sh"
        assert err.reason == "not executable"
        assert "/bad.sh" in str(err)
        assert "not executable" in str(err)

    def test_is_bluetooth_autoconnect_error(self) -> None:
        from bluetooth_autoconnect.exceptions import BluetoothAutoConnectError

        assert isinstance(HookError("/x.sh", "y"), BluetoothAutoConnectError)


# ─────────────────────────────────────────────────────────────────────────────
# Daemon hook wiring — run_once fires on_connect
# ─────────────────────────────────────────────────────────────────────────────


class TestDaemonRunOnceHookWiring:
    """Verify daemon.run_once() calls hook_runner.fire() on successful connect."""

    def _make_device(self, connected: bool = False) -> Device:
        return Device(
            path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            address="AA:BB:CC:DD:EE:FF",
            name="Headset",
            adapter_path="/org/bluez/hci0",
            paired=True,
            trusted=True,
            connected=connected,
        )

    _ADAPTER = Adapter(
        path="/org/bluez/hci0",
        name="hci0",
        address="00:11:22:33:44:55",
        powered=True,
    )

    def test_on_connect_hook_fired_after_successful_connect(self) -> None:
        fired: list[tuple[HookEvent, str]] = []

        runner = HookRunner(on_connect=["/hook.sh"], on_disconnect=[])
        runner.fire = lambda event, dev: fired.append((event, dev.address))  # type: ignore[method-assign]

        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=runner)
        device = self._make_device(connected=False)

        async def run() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[self._ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock()  # success
            from bluetooth_autoconnect.connector import RetryPolicy
            daemon.policy = RetryPolicy(max_attempts=1)
            await daemon.run_once()

        asyncio.run(run())
        assert (HookEvent.CONNECTED, "AA:BB:CC:DD:EE:FF") in fired

    def test_on_connect_hook_not_fired_on_failure(self) -> None:
        fired: list = []

        runner = HookRunner(on_connect=["/hook.sh"], on_disconnect=[])
        runner.fire = lambda event, dev: fired.append(event)  # type: ignore[method-assign]

        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=runner)
        device = self._make_device(connected=False)

        async def run() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[self._ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock(
                side_effect=OSError("page-timeout")
            )
            from bluetooth_autoconnect.connector import RetryPolicy
            daemon.policy = RetryPolicy(max_attempts=1)
            await daemon.run_once()

        asyncio.run(run())
        assert HookEvent.CONNECTED not in fired

    def test_no_hook_runner_does_not_crash(self) -> None:
        """Daemon with hook_runner=None must complete without error."""
        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=None)
        device = self._make_device(connected=False)

        async def run() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[self._ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock()
            from bluetooth_autoconnect.connector import RetryPolicy
            daemon.policy = RetryPolicy(max_attempts=1)
            await daemon.run_once()

        asyncio.run(run())  # must not raise

    def test_already_connected_device_does_not_fire_hook(self) -> None:
        fired: list = []

        runner = HookRunner(on_connect=["/hook.sh"], on_disconnect=[])
        runner.fire = lambda event, dev: fired.append(event)  # type: ignore[method-assign]

        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=runner)
        device = self._make_device(connected=True)

        async def run() -> None:
            daemon.client.get_adapters = AsyncMock(return_value=[self._ADAPTER])
            daemon.client.get_devices = AsyncMock(return_value=[device])
            daemon.client.connect_device = AsyncMock()
            await daemon.run_once()

        asyncio.run(run())
        # An already-connected device is reported as success but connect_device
        # is never called; the hook should not be fired for it.
        assert HookEvent.CONNECTED not in fired


# ─────────────────────────────────────────────────────────────────────────────
# Daemon hook wiring — _fire_disconnect_hook
# ─────────────────────────────────────────────────────────────────────────────


class TestFireDisconnectHook:
    def _make_daemon(self) -> tuple[AutoConnectDaemon, list]:
        fired: list[tuple[HookEvent, str]] = []
        runner = HookRunner(on_connect=[], on_disconnect=["/hook.sh"])
        runner.fire = lambda event, dev: fired.append((event, dev.address))  # type: ignore[method-assign]
        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=runner)
        return daemon, fired

    def test_fires_with_real_device_from_bluez(self) -> None:
        daemon, fired = self._make_daemon()
        real_device = Device(
            path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            address="AA:BB:CC:DD:EE:FF",
            name="JBL Speaker",
            adapter_path="/org/bluez/hci0",
            paired=True,
            trusted=True,
            connected=False,
        )
        daemon.client.get_devices = AsyncMock(return_value=[real_device])

        asyncio.run(
            daemon._fire_disconnect_hook("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")
        )
        assert (HookEvent.DISCONNECTED, "AA:BB:CC:DD:EE:FF") in fired

    def test_falls_back_to_synthetic_device_when_bluez_query_fails(self) -> None:
        daemon, fired = self._make_daemon()
        daemon.client.get_devices = AsyncMock(
            side_effect=RuntimeError("dbus error")
        )

        asyncio.run(
            daemon._fire_disconnect_hook("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF")
        )
        assert len(fired) == 1
        assert fired[0][0] == HookEvent.DISCONNECTED
        assert fired[0][1] == "AA:BB:CC:DD:EE:FF"

    def test_falls_back_to_synthetic_when_device_not_in_results(self) -> None:
        daemon, fired = self._make_daemon()
        # BlueZ returns an empty list — device already removed
        daemon.client.get_devices = AsyncMock(return_value=[])

        asyncio.run(
            daemon._fire_disconnect_hook("/org/bluez/hci0/dev_BB_CC_DD_EE_FF_00")
        )
        assert len(fired) == 1
        assert fired[0][0] == HookEvent.DISCONNECTED

    def test_synthetic_device_mac_parsed_from_path(self) -> None:
        daemon, fired = self._make_daemon()
        daemon.client.get_devices = AsyncMock(return_value=[])

        asyncio.run(
            daemon._fire_disconnect_hook("/org/bluez/hci0/dev_11_22_33_44_55_66")
        )
        assert fired[0][1] == "11:22:33:44:55:66"


# ─────────────────────────────────────────────────────────────────────────────
# Daemon _on_dbus_event fires disconnect hook
# ─────────────────────────────────────────────────────────────────────────────


class TestDaemonDbusEventDisconnectHook:
    def test_connected_false_calls_fire_disconnect_hook(self) -> None:
        fired: list = []
        runner = HookRunner(on_connect=[], on_disconnect=["/hook.sh"])
        runner.fire = lambda event, dev: fired.append(event)  # type: ignore[method-assign]
        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=runner)

        # _fire_disconnect_hook makes an async BlueZ call; mock it out.
        daemon._fire_disconnect_hook = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(
            daemon._on_dbus_event(
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Connected": False},
            )
        )

        daemon._fire_disconnect_hook.assert_awaited_once_with(
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
        )

    def test_connected_false_without_hook_runner_does_not_call_helper(self) -> None:
        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=None)
        daemon._fire_disconnect_hook = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(
            daemon._on_dbus_event(
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Connected": False},
            )
        )

        daemon._fire_disconnect_hook.assert_not_awaited()

    def test_rescan_event_still_set_after_hook_fires(self) -> None:
        runner = HookRunner(on_connect=[], on_disconnect=["/hook.sh"])
        runner.fire = MagicMock()
        daemon = AutoConnectDaemon(rescan_interval=0, hook_runner=runner)
        daemon._fire_disconnect_hook = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(
            daemon._on_dbus_event(
                "properties_changed",
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
                DEVICE_IFACE,
                {"Connected": False},
            )
        )

        # The rescan event must still be set so reconnect is attempted.
        assert daemon._rescan_event.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# CLI config loading
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadConfig:
    """Tests for cli._load_config."""

    def test_absent_file_returns_empty_dict(self, tmp_path: Path) -> None:
        from bluetooth_autoconnect.cli import _load_config

        result = _load_config(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_valid_yaml_parsed(self, tmp_path: Path) -> None:
        from bluetooth_autoconnect.cli import _load_config

        cfg = tmp_path / "config.yaml"
        cfg.write_text("hooks:\n  on_connect:\n    - /a.sh\n")
        result = _load_config(cfg)
        assert result["hooks"]["on_connect"] == ["/a.sh"]

    def test_invalid_yaml_returns_empty_dict(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from bluetooth_autoconnect.cli import _load_config

        bad = tmp_path / "bad.yaml"
        bad.write_text("key: [unclosed\n")
        with caplog.at_level("WARNING"):
            result = _load_config(bad)
        assert result == {}
        assert any("Failed to parse" in r.getMessage() for r in caplog.records)

    def test_non_mapping_yaml_returns_empty_dict(
        self, tmp_path: Path
    ) -> None:
        from bluetooth_autoconnect.cli import _load_config

        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        result = _load_config(f)
        assert result == {}

    def test_default_path_used_when_none_given(self) -> None:
        from bluetooth_autoconnect.cli import _load_config

        # Just verify it does not crash when the default path doesn't exist.
        result = _load_config(None)
        assert isinstance(result, dict)


class TestBuildHookRunnerFromConfig:
    """Tests for cli._build_hook_runner_from_config."""

    def test_no_hooks_key_returns_none(self) -> None:
        from bluetooth_autoconnect.cli import _build_hook_runner_from_config

        assert _build_hook_runner_from_config({}) is None

    def test_empty_hooks_key_returns_none(self) -> None:
        from bluetooth_autoconnect.cli import _build_hook_runner_from_config

        assert _build_hook_runner_from_config({"hooks": {}}) is None

    def test_valid_hooks_returns_runner(self, exec_script: str) -> None:
        from bluetooth_autoconnect.cli import _build_hook_runner_from_config

        raw = {"hooks": {"on_connect": [exec_script], "timeout_seconds": 15.0}}
        runner = _build_hook_runner_from_config(raw)
        assert runner is not None
        assert runner.on_connect == [exec_script]
        assert runner.timeout_seconds == 15.0

    def test_all_invalid_paths_returns_none(self) -> None:
        from bluetooth_autoconnect.cli import _build_hook_runner_from_config

        raw = {
            "hooks": {
                "on_connect": ["/nonexistent.sh"],
                "on_disconnect": [],
            }
        }
        assert _build_hook_runner_from_config(raw) is None

    def test_non_dict_hooks_value_returns_none(self) -> None:
        from bluetooth_autoconnect.cli import _build_hook_runner_from_config

        assert _build_hook_runner_from_config({"hooks": "not a dict"}) is None


# ─────────────────────────────────────────────────────────────────────────────
# CLI --config flag parsing
# ─────────────────────────────────────────────────────────────────────────────


class TestCLIConfigFlag:
    def test_config_flag_parsed(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args(["--config", "/tmp/my.yaml"])
        assert args.config == Path("/tmp/my.yaml")

    def test_config_flag_defaults_to_none(self) -> None:
        from bluetooth_autoconnect.cli import build_parser

        args = build_parser().parse_args([])
        assert args.config is None

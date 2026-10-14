"""Hook execution engine for bluetooth-autoconnect.

Hooks are user-supplied executable scripts (or any executable file) that are
run asynchronously when a Bluetooth device connects or disconnects.  Hook
failures are always logged and swallowed — they must never crash the daemon.

Configuration example (``/etc/bluetooth-autoconnect/config.yaml``)::

    hooks:
      timeout_seconds: 30          # per-hook wall-clock timeout (0 = no limit)
      on_connect:
        - /usr/local/bin/bt-connected.sh
        - /home/alice/scripts/notify.sh
      on_disconnect:
        - /usr/local/bin/bt-disconnected.sh

Environment variables passed to every hook script
--------------------------------------------------
``BT_EVENT``
    ``"connected"`` or ``"disconnected"``
``BT_DEVICE_MAC``
    MAC address of the device, e.g. ``"AA:BB:CC:DD:EE:FF"``
``BT_DEVICE_NAME``
    Human-readable name reported by BlueZ, e.g. ``"JBL Speaker"``
``BT_DEVICE_PATH``
    D-Bus object path, e.g. ``"/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"``
``BT_ADAPTER_PATH``
    D-Bus object path of the owning adapter, e.g. ``"/org/bluez/hci0"``
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from .models import Device

logger = logging.getLogger("bluetooth_autoconnect.hooks")

# Maximum bytes captured from each of stdout / stderr.
# Prevents a chatty hook from filling the system journal.
_MAX_OUTPUT_BYTES: int = 65_536  # 64 KiB

# Default timeout applied when the user does not specify one.
_DEFAULT_TIMEOUT_SECONDS: float = 30.0


# ── Event type ────────────────────────────────────────────────────────────────


class HookEvent(str, Enum):
    """The Bluetooth event that triggered hook execution."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


# ── Validation helpers ────────────────────────────────────────────────────────


def _is_executable(path: str) -> bool:
    """Return True if *path* refers to an existing executable file."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & 0o111)


def validate_hook_paths(
    paths: Sequence[str],
    *,
    label: str = "hook",
) -> list[str]:
    """Validate a list of hook paths; return only the valid ones.

    Validation rules:
    - Must be an absolute path.
    - Must exist as a regular file.
    - Must have at least one executable bit set.

    Invalid entries are logged as warnings and excluded from the result.
    This means a mis-configured hook degrades gracefully rather than
    preventing the daemon from starting.

    Args:
        paths:  Sequence of file-system paths to validate.
        label:  Human-readable label used in warning messages.

    Returns:
        List of paths that passed validation, in the same order they appeared.
    """
    valid: list[str] = []
    for path in paths:
        if not os.path.isabs(path):
            logger.warning(
                "%s path is not absolute and will be skipped: %r", label, path
            )
            continue
        if not os.path.exists(path):
            logger.warning(
                "%s path does not exist and will be skipped: %r", label, path
            )
            continue
        if not _is_executable(path):
            logger.warning(
                "%s path is not executable and will be skipped: %r", label, path
            )
            continue
        valid.append(path)
    return valid


# ── HookRunner ────────────────────────────────────────────────────────────────


@dataclass
class HookRunner:
    """Asynchronous, fault-tolerant hook executor.

    Fires configured scripts after a device connects or disconnects.  Each
    script runs in a separate subprocess and receives device context through
    environment variables (see module docstring).

    Args:
        on_connect:        Validated paths of scripts to run on connect.
        on_disconnect:     Validated paths of scripts to run on disconnect.
        timeout_seconds:   Maximum seconds a single hook may run before it is
                           killed.  ``0`` or negative means no limit.
    """

    on_connect: list[str] = field(default_factory=list)
    on_disconnect: list[str] = field(default_factory=list)
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    # ── public interface ──────────────────────────────────────────────────

    def fire(self, event: HookEvent, device: Device) -> None:
        """Schedule hook execution as a fire-and-forget background task.

        Calling code does **not** await this method.  The hooks run
        concurrently in the background; any failure is logged, not raised.

        Args:
            event:   The triggering event (connected or disconnected).
            device:  The Device whose state changed.
        """
        scripts = (
            self.on_connect if event is HookEvent.CONNECTED else self.on_disconnect
        )
        if not scripts:
            return

        env = _build_env(event, device)
        for script in scripts:
            asyncio.get_event_loop().create_task(
                self._run_one(script=script, env=env, event=event, device=device),
                name=f"hook:{event.value}:{device.address}:{os.path.basename(script)}",
            )

    # ── internals ─────────────────────────────────────────────────────────

    async def _run_one(
        self,
        *,
        script: str,
        env: dict[str, str],
        event: HookEvent,
        device: Device,
    ) -> None:
        """Execute a single hook script; swallow any error.

        Args:
            script: Absolute path to the executable.
            env:    Environment mapping to pass to the subprocess.
            event:  The triggering event (used for log messages).
            device: The Device whose state changed.
        """
        logger.debug(
            "hook: running script=%r event=%s mac=%s",
            script,
            event.value,
            device.address,
        )

        timeout: float | None = (
            self.timeout_seconds if self.timeout_seconds > 0 else None
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, PermissionError) as exc:
            logger.error(
                "hook: failed to launch script=%r event=%s mac=%s: %s",
                script,
                event.value,
                device.address,
                exc,
            )
            return

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "hook: timed out after %.0fs — killing script=%r event=%s mac=%s",
                self.timeout_seconds,
                script,
                event.value,
                device.address,
            )
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass  # process already exited between timeout and kill
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hook: unexpected error waiting for script=%r event=%s mac=%s: %s",
                script,
                event.value,
                device.address,
                exc,
            )
            return

        returncode = proc.returncode if proc.returncode is not None else -1
        _log_output(
            script=script,
            event=event,
            device=device,
            returncode=returncode,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_env(event: HookEvent, device: Device) -> dict[str, str]:
    """Build the subprocess environment for a hook invocation.

    Inherits the daemon's own environment and adds ``BT_*`` variables so
    hook scripts can act on the specific device that triggered them.

    Args:
        event:   The triggering event.
        device:  The Device whose state changed.

    Returns:
        A complete environment dict (inherits os.environ).
    """
    env = os.environ.copy()
    env.update(
        {
            "BT_EVENT": event.value,
            "BT_DEVICE_MAC": device.address,
            "BT_DEVICE_NAME": device.name,
            "BT_DEVICE_PATH": device.path,
            "BT_ADAPTER_PATH": device.adapter_path,
        }
    )
    return env


def _decode_output(raw: bytes) -> tuple[str, bool]:
    """Decode subprocess output, truncating if it exceeds the size limit.

    Args:
        raw: Raw bytes from stdout or stderr.

    Returns:
        A 2-tuple of (decoded_string, was_truncated).
    """
    truncated = len(raw) > _MAX_OUTPUT_BYTES
    chunk = raw[:_MAX_OUTPUT_BYTES]
    text = chunk.decode("utf-8", errors="replace").rstrip()
    return text, truncated


def _log_output(
    *,
    script: str,
    event: HookEvent,
    device: Device,
    returncode: int,
    stdout_bytes: bytes,
    stderr_bytes: bytes,
) -> None:
    """Log the result and captured output of a finished hook.

    Uses DEBUG for successful (exit 0) hooks so normal operation is quiet,
    and WARNING for non-zero exit codes so problems are visible at INFO level.

    Args:
        script:        Absolute path to the hook script.
        event:         The triggering event.
        device:        The Device whose state changed.
        returncode:    Process exit code.
        stdout_bytes:  Raw stdout from the subprocess.
        stderr_bytes:  Raw stderr from the subprocess.
    """
    level = logging.DEBUG if returncode == 0 else logging.WARNING
    logger.log(
        level,
        "hook: script=%r event=%s mac=%s exit=%d",
        script,
        event.value,
        device.address,
        returncode,
    )

    stdout, stdout_truncated = _decode_output(stdout_bytes)
    stderr, stderr_truncated = _decode_output(stderr_bytes)

    if stdout:
        logger.debug(
            "hook stdout (script=%r):%s\n%s",
            script,
            " [truncated]" if stdout_truncated else "",
            stdout,
        )
    if stderr:
        log_fn = logger.warning if returncode != 0 else logger.debug
        log_fn(
            "hook stderr (script=%r):%s\n%s",
            script,
            " [truncated]" if stderr_truncated else "",
            stderr,
        )


# ── Factory ───────────────────────────────────────────────────────────────────


def build_hook_runner(
    on_connect: Sequence[str],
    on_disconnect: Sequence[str],
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> HookRunner | None:
    """Validate hook paths and build a :class:`HookRunner`.

    Invalid paths are warned about and dropped.  Returns ``None`` when both
    lists are empty after validation, so callers can skip hook machinery
    entirely for users who have not configured any hooks.

    Args:
        on_connect:       Raw paths for ``on_connect`` hooks (from config).
        on_disconnect:    Raw paths for ``on_disconnect`` hooks (from config).
        timeout_seconds:  Per-hook timeout.

    Returns:
        A ready :class:`HookRunner`, or ``None`` if no valid hooks remain.
    """
    valid_connect = validate_hook_paths(list(on_connect), label="on_connect hook")
    valid_disconnect = validate_hook_paths(
        list(on_disconnect), label="on_disconnect hook"
    )

    if not valid_connect and not valid_disconnect:
        return None

    return HookRunner(
        on_connect=valid_connect,
        on_disconnect=valid_disconnect,
        timeout_seconds=timeout_seconds,
    )

"""Device connection orchestration: retries, backoff, and concurrency."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .exceptions import DeviceAlreadyConnectedError, DeviceConnectionError
from .models import Device

logger = logging.getLogger("bluetooth_autoconnect.connector")
ConnectFn = Callable[[str], Awaitable[None]]

# ── BlueZ error strings ───────────────────────────────────────────────────────
# These are the D-Bus error names BlueZ emits.  We classify them here so
# that connector.py remains agnostic to the D-Bus library (dbus-next surfaces
# them as plain exception message strings or as dbus_next.errors.DBusError).

# A successful outcome masquerading as an error — the device is already up.
_BLUEZ_ALREADY_CONNECTED = "org.bluez.Error.AlreadyConnected"

# Transient: another Connect() call is in flight; retry with a short delay.
_BLUEZ_IN_PROGRESS = "org.bluez.Error.InProgress"

# Transient: adapter or device stack not ready yet; retry.
_BLUEZ_NOT_READY = "org.bluez.Error.NotReady"

# Hard errors — no point retrying the same device immediately.
_BLUEZ_DOES_NOT_EXIST = "org.bluez.Error.DoesNotExist"
_BLUEZ_AUTH_CANCELLED = "org.bluez.Error.AuthenticationCanceled"
_BLUEZ_AUTH_FAILED = "org.bluez.Error.AuthenticationFailed"
_BLUEZ_AUTH_REJECTED = "org.bluez.Error.AuthenticationRejected"
_BLUEZ_AUTH_TIMEOUT = "org.bluez.Error.AuthenticationTimeout"

# Page-timeout class — device is genuinely out of range; normal backoff.
_BLUEZ_PAGE_TIMEOUT = "org.bluez.Error.Failed"  # BlueZ wraps page-timeout here

# ── Helpers ───────────────────────────────────────────────────────────────────

_TRANSIENT_ERRORS: frozenset[str] = frozenset(
    {_BLUEZ_IN_PROGRESS, _BLUEZ_NOT_READY}
)

_PERMANENT_ERRORS: frozenset[str] = frozenset(
    {
        _BLUEZ_DOES_NOT_EXIST,
        _BLUEZ_AUTH_CANCELLED,
        _BLUEZ_AUTH_FAILED,
        _BLUEZ_AUTH_REJECTED,
        _BLUEZ_AUTH_TIMEOUT,
    }
)


def _bluez_error_name(exc: Exception) -> str | None:
    """Extract the BlueZ D-Bus error name from an exception, if present.

    dbus-next raises ``dbus_next.errors.DBusError`` whose ``type`` attribute
    holds the fully-qualified error name (e.g. ``org.bluez.Error.InProgress``).
    We also check ``str(exc)`` so that mocked or wrapped exceptions that carry
    the error name as part of their message are handled correctly in tests.

    Returns the error name string, or ``None`` if it cannot be determined.
    """
    # dbus-next native: DBusError has a .type attribute
    error_type = getattr(exc, "type", None)
    if error_type is not None:
        return str(error_type)

    # Fallback: check if the error name appears in the string representation.
    # This covers mocked exceptions and wrapped errors in unit tests.
    msg = str(exc)
    for candidate in (
        _BLUEZ_ALREADY_CONNECTED,
        _BLUEZ_IN_PROGRESS,
        _BLUEZ_NOT_READY,
        _BLUEZ_DOES_NOT_EXIST,
        _BLUEZ_AUTH_CANCELLED,
        _BLUEZ_AUTH_FAILED,
        _BLUEZ_AUTH_REJECTED,
        _BLUEZ_AUTH_TIMEOUT,
    ):
        if candidate in msg:
            return candidate

    return None


# ── Public API ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        delay = self.base_delay * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay)


async def connect_with_retry(
    device: Device,
    connect_fn: ConnectFn,
    policy: RetryPolicy | None = None,
) -> bool:
    """Attempt to connect *device*, retrying on transient errors.

    BlueZ error handling
    --------------------
    ``AlreadyConnected``
        Treated as immediate success — the device is connected.  Backoff
        is reset by the caller.  No retry is performed.

    ``InProgress`` / ``NotReady``
        Transient.  Retried after a short delay (same exponential schedule
        as ordinary failures).

    ``DoesNotExist`` / ``Authentication*``
        Permanent.  No retry; raises :class:`DeviceConnectionError`
        immediately so the caller can put the device into backoff.

    All other errors
        Retried up to *max_attempts* times.  If all attempts fail,
        :class:`DeviceConnectionError` is raised.
    """
    policy = policy or RetryPolicy()

    for attempt in range(1, policy.max_attempts + 1):
        logger.debug(
            "attempting connection: name=%r mac=%s path=%s attempt=%d/%d",
            device.name,
            device.address,
            device.path,
            attempt,
            policy.max_attempts,
        )
        try:
            await connect_fn(device.path)
            logger.info(
                "connection succeeded: mac=%s name=%r attempt=%d",
                device.address,
                device.name,
                attempt,
            )
            return True

        except Exception as exc:  # noqa: BLE001
            error_name = _bluez_error_name(exc)

            # ── Already connected ─────────────────────────────────────────
            if error_name == _BLUEZ_ALREADY_CONNECTED:
                logger.info(
                    "connection: mac=%s name=%r is already connected"
                    " — treating as success",
                    device.address,
                    device.name,
                )
                raise DeviceAlreadyConnectedError(device.address) from exc

            # ── Permanent errors — give up immediately ─────────────────────
            if error_name in _PERMANENT_ERRORS:
                logger.warning(
                    "connection failed (permanent error): mac=%s name=%r"
                    " error=%s — not retrying",
                    device.address,
                    device.name,
                    error_name or exc,
                )
                raise DeviceConnectionError(device.address, str(exc)) from exc

            # ── Final attempt exhausted ────────────────────────────────────
            if attempt >= policy.max_attempts:
                logger.warning(
                    "connection failed: mac=%s name=%r"
                    " attempt=%d/%d error=%s — giving up",
                    device.address,
                    device.name,
                    attempt,
                    policy.max_attempts,
                    exc,
                )
                raise DeviceConnectionError(device.address, str(exc)) from exc

            # ── Transient errors ───────────────────────────────────────────
            delay = policy.delay_for_attempt(attempt)
            if error_name in _TRANSIENT_ERRORS:
                logger.debug(
                    "connection transient error: mac=%s attempt=%d/%d"
                    " error=%s — retry in %.1fs",
                    device.address,
                    attempt,
                    policy.max_attempts,
                    error_name,
                    delay,
                )
            else:
                logger.debug(
                    "connection failed: mac=%s name=%r attempt=%d/%d"
                    " error=%s — retry in %.1fs",
                    device.address,
                    device.name,
                    attempt,
                    policy.max_attempts,
                    exc,
                    delay,
                )
            await asyncio.sleep(delay)

    return False  # pragma: no cover


async def connect_all(
    devices: list[Device],
    connect_fn: ConnectFn,
    policy: RetryPolicy | None = None,
    max_concurrency: int = 5,
) -> dict[str, bool]:
    """Attempt to connect all eligible *devices* concurrently.

    Returns a dict mapping ``device.address → success``.

    ``AlreadyConnected`` is treated as success so the caller resets backoff
    rather than advancing it.
    """
    semaphore = asyncio.Semaphore(max_concurrency)
    results: dict[str, bool] = {}

    async def _run(device: Device) -> None:
        async with semaphore:
            try:
                success = await connect_with_retry(device, connect_fn, policy)
                results[device.address] = success
            except DeviceAlreadyConnectedError:
                # Device is up — count as success so backoff is reset.
                logger.debug(
                    "connect_all: mac=%s already connected — marking success",
                    device.address,
                )
                results[device.address] = True
            except DeviceConnectionError:
                results[device.address] = False

    eligible = [d for d in devices if d.is_autoconnect_eligible]
    skipped = [d for d in devices if not d.is_autoconnect_eligible]
    for device in skipped:
        logger.debug(
            "skipping %s: Paired=%s Trusted=%s Connected=%s",
            device.address,
            device.paired,
            device.trusted,
            device.connected,
        )

    already_connected = [d for d in eligible if d.connected]
    to_connect = [d for d in eligible if not d.connected]

    for device in already_connected:
        logger.debug(
            "mac=%s name=%r is already connected — skipping",
            device.address,
            device.name,
        )
        results[device.address] = True

    if not to_connect:
        return results

    await asyncio.gather(*(_run(device) for device in to_connect))
    return results

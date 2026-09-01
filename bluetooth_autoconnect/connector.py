"""Device connection orchestration: retries, backoff, and concurrency.

This module is intentionally decoupled from the D-Bus transport: it takes
a small callable (``connect_fn``) rather than a :class:`BlueZClient`
directly, so the retry/backoff/concurrency logic can be unit tested with
a fake connect function and no real D-Bus bus.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .exceptions import DeviceConnectionError
from .models import Device

logger = logging.getLogger("bluetooth_autoconnect.connector")

# A connect function takes a device path and returns None on success or
# raises an exception on failure.
ConnectFn = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff configuration for connection attempts.

    Attributes:
        max_attempts: Maximum number of attempts per connection request
            (including the first attempt).
        base_delay: Delay, in seconds, before the first retry.
        max_delay: Upper bound on the backoff delay, in seconds.
        multiplier: Factor the delay is multiplied by after each attempt.
    """

    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        """Return the backoff delay before the given attempt number.

        Args:
            attempt: 1-indexed attempt number that just failed (i.e. the
                delay returned is how long to wait before attempt + 1).
        """
        delay = self.base_delay * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay)


async def connect_with_retry(
    device: Device,
    connect_fn: ConnectFn,
    policy: RetryPolicy | None = None,
) -> bool:
    """Attempt to connect a single device, retrying with backoff.

    Args:
        device: The device to connect. Only used for logging/identity;
            not mutated.
        connect_fn: Async callable that performs the actual D-Bus
            ``Connect()`` call, given the device's D-Bus object path.
        policy: Retry policy to use. Defaults to a sensible policy of
            5 attempts, starting at 1s and doubling up to 60s.

    Returns:
        True if the device connected successfully, False if all retry
        attempts were exhausted.
    """
    policy = policy or RetryPolicy()

    for attempt in range(1, policy.max_attempts + 1):
        try:
            await connect_fn(device.path)
            logger.info("Connected %s on attempt %d", device, attempt)
            return True
        except Exception as exc:  # noqa: BLE001 - any D-Bus/BlueZ failure
            if attempt >= policy.max_attempts:
                logger.warning(
                    "Giving up on %s after %d attempts: %s",
                    device,
                    attempt,
                    exc,
                )
                raise DeviceConnectionError(device.address, str(exc)) from exc

            delay = policy.delay_for_attempt(attempt)
            logger.debug(
                "Attempt %d/%d failed for %s (%s); retrying in %.1fs",
                attempt,
                policy.max_attempts,
                device,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    return False  # pragma: no cover - unreachable, loop always returns/raises


async def connect_all(
    devices: list[Device],
    connect_fn: ConnectFn,
    policy: RetryPolicy | None = None,
    max_concurrency: int = 5,
) -> dict[str, bool]:
    """Connect multiple devices concurrently, each with its own retries.

    Devices that are already connected, or that are not both paired and
    trusted, are skipped up front and reported as such in the returned
    mapping (as ``True`` for already-connected, ``False`` is never used
    for skipped-ineligible devices -- they are simply omitted).

    Args:
        devices: Candidate devices, typically from
            :meth:`BlueZClient.get_devices`.
        connect_fn: Async callable performing the actual connect.
        policy: Retry policy shared by all devices.
        max_concurrency: Maximum number of simultaneous connection
            attempts, to avoid hammering the adapter.

    Returns:
        A mapping of device address -> success (True/False) for every
        device that was actually attempted (eligible and not already
        connected).
    """
    semaphore = asyncio.Semaphore(max_concurrency)
    results: dict[str, bool] = {}

    async def _run(device: Device) -> None:
        async with semaphore:
            try:
                success = await connect_with_retry(device, connect_fn, policy)
                results[device.address] = success
            except DeviceConnectionError:
                results[device.address] = False

    eligible = [d for d in devices if d.is_autoconnect_eligible]
    skipped = [d for d in devices if not d.is_autoconnect_eligible]
    for device in skipped:
        logger.debug(
            "Skipping %s (paired=%s, trusted=%s)",
            device,
            device.paired,
            device.trusted,
        )

    already_connected = [d for d in eligible if d.connected]
    to_connect = [d for d in eligible if not d.connected]
    for device in already_connected:
        logger.debug("%s is already connected", device)
        results[device.address] = True

    if not to_connect:
        return results

    await asyncio.gather(*(_run(device) for device in to_connect))
    return results

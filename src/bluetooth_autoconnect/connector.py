"""Device connection orchestration: retries, backoff, and concurrency."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .exceptions import DeviceConnectionError
from .models import Device

logger = logging.getLogger("bluetooth_autoconnect.connector")
ConnectFn = Callable[[str], Awaitable[None]]


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
    policy = policy or RetryPolicy()

    for attempt in range(1, policy.max_attempts + 1):
        try:
            await connect_fn(device.path)
            logger.info("Connected %s on attempt %d", device, attempt)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt >= policy.max_attempts:
                logger.warning(
                    "Giving up on %s after %d attempts: %s",
                    device, attempt, exc,
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

    return False  # pragma: no cover


async def connect_all(
    devices: list[Device],
    connect_fn: ConnectFn,
    policy: RetryPolicy | None = None,
    max_concurrency: int = 5,
) -> dict[str, bool]:
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
            device, device.paired, device.trusted,
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

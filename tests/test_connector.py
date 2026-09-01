"""Unit tests for bluetooth_autoconnect.connector.

These tests use fake async connect functions instead of a real D-Bus
connection, and patch out ``asyncio.sleep`` so backoff delays don't slow
down the test suite.
"""

from __future__ import annotations

import pytest

from bluetooth_autoconnect import connector as connector_module
from bluetooth_autoconnect.connector import (
    RetryPolicy,
    connect_all,
    connect_with_retry,
)
from bluetooth_autoconnect.exceptions import DeviceConnectionError
from bluetooth_autoconnect.models import Device


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent real delays during backoff so tests run fast."""

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(connector_module.asyncio, "sleep", _fake_sleep)


class TestRetryPolicy:
    def test_delay_grows_exponentially(self) -> None:
        policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=100.0)
        assert policy.delay_for_attempt(1) == 1.0
        assert policy.delay_for_attempt(2) == 2.0
        assert policy.delay_for_attempt(3) == 4.0
        assert policy.delay_for_attempt(4) == 8.0

    def test_delay_is_capped_at_max_delay(self) -> None:
        policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=5.0)
        assert policy.delay_for_attempt(10) == 5.0


class TestConnectWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self, trusted_device: Device) -> None:
        calls: list[str] = []

        async def connect_fn(path: str) -> None:
            calls.append(path)

        result = await connect_with_retry(trusted_device, connect_fn)
        assert result is True
        assert calls == [trusted_device.path]

    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failures(
        self, trusted_device: Device
    ) -> None:
        attempts = {"count": 0}

        async def connect_fn(path: str) -> None:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("org.bluez.Error.Failed: br-connection-busy")

        policy = RetryPolicy(max_attempts=5, base_delay=0.01)
        result = await connect_with_retry(trusted_device, connect_fn, policy)
        assert result is True
        assert attempts["count"] == 3

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_retries(
        self, trusted_device: Device
    ) -> None:
        async def connect_fn(path: str) -> None:
            raise RuntimeError("org.bluez.Error.NotAvailable")

        policy = RetryPolicy(max_attempts=3, base_delay=0.01)
        with pytest.raises(DeviceConnectionError) as exc_info:
            await connect_with_retry(trusted_device, connect_fn, policy)

        assert exc_info.value.device_address == trusted_device.address


class TestConnectAll:
    @pytest.mark.asyncio
    async def test_skips_untrusted_and_unpaired(
        self,
        trusted_device: Device,
        untrusted_device: Device,
        unpaired_device: Device,
    ) -> None:
        attempted: list[str] = []

        async def connect_fn(path: str) -> None:
            attempted.append(path)

        results = await connect_all(
            [trusted_device, untrusted_device, unpaired_device],
            connect_fn,
        )

        assert attempted == [trusted_device.path]
        assert results == {trusted_device.address: True}

    @pytest.mark.asyncio
    async def test_skips_already_connected(
        self, already_connected_device: Device
    ) -> None:
        attempted: list[str] = []

        async def connect_fn(path: str) -> None:
            attempted.append(path)

        results = await connect_all([already_connected_device], connect_fn)

        assert attempted == []  # never actually called Connect()
        assert results == {already_connected_device.address: True}

    @pytest.mark.asyncio
    async def test_connects_multiple_devices_concurrently(self) -> None:
        devices = [
            Device(
                path=f"/org/bluez/hci0/dev_{i}",
                address=f"00:00:00:00:00:0{i}",
                name=f"Device {i}",
                adapter_path="/org/bluez/hci0",
                paired=True,
                trusted=True,
                connected=False,
            )
            for i in range(4)
        ]

        async def connect_fn(path: str) -> None:
            return None

        results = await connect_all(devices, connect_fn, max_concurrency=2)
        assert all(results.values())
        assert len(results) == 4

    @pytest.mark.asyncio
    async def test_one_device_failing_does_not_block_others(self) -> None:
        good = Device(
            path="/org/bluez/hci0/dev_good",
            address="AA:AA:AA:AA:AA:AA",
            name="Good Device",
            adapter_path="/org/bluez/hci0",
            paired=True,
            trusted=True,
            connected=False,
        )
        bad = Device(
            path="/org/bluez/hci0/dev_bad",
            address="BB:BB:BB:BB:BB:BB",
            name="Bad Device",
            adapter_path="/org/bluez/hci0",
            paired=True,
            trusted=True,
            connected=False,
        )

        async def connect_fn(path: str) -> None:
            if path == bad.path:
                raise RuntimeError("org.bluez.Error.Failed")

        policy = RetryPolicy(max_attempts=2, base_delay=0.01)
        results = await connect_all([good, bad], connect_fn, policy=policy)

        assert results[good.address] is True
        assert results[bad.address] is False

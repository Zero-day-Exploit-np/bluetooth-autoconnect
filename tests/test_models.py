"""Unit tests for bluetooth_autoconnect.models."""

from __future__ import annotations

from bluetooth_autoconnect.models import Adapter, Device


class TestDeviceEligibility:
    def test_paired_and_trusted_is_eligible(self, trusted_device: Device) -> None:
        assert trusted_device.is_autoconnect_eligible is True

    def test_untrusted_is_not_eligible(self, untrusted_device: Device) -> None:
        assert untrusted_device.is_autoconnect_eligible is False

    def test_unpaired_is_not_eligible(self, unpaired_device: Device) -> None:
        assert unpaired_device.is_autoconnect_eligible is False

    def test_paired_not_trusted_not_eligible(self) -> None:
        device = Device(
            path="/org/bluez/hci0/dev_00_00_00_00_00_01",
            address="00:00:00:00:00:01",
            name="Half Paired",
            adapter_path="/org/bluez/hci0",
            paired=True,
            trusted=False,
            connected=False,
        )
        assert device.is_autoconnect_eligible is False

    def test_trusted_not_paired_not_eligible(self) -> None:
        device = Device(
            path="/org/bluez/hci0/dev_00_00_00_00_00_02",
            address="00:00:00:00:00:02",
            name="Weird State",
            adapter_path="/org/bluez/hci0",
            paired=False,
            trusted=True,
            connected=False,
        )
        assert device.is_autoconnect_eligible is False


class TestDeviceStr:
    def test_str_includes_name_and_address(self, trusted_device: Device) -> None:
        text = str(trusted_device)
        assert "Test Headphones" in text
        assert "AA:BB:CC:DD:EE:FF" in text


class TestAdapter:
    def test_adapter_is_frozen(self) -> None:
        adapter = Adapter(
            path="/org/bluez/hci0",
            name="hci0",
            address="00:11:22:33:44:55",
            powered=True,
        )
        assert adapter.powered is True
        try:
            adapter.powered = False  # type: ignore[misc]
            assert False, "Adapter should be immutable"  # noqa: B011
        except AttributeError:
            pass

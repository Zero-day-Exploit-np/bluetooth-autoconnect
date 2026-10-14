"""Shared pytest fixtures for bluetooth-autoconnect tests."""

from __future__ import annotations

import pytest

from bluetooth_autoconnect.models import Device


@pytest.fixture
def trusted_device() -> Device:
    """A paired, trusted, currently-disconnected device."""
    return Device(
        path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        address="AA:BB:CC:DD:EE:FF",
        name="Test Headphones",
        adapter_path="/org/bluez/hci0",
        paired=True,
        trusted=True,
        connected=False,
    )


@pytest.fixture
def untrusted_device() -> Device:
    """A paired but not-trusted device, which should never be autoconnected."""
    return Device(
        path="/org/bluez/hci0/dev_11_22_33_44_55_66",
        address="11:22:33:44:55:66",
        name="Untrusted Gadget",
        adapter_path="/org/bluez/hci0",
        paired=True,
        trusted=False,
        connected=False,
    )


@pytest.fixture
def unpaired_device() -> Device:
    """A device that has never been paired; must be skipped."""
    return Device(
        path="/org/bluez/hci0/dev_77_88_99_AA_BB_CC",
        address="77:88:99:AA:BB:CC",
        name="Nearby Stranger",
        adapter_path="/org/bluez/hci0",
        paired=False,
        trusted=False,
        connected=False,
    )


@pytest.fixture
def already_connected_device() -> Device:
    """A device that is already connected and should be skipped/no-op."""
    return Device(
        path="/org/bluez/hci0/dev_DE_AD_BE_EF_00_01",
        address="DE:AD:BE:EF:00:01",
        name="Test Mouse",
        adapter_path="/org/bluez/hci0",
        paired=True,
        trusted=True,
        connected=True,
    )

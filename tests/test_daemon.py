"""Unit tests for bluetooth_autoconnect.daemon event handling.

These tests exercise ``AutoConnectDaemon._on_dbus_event`` directly with
synthetic events, so they don't require a real D-Bus connection.
"""

from __future__ import annotations

import pytest

from bluetooth_autoconnect.daemon import ADAPTER_IFACE, DEVICE_IFACE, AutoConnectDaemon


@pytest.fixture
def daemon() -> AutoConnectDaemon:
    return AutoConnectDaemon()


class TestOnDBusEvent:
    @pytest.mark.asyncio
    async def test_adapter_powered_on_triggers_rescan(
        self, daemon: AutoConnectDaemon
    ) -> None:
        assert not daemon._rescan_event.is_set()
        await daemon._on_dbus_event(
            "properties_changed", "/org/bluez/hci0", ADAPTER_IFACE, {"Powered": True}
        )
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_adapter_powered_off_does_not_trigger_rescan(
        self, daemon: AutoConnectDaemon
    ) -> None:
        await daemon._on_dbus_event(
            "properties_changed", "/org/bluez/hci0", ADAPTER_IFACE, {"Powered": False}
        )
        assert not daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_device_disconnected_triggers_rescan(
        self, daemon: AutoConnectDaemon
    ) -> None:
        await daemon._on_dbus_event(
            "properties_changed",
            "/org/bluez/hci0/dev_AA_BB",
            DEVICE_IFACE,
            {"Connected": False},
        )
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_device_connected_true_does_not_trigger_rescan(
        self, daemon: AutoConnectDaemon
    ) -> None:
        await daemon._on_dbus_event(
            "properties_changed",
            "/org/bluez/hci0/dev_AA_BB",
            DEVICE_IFACE,
            {"Connected": True},
        )
        assert not daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_new_device_added_triggers_rescan(
        self, daemon: AutoConnectDaemon
    ) -> None:
        await daemon._on_dbus_event(
            "added", "/org/bluez/hci0/dev_CC_DD", DEVICE_IFACE, {}
        )
        assert daemon._rescan_event.is_set()

    @pytest.mark.asyncio
    async def test_unrelated_interface_ignored(
        self, daemon: AutoConnectDaemon
    ) -> None:
        await daemon._on_dbus_event(
            "properties_changed",
            "/org/bluez/hci0/dev_AA_BB",
            "org.bluez.Battery1",
            {"Percentage": 42},
        )
        assert not daemon._rescan_event.is_set()

"""Custom exception hierarchy for bluetooth-autoconnect."""

from __future__ import annotations


class BluetoothAutoConnectError(Exception):
    """Base class for all errors raised by bluetooth-autoconnect."""


class DBusConnectionError(BluetoothAutoConnectError):
    """Raised when the D-Bus system bus cannot be reached or introspected."""


class BlueZNotAvailableError(BluetoothAutoConnectError):
    """Raised when the ``org.bluez`` service is not present on the bus."""


class AdapterNotFoundError(BluetoothAutoConnectError):
    """Raised when no Bluetooth adapters are found on the system."""


class DeviceConnectionError(BluetoothAutoConnectError):
    """Raised when a specific device fails to connect after all retries."""

    def __init__(self, device_address: str, reason: str) -> None:
        self.device_address = device_address
        self.reason = reason
        super().__init__(f"Failed to connect {device_address}: {reason}")

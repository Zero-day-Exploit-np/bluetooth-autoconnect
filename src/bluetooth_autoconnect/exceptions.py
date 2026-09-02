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


class HookError(BluetoothAutoConnectError):
    """Raised when a hook script cannot be launched or configured correctly.

    Note: runtime failures of hook subprocesses (non-zero exit, timeout,
    etc.) are logged and swallowed by :class:`~bluetooth_autoconnect.hooks.HookRunner`
    and do **not** raise this exception.  ``HookError`` is reserved for
    structural problems discovered at config-load time, such as a hook path
    that is not absolute or not executable.
    """

    def __init__(self, script: str, reason: str) -> None:
        self.script = script
        self.reason = reason
        super().__init__(f"Hook configuration error for {script!r}: {reason}")

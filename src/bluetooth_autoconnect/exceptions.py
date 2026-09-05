"""Custom exception hierarchy for bluetooth-autoconnect."""

from __future__ import annotations


class BluetoothAutoConnectError(Exception):
    """Base class for all errors raised by bluetooth-autoconnect."""


# ── Backend exceptions ────────────────────────────────────────────────────────


class BackendError(BluetoothAutoConnectError):
    """Base class for platform backend errors.

    Raised when a backend operation fails in a way that is not specific to
    D-Bus or a particular OS API.
    """


class BackendNotAvailableError(BackendError):
    """The requested backend cannot be used on the current platform.

    Examples
    --------
    * ``LinuxBackend`` on Windows (no D-Bus SYSTEM bus)
    * ``WindowsBackend`` on Linux (no WinRT runtime)
    """


class BackendUnsupportedError(BackendError):
    """The operation requested is not supported by the active backend.

    Raised when a backend is asked to perform an operation it explicitly
    does not implement (e.g. ``set_adapter_powered`` on Windows, where the
    radio state is managed by the OS).
    """


# ── D-Bus / Linux-specific exceptions ────────────────────────────────────────


class DBusConnectionError(BackendError):
    """Raised when the D-Bus system bus cannot be reached or introspected.

    Sub-class of :class:`BackendError` so callers that catch
    ``BackendError`` automatically cover this case.
    """


class BlueZNotAvailableError(BackendError):
    """Raised when the ``org.bluez`` service is not present on the bus."""


# ── Device-level exceptions ───────────────────────────────────────────────────


class AdapterNotFoundError(BluetoothAutoConnectError):
    """Raised when no Bluetooth adapters are found on the system."""


class DeviceConnectionError(BluetoothAutoConnectError):
    """Raised when a specific device fails to connect after all retries."""

    def __init__(self, device_address: str, reason: str) -> None:
        self.device_address = device_address
        self.reason = reason
        super().__init__(f"Failed to connect {device_address}: {reason}")


# ── Hook exceptions ───────────────────────────────────────────────────────────


class HookError(BluetoothAutoConnectError):
    """Raised when a hook script cannot be launched or configured correctly.

    Note: runtime failures of hook subprocesses (non-zero exit, timeout,
    etc.) are logged and swallowed by
    :class:`~bluetooth_autoconnect.hooks.HookRunner` and do **not** raise
    this exception.  ``HookError`` is reserved for structural problems
    discovered at config-load time, such as a hook path that is not absolute
    or not executable.
    """

    def __init__(self, script: str, reason: str) -> None:
        self.script = script
        self.reason = reason
        super().__init__(f"Hook configuration error for {script!r}: {reason}")

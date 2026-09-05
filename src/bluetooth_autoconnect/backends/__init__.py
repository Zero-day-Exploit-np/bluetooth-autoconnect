"""Platform-specific Bluetooth backend package.

Architecture
------------
``BluetoothBackend`` is a :class:`typing.Protocol` that defines the
interface every platform implementation must satisfy.  The daemon,
doctor, and CLI never import a concrete backend directly — they receive
one via :func:`create_backend`.

Backend implementations
-----------------------
:class:`~bluetooth_autoconnect.backends.linux.LinuxBackend`
    Uses the BlueZ D-Bus API via ``dbus-next``.  Available on any Linux
    system with BlueZ and a running D-Bus system bus.

:class:`~bluetooth_autoconnect.backends.windows.WindowsBackend`
    Uses the Windows Runtime (WinRT) Bluetooth APIs via the ``winrt``
    Python package.  Available on Windows 10 1809+ / Windows 11.

Factory
-------
:func:`create_backend` detects the current platform and returns the
appropriate backend instance.  The ``backend`` keyword can override
auto-detection for testing or advanced use-cases.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from ..models import Adapter, Device

# Re-export the callback type so callers can import it from one place.
EventCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]

__all__ = [
    "BluetoothBackend",
    "EventCallback",
    "create_backend",
    "get_platform_name",
]


@runtime_checkable
class BluetoothBackend(Protocol):
    """Interface that every platform backend must satisfy.

    All methods are ``async``.  Implementations may raise
    :class:`~bluetooth_autoconnect.exceptions.BackendError` (or any
    subclass) on infrastructure failures.

    The daemon interacts *only* through this interface so that the
    connection logic, cooldown tracking, and hook system are fully
    platform-agnostic.
    """

    async def connect(self) -> None:
        """Initialise the backend and acquire any required system resources.

        Called once at daemon startup before any other method.

        Raises
        ------
        BackendNotAvailableError
            The backend cannot be used on this system.
        BackendError
            Any other initialisation failure.
        """
        ...

    async def close(self) -> None:
        """Release all resources acquired in :meth:`connect`.

        Must be idempotent — calling ``close()`` on an already-closed
        backend must not raise.
        """
        ...

    async def get_adapters(self) -> list[Adapter]:
        """Return all Bluetooth adapters visible to the system.

        Returns an empty list (not an exception) when no adapters are
        present.

        Returns
        -------
        list[Adapter]
            Each entry represents one physical or virtual adapter.
            The ``powered`` field reflects the current radio state.
        """
        ...

    async def get_devices(
        self, adapter_path: str | None = None
    ) -> list[Device]:
        """Return all known (paired/bonded) Bluetooth devices.

        Parameters
        ----------
        adapter_path:
            When given, restrict results to devices belonging to that
            adapter.  The meaning of "path" is backend-specific:
            on Linux it is a D-Bus object path; on Windows it is the
            adapter's Bluetooth address.

        Returns
        -------
        list[Device]
            All known devices, regardless of current connection state.
        """
        ...

    async def connect_device(self, device_path: str) -> None:
        """Initiate a connection to the device identified by *device_path*.

        The path format is backend-specific (D-Bus object path on Linux,
        Bluetooth address on Windows).

        This call returns as soon as the connection attempt is *accepted*
        by the kernel/OS — it does not wait for profile negotiation to
        complete.  The authoritative ``connected`` signal comes from
        :meth:`subscribe`.

        Raises
        ------
        DeviceConnectionError
            The connection attempt was refused or timed out.
        BackendError
            Any other infrastructure failure.
        """
        ...

    async def subscribe(self, callback: EventCallback) -> None:
        """Register *callback* to receive Bluetooth events.

        The callback is invoked with four positional arguments::

            callback(event_type, object_path, interface_name, changed_props)

        ``event_type`` is one of:

        ``"added"``
            A new adapter or device object appeared.
        ``"removed"``
            An adapter or device object was removed.
        ``"properties_changed"``
            One or more properties changed on an existing object.

        ``object_path`` and ``interface_name`` are backend-specific
        identifiers; ``changed_props`` is a plain ``dict[str, Any]``
        of the changed property names and their new values.

        The callback is an ``async`` coroutine.  The backend is
        responsible for scheduling it on the running event loop without
        blocking the event source thread.

        Parameters
        ----------
        callback:
            Async callable with the signature described above.
        """
        ...


def get_platform_name() -> str:
    """Return the canonical lower-case platform name.

    Returns
    -------
    str
        ``"linux"``, ``"windows"``, ``"darwin"``, or the raw
        :data:`sys.platform` value for anything else.
    """
    s = sys.platform.lower()
    if s.startswith("linux"):
        return "linux"
    if s.startswith("win"):
        return "windows"
    if s.startswith("darwin"):
        return "darwin"
    return s


def create_backend(
    *,
    backend: str | None = None,
) -> BluetoothBackend:
    """Instantiate and return the appropriate backend for the current platform.

    Parameters
    ----------
    backend:
        Force a specific backend by name: ``"linux"`` or ``"windows"``.
        ``None`` (the default) selects the backend automatically based
        on :data:`sys.platform`.

    Returns
    -------
    BluetoothBackend
        A newly-constructed, not-yet-connected backend instance.

    Raises
    ------
    BackendNotAvailableError
        The requested or auto-detected backend cannot be used on this
        platform (e.g. requesting ``"linux"`` on Windows).
    BackendUnsupportedError
        An unknown backend name was supplied.

    Examples
    --------
    >>> from bluetooth_autoconnect.backends import create_backend
    >>> backend = create_backend()          # auto-detect
    >>> backend = create_backend(backend="linux")   # explicit
    """
    from ..exceptions import BackendNotAvailableError, BackendUnsupportedError

    chosen = backend.lower() if backend else get_platform_name()

    if chosen == "linux":
        if not sys.platform.startswith("linux"):
            raise BackendNotAvailableError(
                "LinuxBackend requires a Linux system with a running D-Bus "
                f"system bus; current platform is {sys.platform!r}."
            )
        from .linux import LinuxBackend

        return LinuxBackend()

    if chosen == "windows":
        if not sys.platform.startswith("win"):
            raise BackendNotAvailableError(
                "WindowsBackend requires Windows 10 1809+ or Windows 11; "
                f"current platform is {sys.platform!r}."
            )
        try:
            from .windows import WindowsBackend
        except ImportError as exc:
            raise BackendNotAvailableError(
                "WindowsBackend requires the 'winrt' package. "
                "Install it with: pip install bluetooth-autoconnect[windows]"
            ) from exc
        return WindowsBackend()

    if chosen == "darwin":
        raise BackendNotAvailableError(
            "macOS (CoreBluetooth) backend is not yet implemented. "
            "Contributions welcome: https://github.com/Zero-day-Exploit-np/"
            "bluetooth-autoconnect"
        )

    raise BackendUnsupportedError(
        f"Unknown backend {chosen!r}. "
        "Valid options: 'linux', 'windows'."
    )

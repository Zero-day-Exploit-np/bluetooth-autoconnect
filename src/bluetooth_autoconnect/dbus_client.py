"""Backward-compatibility shim for ``bluetooth_autoconnect.dbus_client``.

The BlueZ client implementation has moved to
``bluetooth_autoconnect.backends.linux``.

This module re-exports every public name that the original ``dbus_client``
provided so that existing tests and external code continue to work
unchanged.

.. deprecated::
    Import directly from ``bluetooth_autoconnect.backends.linux`` or use
    :func:`~bluetooth_autoconnect.backends.create_backend` instead.
    This shim will be removed in a future major release.
"""

from __future__ import annotations

# Re-export everything the original dbus_client.py exposed, including the
# names that existing tests monkeypatch (MessageBus, BlueZNotAvailableError).
# Importing via the backend module means there is only one real implementation.
from dbus_next.aio import MessageBus  # noqa: F401  (tests monkeypatch this)

from .backends.linux import (  # noqa: F401  (re-exports)
    ADAPTER_IFACE,
    BLUEZ_SERVICE,
    DBUS_PATH,
    DBUS_SERVICE,
    DEVICE_IFACE,
    OBJECT_MANAGER_IFACE,
    PROPERTIES_IFACE,
    EventCallback,
    _schedule,
    _unwrap,
)
from .backends.linux import (
    LinuxBackend as BlueZClient,
)
from .exceptions import (
    BlueZNotAvailableError,  # noqa: F401  (tests monkeypatch via this module)
)

__all__ = [
    "BlueZClient",
    "BLUEZ_SERVICE",
    "ADAPTER_IFACE",
    "DEVICE_IFACE",
    "OBJECT_MANAGER_IFACE",
    "PROPERTIES_IFACE",
    "DBUS_SERVICE",
    "DBUS_PATH",
    "EventCallback",
    "MessageBus",
    "BlueZNotAvailableError",
    "_unwrap",
    "_schedule",
]

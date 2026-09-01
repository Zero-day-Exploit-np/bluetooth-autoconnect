"""Plain data models representing BlueZ adapters and devices.

These dataclasses are deliberately decoupled from the D-Bus layer so that
they can be constructed, compared, and unit-tested without a running
D-Bus session or a real Bluetooth adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Adapter:
    """A Bluetooth controller/adapter exposed by BlueZ.

    Attributes:
        path: The D-Bus object path, e.g. ``/org/bluez/hci0``.
        name: The adapter's friendly name (``Alias``), e.g. ``hci0``.
        address: The adapter's own MAC address.
        powered: Whether the adapter is currently powered on.
    """

    path: str
    name: str
    address: str
    powered: bool


@dataclass
class Device:
    """A Bluetooth device known to a specific adapter.

    Attributes:
        path: The D-Bus object path, e.g.
            ``/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF``.
        address: The device's MAC address, e.g. ``AA:BB:CC:DD:EE:FF``.
        name: The device's friendly name, falling back to the address
            if BlueZ has not resolved a name yet.
        adapter_path: The D-Bus object path of the owning adapter.
        paired: Whether the device is paired.
        trusted: Whether the device is trusted (required for autoconnect).
        connected: Whether the device is currently connected.
        rssi: Last known signal strength, if the device is in range and
            advertising. ``None`` when unknown.
        retry_count: Number of consecutive failed connection attempts,
            used to drive exponential backoff. Not part of BlueZ state;
            tracked locally by the connector.
    """

    path: str
    address: str
    name: str
    adapter_path: str
    paired: bool
    trusted: bool
    connected: bool
    rssi: int | None = None
    retry_count: int = field(default=0, compare=False)

    @property
    def is_autoconnect_eligible(self) -> bool:
        """Whether this device should ever be auto-connected.

        Only devices that are both paired *and* trusted are eligible.
        BlueZ sets ``Trusted`` when a device is paired through a normal
        interactive pairing flow, but it can also be toggled independently,
        so we check both explicitly rather than assuming one implies
        the other.
        """
        return self.paired and self.trusted

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.name} ({self.address})"

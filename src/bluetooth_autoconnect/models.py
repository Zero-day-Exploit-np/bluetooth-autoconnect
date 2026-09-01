"""Plain data models representing BlueZ adapters and devices."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Adapter:
    path: str
    name: str
    address: str
    powered: bool


@dataclass
class Device:
    path: str
    address: str
    name: str
    adapter_path: str
    paired: bool
    trusted: bool
    connected: bool
    rssi: int | None = None
    retry_count: int = field(default=0, compare=False)
    priority: int = 100

    @property
    def is_autoconnect_eligible(self) -> bool:
        return self.paired and self.trusted

    def __str__(self) -> str:
        return f"{self.name} ({self.address})"

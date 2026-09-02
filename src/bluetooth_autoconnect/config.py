"""Configuration model and rule handling for bluetooth-autoconnect."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetryConfig:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "multiplier": self.multiplier,
        }


@dataclass
class LoggingConfig:
    level: str = "INFO"
    structured: bool = True
    rotate: str = "weekly"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "structured": self.structured,
            "rotate": self.rotate,
        }


@dataclass
class DaemonConfig:
    scan_interval: int = 30  # legacy name kept for compat
    rescan_interval_seconds: int = 30
    adapter: str | None = None
    max_concurrency: int = 5
    enable_automatic_reconnect: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_interval": self.scan_interval,
            "rescan_interval_seconds": self.rescan_interval_seconds,
            "adapter": self.adapter,
            "max_concurrency": self.max_concurrency,
            "enable_automatic_reconnect": self.enable_automatic_reconnect,
        }


@dataclass
class HooksConfig:
    """Configuration for the hook execution system.

    Hooks are user-supplied executable scripts fired when a device connects
    or disconnects.  Each list entry must be an absolute path to an
    executable file; invalid entries are warned about and ignored at runtime.

    Args:
        on_connect:       Paths of scripts to run when a device connects.
        on_disconnect:    Paths of scripts to run when a device disconnects.
        timeout_seconds:  Per-hook wall-clock timeout in seconds.
                          ``0`` or negative means no timeout (use with care).
    """

    on_connect: list[str] = field(default_factory=list)
    on_disconnect: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "on_connect": list(self.on_connect),
            "on_disconnect": list(self.on_disconnect),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class DeviceRule:
    address: str
    priority: int = 100
    enabled: bool = True
    group: str | None = None
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "priority": self.priority,
            "enabled": self.enabled,
            "group": self.group,
            "comment": self.comment,
        }


@dataclass
class DeviceRuleSet:
    rules: list[DeviceRule] = field(default_factory=list)

    def add_rule(self, rule: DeviceRule) -> None:
        self.rules.append(rule)

    def get_priority(self, address: str) -> int:
        for rule in self.rules:
            if rule.address == address:
                return rule.priority
        return 100

    def is_blacklisted(self, address: str) -> bool:
        for rule in self.rules:
            if rule.address == address and not rule.enabled:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [rule.to_dict() for rule in self.rules]}


@dataclass
class AutoConnectConfig:
    """Top-level configuration object for bluetooth-autoconnect.

    All nested config objects accept either a typed dataclass instance or a
    plain ``dict``; they are coerced to the dataclass form in
    ``__post_init__``.  This makes it straightforward to build an
    ``AutoConnectConfig`` directly from a parsed YAML document.
    """

    retry: RetryConfig | dict[str, Any] = field(default_factory=RetryConfig)
    logging: LoggingConfig | dict[str, Any] = field(default_factory=LoggingConfig)
    daemon: DaemonConfig | dict[str, Any] = field(default_factory=DaemonConfig)
    hooks: HooksConfig | dict[str, Any] = field(default_factory=HooksConfig)
    adapter: str | None = None
    device_priorities: dict[str, int] = field(
        default_factory=lambda: {"default": 100}
    )
    device_rules: DeviceRuleSet = field(default_factory=DeviceRuleSet)
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.retry, dict):
            self.retry = RetryConfig(**self.retry)
        if isinstance(self.logging, dict):
            self.logging = LoggingConfig(**self.logging)
        if isinstance(self.daemon, dict):
            self.daemon = DaemonConfig(**self.daemon)
        if isinstance(self.hooks, dict):
            self.hooks = HooksConfig(**self.hooks)

    def to_dict(self) -> dict[str, Any]:
        # After __post_init__ the union fields are always the dataclass type,
        # never plain dicts.  Assert to help mypy narrow the union.
        assert isinstance(self.retry, RetryConfig)
        assert isinstance(self.logging, LoggingConfig)
        assert isinstance(self.daemon, DaemonConfig)
        assert isinstance(self.hooks, HooksConfig)
        return {
            "adapter": self.adapter,
            "retry": self.retry.to_dict(),
            "logging": self.logging.to_dict(),
            "daemon": self.daemon.to_dict(),
            "hooks": self.hooks.to_dict(),
            "device_priorities": self.device_priorities,
            "rules": self.device_rules.to_dict()["rules"],
            "whitelist": self.whitelist,
            "blacklist": self.blacklist,
        }

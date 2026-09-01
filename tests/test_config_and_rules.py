from __future__ import annotations

from bluetooth_autoconnect.config import AutoConnectConfig, DeviceRule, DeviceRuleSet


def test_default_config_has_production_defaults() -> None:
    config = AutoConnectConfig()
    assert config.retry.max_attempts == 5
    assert config.daemon.scan_interval == 30
    assert config.logging.level == "INFO"
    assert config.device_priorities["default"] == 100


def test_rule_set_allows_priority_and_blacklist_logic() -> None:
    rules = DeviceRuleSet()
    rules.add_rule(DeviceRule(address="AA:BB:CC:DD:EE:FF", priority=250, enabled=True))
    rules.add_rule(DeviceRule(address="11:22:33:44:55:66", enabled=False))

    assert rules.get_priority("AA:BB:CC:DD:EE:FF") == 250
    assert rules.is_blacklisted("11:22:33:44:55:66") is True
    assert rules.is_blacklisted("00:11:22:33:44:55") is False


def test_config_can_store_and_load_yaml_like_dict() -> None:
    config = AutoConnectConfig(
        adapter="hci0",
        logging={"level": "DEBUG", "structured": True},
        device_priorities={"AA:BB:CC:DD:EE:FF": 300},
    )

    data = config.to_dict()
    assert data["adapter"] == "hci0"
    assert data["logging"]["level"] == "DEBUG"
    assert data["device_priorities"]["AA:BB:CC:DD:EE:FF"] == 300

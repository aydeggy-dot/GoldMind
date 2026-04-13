"""Configuration loader for GoldMind.

Loads and validates YAML config + credentials from the config/ directory.
config.yaml is the SINGLE SOURCE OF TRUTH for runtime parameters.
credentials.yaml is gitignored and contains MT5/Telegram secrets.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"
DEFAULT_CREDS_PATH = CONFIG_DIR / "credentials.yaml"

REQUIRED_SECTIONS = (
    "strategy_version", "mt5", "gold_specs", "strategy", "sessions",
    "risk", "margin", "sanity_check", "circuit_breakers", "adaptive_sizing",
    "compounding", "regime", "macro", "news", "holidays", "trade_management",
    "data_validation", "broker_monitoring", "telegram", "database", "logging",
    "backtesting", "health", "warm_up",
)

REQUIRED_CRED_SECTIONS = ("mt5", "telegram")


class ConfigError(Exception):
    """Raised when config is missing, malformed, or fails validation."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


def _validate_config(cfg: dict[str, Any]) -> None:
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ConfigError(f"config.yaml missing sections: {missing}")

    risk = cfg["risk"]
    if not (0 < risk["risk_per_trade_pct"] <= risk["max_risk_per_trade_pct"]):
        raise ConfigError("risk.risk_per_trade_pct must be > 0 and <= max_risk_per_trade_pct")
    if risk["min_lot_size"] <= 0 or risk["max_lot_size"] < risk["min_lot_size"]:
        raise ConfigError("risk lot bounds invalid")
    if risk["min_rr_ratio"] <= 0:
        raise ConfigError("risk.min_rr_ratio must be positive")

    sanity = cfg["sanity_check"]
    if sanity["max_lot_hard_ceiling"] <= 0:
        raise ConfigError("sanity_check.max_lot_hard_ceiling must be positive")

    margin = cfg["margin"]
    if not (margin["danger_margin_level"] < margin["warning_margin_level"]
            < margin["min_margin_level"]):
        raise ConfigError("margin levels must satisfy danger < warning < min")

    cb = cfg["circuit_breakers"]
    if cb["max_total_drawdown_pct"] <= cb["max_weekly_drawdown_pct"]:
        raise ConfigError("max_total_drawdown_pct must exceed max_weekly_drawdown_pct")


def _validate_credentials(creds: dict[str, Any]) -> None:
    missing = [s for s in REQUIRED_CRED_SECTIONS if s not in creds]
    if missing:
        raise ConfigError(f"credentials.yaml missing sections: {missing}")
    mt5 = creds["mt5"]
    for k in ("account", "password", "server"):
        if not mt5.get(k):
            raise ConfigError(f"credentials.mt5.{k} is required")
    if not isinstance(mt5["account"], int):
        raise ConfigError("credentials.mt5.account must be an integer")
    tg = creds["telegram"]
    for k in ("bot_token", "chat_id"):
        if not tg.get(k):
            raise ConfigError(f"credentials.telegram.{k} is required")


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate config.yaml."""
    cfg = _read_yaml(Path(path) if path else DEFAULT_CONFIG_PATH)
    _validate_config(cfg)
    return cfg


def load_credentials(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate credentials.yaml."""
    creds = _read_yaml(Path(path) if path else DEFAULT_CREDS_PATH)
    _validate_credentials(creds)
    return creds


def load_all(config_path: Path | str | None = None,
             credentials_path: Path | str | None = None) -> dict[str, Any]:
    """Load both files, return a deep-copied merged view under separate keys."""
    return {
        "config": copy.deepcopy(load_config(config_path)),
        "credentials": copy.deepcopy(load_credentials(credentials_path)),
    }


__all__ = ["load_config", "load_credentials", "load_all", "ConfigError"]

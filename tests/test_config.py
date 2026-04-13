"""Tests for config loading and validation."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config import ConfigError, load_config, load_credentials


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_real_example_config_passes():
    """The committed config.example.yaml must validate cleanly."""
    cfg = load_config(Path("config/config.example.yaml"))
    assert cfg["mt5"]["symbol"] == "XAUUSD"
    assert cfg["risk"]["risk_per_trade_pct"] == 1.0


def test_missing_section_rejected(tmp_path: Path):
    bad = _write(tmp_path, "c.yaml", "mt5:\n  symbol: XAUUSD\n")
    with pytest.raises(ConfigError, match="missing sections"):
        load_config(bad)


def test_invalid_risk_rejected(tmp_path: Path):
    base = Path("config/config.example.yaml").read_text(encoding="utf-8")
    base = base.replace("risk_per_trade_pct: 1.0", "risk_per_trade_pct: 5.0")
    p = tmp_path / "c.yaml"
    p.write_text(base, encoding="utf-8")
    with pytest.raises(ConfigError, match="risk_per_trade_pct"):
        load_config(p)


def test_invalid_margin_levels_rejected(tmp_path: Path):
    base = Path("config/config.example.yaml").read_text(encoding="utf-8")
    base = base.replace("warning_margin_level: 150", "warning_margin_level: 250")
    p = tmp_path / "c.yaml"
    p.write_text(base, encoding="utf-8")
    with pytest.raises(ConfigError, match="margin levels"):
        load_config(p)


def test_credentials_validation(tmp_path: Path):
    good = _write(tmp_path, "creds.yaml", """
        mt5:
          account: 12345
          password: pw
          server: Broker
        telegram:
          bot_token: t
          chat_id: c
    """)
    creds = load_credentials(good)
    assert creds["mt5"]["account"] == 12345

    bad = _write(tmp_path, "bad.yaml", """
        mt5:
          account: not_an_int
          password: pw
          server: Broker
        telegram:
          bot_token: t
          chat_id: c
    """)
    with pytest.raises(ConfigError):
        load_credentials(bad)

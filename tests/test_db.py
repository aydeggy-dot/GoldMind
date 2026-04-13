"""Tests for DBManager — schema init, WAL mode, state persistence, CRUD."""
from __future__ import annotations

from pathlib import Path

import pytest

from database import DBManager


@pytest.fixture()
def db(tmp_path: Path) -> DBManager:
    d = DBManager(tmp_path / "test.db", wal_mode=True)
    yield d
    d.close()


def test_wal_mode_enabled(db: DBManager):
    row = db.fetchone("PRAGMA journal_mode")
    assert row[0].lower() == "wal"


def test_integrity_check(db: DBManager):
    assert db.integrity_check() is True


def test_schema_created(db: DBManager):
    rows = db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {
        "trades", "signals", "daily_reports", "system_state",
        "circuit_breaker_events", "broker_spec_baseline", "withdrawals",
    }
    assert expected.issubset(names)


def test_state_roundtrip(db: DBManager):
    db.set_state("test_key", {"a": 1, "b": [1, 2, 3]})
    assert db.get_state("test_key") == {"a": 1, "b": [1, 2, 3]}
    db.delete_state("test_key")
    assert db.get_state("test_key", default="x") == "x"


def test_state_survives_reopen(tmp_path: Path):
    path = tmp_path / "persist.db"
    d1 = DBManager(path)
    d1.set_state("k", "v")
    d1.close()
    d2 = DBManager(path)
    assert d2.get_state("k") == "v"
    d2.close()


def test_insert_trade_and_signal(db: DBManager):
    tid = db.insert_trade({
        "ticket": 1, "type": "LONG", "setup_type": "SWEEP_REVERSAL",
        "strategy_version": "1.0.0", "entry_price": 2000.0, "stop_loss": 1995.0,
        "take_profit": 2010.0, "requested_lot": 0.01, "filled_lot": 0.01,
    })
    assert tid > 0
    sid = db.insert_signal({
        "type": "TREND_CONTINUATION", "direction": "LONG",
        "entry_price": 2000.0, "stop_loss": 1995.0, "take_profit": 2010.0,
        "confidence": 0.7, "was_traded": 1,
    })
    assert sid > 0


def test_broker_baseline_upsert(db: DBManager):
    specs = {
        "contract_size": 100.0, "margin_initial": 1000.0,
        "margin_maintenance": 500.0, "volume_min": 0.01,
        "volume_max": 100.0, "volume_step": 0.01,
        "swap_long": -5.0, "swap_short": -3.0, "leverage": 500,
    }
    db.upsert_broker_baseline("XAUUSD", specs)
    got = db.get_broker_baseline("XAUUSD")
    assert got["contract_size"] == 100.0
    assert got["leverage"] == 500
    specs["leverage"] = 200
    db.upsert_broker_baseline("XAUUSD", specs)
    assert db.get_broker_baseline("XAUUSD")["leverage"] == 200

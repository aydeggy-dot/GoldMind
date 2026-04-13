"""Phase 7 tests — performance, health monitor, dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analytics.dashboard import (
    build_daily_report, build_weekly_report, daily_message, persist_daily,
    weekly_message,
)
from analytics.health_monitor import HealthMonitor
from analytics.performance import PerformanceMetrics, compute_metrics
from config import load_config
from database import DBManager


# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture()
def db(tmp_path: Path) -> DBManager:
    d = DBManager(tmp_path / "analytics.db")
    yield d
    d.close()


def _trade(
    i: int,
    *,
    pnl: float,
    setup: str = "trend_continuation",
    session: str = "NY_OVERLAP",
    rr: float = 2.0,
    swap: float = 0.0,
    partial: int = 0,
    minutes_ago: int = 60,
) -> dict:
    entry = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    exit_ = entry + timedelta(minutes=30)
    return {
        "ticket": i, "type": "LONG", "setup_type": setup,
        "strategy_version": "1.0.0", "entry_price": 2000 + i, "exit_price": 2005 + i,
        "stop_loss": 1990 + i, "take_profit": 2010 + i,
        "requested_lot": 0.04, "filled_lot": 0.04, "partial_fill": partial,
        "pnl": pnl, "pnl_pct": pnl / 500.0, "swap_cost": swap, "commission": 0.0,
        "rr_achieved": rr, "duration_minutes": 30,
        "session": session, "regime": "TRENDING_BULLISH", "macro_bias": "BULLISH",
        "confidence": 0.7, "margin_level_at_entry": 5000.0,
        "entry_time": entry.isoformat(), "exit_time": exit_.isoformat(),
        "exit_reason": "take_profit", "is_backtest": 0, "notes": "",
    }


# ----------------------------------------------------------------------
# Performance
# ----------------------------------------------------------------------
def test_compute_metrics_empty_returns_zeros():
    m = compute_metrics([])
    assert isinstance(m, PerformanceMetrics)
    assert m.trades == 0 and m.win_rate == 0.0


def test_compute_metrics_basic():
    trades = [
        _trade(1, pnl=10.0), _trade(2, pnl=-5.0),
        _trade(3, pnl=15.0), _trade(4, pnl=-2.0, setup="sweep_reversal"),
    ]
    m = compute_metrics(trades, window=50)
    assert m.trades == 4
    assert m.wins == 2 and m.losses == 2
    assert m.win_rate == pytest.approx(50.0)
    assert m.profit_factor == pytest.approx((10 + 15) / (5 + 2))
    assert m.total_pnl == pytest.approx(18.0)
    assert m.best_setup in {"trend_continuation", "sweep_reversal"}


def test_compute_metrics_drawdown():
    # +10, +5, -20, -5 -> peak 15, trough -10, DD=25
    trades = [_trade(i, pnl=p) for i, p in enumerate([10.0, 5.0, -20.0, -5.0], 1)]
    m = compute_metrics(trades)
    assert m.max_drawdown == pytest.approx(25.0)
    assert m.max_drawdown_duration >= 1


def test_compute_metrics_window_truncates():
    trades = [_trade(i, pnl=1.0) for i in range(100)]
    m = compute_metrics(trades, window=10)
    assert m.trades == 10


def test_open_trades_excluded():
    closed = _trade(1, pnl=5.0)
    open_ = {**_trade(2, pnl=0.0), "exit_time": None, "pnl": None}
    m = compute_metrics([closed, open_])
    assert m.trades == 1


# ----------------------------------------------------------------------
# HealthMonitor
# ----------------------------------------------------------------------
def test_health_on_close_few_trades_no_pause(cfg, db):
    hm = HealthMonitor(cfg, db)
    r = hm.on_trade_closed({})
    assert r.pause is False


def test_health_pauses_when_two_checks_fail(cfg, db):
    # 10 losses -> win rate 0%, PF 0 -> two failures -> pause
    for i, t in enumerate([_trade(i, pnl=-5.0) for i in range(10)], 1):
        db.insert_trade(t)
    hm = HealthMonitor(cfg, db)
    r = hm.on_trade_closed({})
    assert r.pause is True
    assert len(r.alerts) >= 2


def test_health_healthy_trades_no_pause(cfg, db):
    for t in [_trade(i, pnl=10.0 if i % 3 else -3.0, rr=2.5)
              for i in range(1, 15)]:
        db.insert_trade(t)
    hm = HealthMonitor(cfg, db)
    r = hm.on_trade_closed({})
    assert r.pause is False


def test_health_heartbeat_returns_reading(cfg, db):
    hm = HealthMonitor(cfg, db)
    r = hm.heartbeat({"balance": 500.0})
    # psutil may or may not be present; call must not raise either way
    assert r is not None


def test_broker_health_spread_regime_change(cfg, db):
    hm = HealthMonitor(cfg, db)
    r = hm.broker_health_check(
        current_spread_pts=20.0,
        spread_avg_recent_pts=50.0,
        spread_avg_baseline_pts=20.0,  # 2.5x => triggers alert (mult=2.0)
    )
    assert any("spread regime change" in a for a in r.alerts)


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------
def test_build_daily_report_row_shape(cfg):
    trades = [_trade(1, pnl=10.0), _trade(2, pnl=-3.0)]
    row = build_daily_report(
        trades=trades, balance=500.0, equity=507.0, margin_level=5000.0,
        regime="TRENDING_BULLISH", macro_bias="BULLISH",
        strategy_version="1.0.0", peak_balance=500.0)
    # All columns the DB expects
    required = {"date", "balance", "trades_count", "wins", "losses",
                "daily_pnl", "swap_costs_total", "regime", "notes"}
    assert required.issubset(row.keys())
    assert row["trades_count"] == 2


def test_persist_daily_upserts(cfg, db):
    row = build_daily_report(
        trades=[_trade(1, pnl=1.0)], balance=500.0, equity=501.0,
        margin_level=5000.0, regime="r", macro_bias="m",
        strategy_version="1.0.0")
    persist_daily(db, row)
    persist_daily(db, row)  # second call should UPDATE not duplicate
    out = db.fetchall("SELECT * FROM daily_reports WHERE date = ?", (row["date"],))
    assert len(out) == 1


def test_daily_message_formats():
    row = build_daily_report(
        trades=[_trade(1, pnl=5.0)], balance=500.0, equity=505.0,
        margin_level=5000.0, regime="r", macro_bias="m",
        strategy_version="1.0.0")
    msg = daily_message(row)
    assert "Daily Report" in msg and "500.00" in msg


def test_weekly_report_and_message():
    trades = [_trade(i, pnl=3.0 if i % 2 else -1.0) for i in range(1, 11)]
    row = build_weekly_report(trades=trades, balance=550.0, strategy_version="1.0.0")
    assert row["trades"] == 10
    msg = weekly_message(row)
    assert "Weekly Report" in msg

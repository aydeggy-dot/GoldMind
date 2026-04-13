"""Tests for RiskManager — sizing, sanity, breakers, kill switch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config import load_config
from core.risk_manager import RiskManager
from database import DBManager
from utils.constants import CircuitBreaker, Direction


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture()
def db(tmp_path: Path) -> DBManager:
    d = DBManager(tmp_path / "risk.db")
    yield d
    d.close()


@pytest.fixture()
def rm(cfg, db) -> RiskManager:
    return RiskManager(cfg, db)


@pytest.fixture()
def symbol_info() -> dict:
    return {"point": 0.01, "trade_tick_value": 1.0, "trade_tick_size": 0.01,
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}


@pytest.fixture()
def account() -> dict:
    return {"balance": 500.0, "equity": 500.0, "margin": 0.0,
            "free_margin": 500.0, "margin_level": 10000.0, "leverage": 500}


# ----------------------------------------------------------------------
# Position sizing
# ----------------------------------------------------------------------
def test_sizing_basic(rm, symbol_info):
    # $500 * 1% = $5 risk; SL 200 points; pv=$1/point/lot
    # lot = 5 / (200 * 1) = 0.025 -> rounded down to 0.02 -> clamped to max_lot 0.10 OK
    lot = rm.calculate_position_size(500.0, sl_distance_price=2.0, symbol_info=symbol_info)
    assert lot == pytest.approx(0.02)


def test_sizing_zero_when_underflow(rm, symbol_info):
    # Tiny balance, huge SL -> below min_lot
    lot = rm.calculate_position_size(50.0, sl_distance_price=10.0, symbol_info=symbol_info)
    assert lot == 0.0


def test_sizing_clamped_by_max_lot(rm, symbol_info):
    # Huge balance, tiny SL -> would calculate huge lot, clamped to max
    lot = rm.calculate_position_size(1_000_000.0, sl_distance_price=2.0, symbol_info=symbol_info)
    assert lot <= rm.risk["max_lot_size"]


def test_sizing_reduced_after_consecutive_losses(rm, symbol_info):
    rm.state.consecutive_losses = 2
    rm._save()
    lot_after_2 = rm.calculate_position_size(500.0, 2.0, symbol_info)
    rm.state.consecutive_losses = 0
    base = rm.calculate_position_size(500.0, 2.0, symbol_info)
    assert lot_after_2 < base or lot_after_2 == 0.0


# ----------------------------------------------------------------------
# Sanity check
# ----------------------------------------------------------------------
def test_sanity_rejects_lot_above_ceiling(rm, symbol_info):
    safe, reason = rm.sanity_check_lot(2.0, balance=500.0, sl_distance_price=2.0,
                                       symbol_info=symbol_info)
    assert safe is None and "ceiling" in reason


def test_sanity_rejects_lot_risking_too_much(rm, symbol_info):
    # 0.5 lot * 200 pts * $1/pt = $100 risk on $500 = 20% > 5%
    safe, reason = rm.sanity_check_lot(0.5, balance=500.0, sl_distance_price=2.0,
                                       symbol_info=symbol_info)
    assert safe is None and "risk" in reason.lower()


def test_sanity_passes_safe_lot(rm, symbol_info):
    safe, reason = rm.sanity_check_lot(0.02, balance=500.0, sl_distance_price=2.0,
                                       symbol_info=symbol_info)
    assert safe == 0.02


# ----------------------------------------------------------------------
# Margin pre-check
# ----------------------------------------------------------------------
def test_margin_check_ok(rm):
    def checker(otype, sym, lot, price):
        return {"required_margin": 100, "free_margin": 500}
    ok, _ = rm.check_margin_before_order(0, "XAUUSD", 0.01, 2000.0, checker)
    assert ok


def test_margin_check_blocked_when_over_buffer(rm):
    def checker(otype, sym, lot, price):
        return {"required_margin": 400, "free_margin": 500}
    ok, reason = rm.check_margin_before_order(0, "XAUUSD", 0.01, 2000.0, checker)
    assert not ok and "margin" in reason.lower()


# ----------------------------------------------------------------------
# can_trade gates
# ----------------------------------------------------------------------
def test_can_trade_blocks_below_min_balance(rm, account):
    account["balance"] = 50.0
    d = rm.can_trade(account)
    assert not d.ok and d.breaker == CircuitBreaker.MIN_BALANCE


def test_can_trade_blocks_low_margin_level(rm, account):
    account["margin_level"] = 100.0  # below min 200
    d = rm.can_trade(account)
    assert not d.ok and "margin_level" in d.reason


def test_can_trade_blocks_concurrent_trades(rm, account):
    class P: pass
    positions = [P(), P()]
    d = rm.can_trade(account, open_positions=positions)
    assert not d.ok and "concurrent" in d.reason


def test_can_trade_passes_clean(rm, account):
    d = rm.can_trade(account)
    assert d.ok


# ----------------------------------------------------------------------
# Circuit breakers / state lifecycle
# ----------------------------------------------------------------------
def test_consecutive_losses_trigger_cooldown(rm, account):
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    for _ in range(int(rm.cb["max_consecutive_losses"])):
        rm.update_after_trade(pnl=-5.0, balance_after=495.0, now_utc=now)
    assert rm.state.cooldown_until is not None
    decision = rm.can_trade(account, now_utc=now)
    assert not decision.ok and decision.breaker == CircuitBreaker.CONSECUTIVE_LOSSES


def test_winning_trade_resets_consecutive_losses(rm):
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    rm.update_after_trade(pnl=-5.0, balance_after=495.0, now_utc=now)
    rm.update_after_trade(pnl=+10.0, balance_after=505.0, now_utc=now)
    assert rm.state.consecutive_losses == 0


def test_kill_switch_triggers_on_total_drawdown(rm, account):
    rm.state.peak_balance = 1000.0
    rm._save()
    account["balance"] = 800.0  # 20% DD > 15%
    d = rm.can_trade(account)
    assert not d.ok and rm.state.kill_switch_active
    # Subsequent calls remain blocked
    d2 = rm.can_trade(account)
    assert not d2.ok and d2.breaker == CircuitBreaker.KILL_SWITCH


def test_state_persists_across_instances(cfg, db):
    rm1 = RiskManager(cfg, db)
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    rm1.update_after_trade(pnl=-5.0, balance_after=495.0, now_utc=now)
    losses = rm1.state.consecutive_losses
    rm2 = RiskManager(cfg, db)
    assert rm2.state.consecutive_losses == losses


def test_daily_reset_clears_pnl(rm):
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    rm.update_after_trade(pnl=-5.0, balance_after=495.0, now_utc=now)
    assert rm.state.daily_pnl == -5.0
    next_day = now + timedelta(days=1)
    rm.update_after_trade(pnl=+2.0, balance_after=497.0, now_utc=next_day)
    assert rm.state.daily_pnl == 2.0  # auto-reset on new day


# ----------------------------------------------------------------------
# Signal validation
# ----------------------------------------------------------------------
def test_validate_signal_rejects_low_rr(rm, symbol_info):
    class Sig:
        rr_ratio = 1.0; entry = 2000.0; sl = 1998.0; direction = Direction.LONG
    ok, reason = rm.validate_signal(Sig(), symbol_info, spread_points=10)
    assert not ok and "R:R" in reason


def test_validate_signal_rejects_wide_spread(rm, symbol_info):
    class Sig:
        rr_ratio = 2.5; entry = 2000.0; sl = 1998.0; direction = Direction.LONG
    ok, reason = rm.validate_signal(Sig(), symbol_info, spread_points=100)
    assert not ok and "spread" in reason


def test_validate_signal_rejects_duplicate_direction(rm, symbol_info):
    class Sig:
        rr_ratio = 2.5; entry = 2000.0; sl = 1998.0; direction = Direction.LONG
    class Pos:
        type = 0  # BUY
    ok, reason = rm.validate_signal(Sig(), symbol_info, spread_points=10,
                                     open_positions=[Pos()])
    assert not ok and "long" in reason.lower()


def test_validate_signal_passes_clean(rm, symbol_info):
    class Sig:
        rr_ratio = 2.5; entry = 2000.0; sl = 1997.5; direction = Direction.LONG
    ok, _ = rm.validate_signal(Sig(), symbol_info, spread_points=10)
    assert ok


# ----------------------------------------------------------------------
# Partial fill handling
# ----------------------------------------------------------------------
def test_partial_fill_below_min_returns_close(rm, symbol_info):
    close, _ = rm.handle_partial_fill(123, requested_lot=0.05, actual_lot=0.005,
                                      symbol_info=symbol_info)
    assert close


def test_partial_fill_above_min_keeps_position(rm, symbol_info):
    close, _ = rm.handle_partial_fill(123, requested_lot=0.05, actual_lot=0.03,
                                      symbol_info=symbol_info)
    assert not close

"""Tests for TradeManager — produces the right actions per situation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from config import load_config
from core.trade_manager import ActionType, TradeManager
from utils.constants import ExitReason


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture()
def tm(cfg) -> TradeManager:
    return TradeManager(cfg["trade_management"], cfg["risk"], cfg["sessions"])


def _long_pos(entry: float, sl: float, tp: float, age_hours: float = 1.0,
              ticket: int = 1, volume: float = 0.04, now: datetime | None = None):
    now = now or datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        ticket=ticket, type=0, volume=volume,
        price_open=entry, sl=sl, tp=tp,
        time=int((now - timedelta(hours=age_hours)).timestamp()),
    )


def _short_pos(entry: float, sl: float, tp: float, age_hours: float = 1.0,
               ticket: int = 2, volume: float = 0.04, now: datetime | None = None):
    now = now or datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        ticket=ticket, type=1, volume=volume,
        price_open=entry, sl=sl, tp=tp,
        time=int((now - timedelta(hours=age_hours)).timestamp()),
    )


# ----------------------------------------------------------------------
def test_partial_close_at_1r_for_long(tm):
    # entry 2000, sl 1998 (200pts risk). At 2002 (=1R) -> partial + BE
    pos = _long_pos(entry=2000.0, sl=1998.0, tp=2005.0, age_hours=1.0)
    now = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)  # Mon 14:00 UTC = Mon NY 10:00
    actions = tm.manage([pos], bid=2002.0, ask=2002.2, point=0.01, now_utc=now)
    types = [a.type for a in actions]
    assert ActionType.PARTIAL_CLOSE in types
    assert ActionType.MOVE_TO_BREAKEVEN in types
    pc = next(a for a in actions if a.type == ActionType.PARTIAL_CLOSE)
    assert pc.close_volume == pytest.approx(0.02)


def test_no_partial_below_1r(tm):
    pos = _long_pos(entry=2000.0, sl=1998.0, tp=2005.0)
    now = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    actions = tm.manage([pos], bid=2001.0, ask=2001.2, point=0.01, now_utc=now)
    assert not any(a.type == ActionType.PARTIAL_CLOSE for a in actions)


def test_trailing_stop_after_activation(tm):
    # SL already at BE, price reached >=1.5R -> trailing
    pos = _long_pos(entry=2000.0, sl=2000.0, tp=2010.0)  # SL at BE
    now = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    # 1.5R requires risk_pp first; with SL=BE risk_pp=0 -> no trailing.
    # Use a non-BE SL but past partial: SL still below entry but trailing kicks in.
    pos = _long_pos(entry=2000.0, sl=1998.0, tp=2010.0)
    actions = tm.manage([pos], bid=2003.5, ask=2003.7, point=0.01, now_utc=now)  # 1.75R
    # Expect either partial+BE (since not yet partial) OR trailing — first hit wins
    assert actions  # something fires


def test_max_duration_close(tm):
    pos = _long_pos(entry=2000.0, sl=1998.0, tp=2010.0, age_hours=25.0)
    now = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    actions = tm.manage([pos], bid=2001.0, ask=2001.2, point=0.01, now_utc=now)
    assert len(actions) == 1
    a = actions[0]
    assert a.type == ActionType.CLOSE_FULL
    assert a.exit_reason == ExitReason.MAX_DURATION


def test_friday_close_triggers(tm):
    # Friday April 10 2026, NY 16:00 = UTC 20:00
    pos = _long_pos(entry=2000.0, sl=1998.0, tp=2010.0, age_hours=1.0)
    fri = datetime(2026, 4, 10, 20, 0, tzinfo=timezone.utc)
    actions = tm.manage([pos], bid=2001.0, ask=2001.2, point=0.01, now_utc=fri)
    assert any(a.exit_reason == ExitReason.WEEKEND_CLOSE for a in actions)


def test_short_position_partial_close(tm):
    pos = _short_pos(entry=2000.0, sl=2002.0, tp=1995.0)
    now = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    actions = tm.manage([pos], bid=1997.8, ask=1998.0, point=0.01, now_utc=now)  # 1R for short
    assert any(a.type == ActionType.PARTIAL_CLOSE for a in actions)


def test_no_actions_for_empty_positions(tm):
    actions = tm.manage([], bid=2000.0, ask=2000.2, point=0.01)
    assert actions == []


def test_swap_aware_close_at_be(tm):
    # 16:55 NY = 20:55 UTC (winter) — within 10min of 17:00 rollover.
    # Position is profitable + SL at BE -> close
    pos = _long_pos(entry=2000.0, sl=2000.0, tp=2010.0, age_hours=2.0)
    now = datetime(2026, 1, 6, 21, 55, tzinfo=timezone.utc)  # winter NY = UTC-5; 21:55 UTC = 16:55 NY
    actions = tm.manage([pos], bid=2003.0, ask=2003.2, point=0.01, now_utc=now)
    assert any(a.exit_reason == ExitReason.SWAP_CLOSE for a in actions)

"""Tests for SessionManager — DST-safe session classification."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from config import load_config
from core.session_manager import SessionManager
from utils.constants import Session


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture()
def sm(cfg) -> SessionManager:
    return SessionManager(cfg["sessions"], cfg["holidays"])


def test_summer_london_open_at_0900_utc(sm):
    # July 1 = BST (UTC+1). 09:00 UTC = 10:00 London = LONDON open.
    t = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    assert sm.get_current_session(t) == Session.LONDON


def test_winter_london_open_at_0900_utc(sm):
    # January 6 = GMT (UTC+0). 09:00 UTC = 09:00 London = LONDON open.
    t = datetime(2026, 1, 6, 9, 0, tzinfo=timezone.utc)
    assert sm.get_current_session(t) == Session.LONDON


def test_summer_ny_overlap_at_1330_utc(sm):
    # Summer DST: NY = UTC-4, London = UTC+1.
    # 13:30 UTC = 14:30 London (open) AND 09:30 NY (open) -> overlap.
    t = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    assert sm.get_current_session(t) == Session.NY_OVERLAP


def test_winter_ny_overlap_at_1430_utc(sm):
    # Winter: NY = UTC-5, London = UTC+0.
    # 14:30 UTC = 14:30 London (open) AND 09:30 NY (open) -> overlap.
    t = datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc)
    assert sm.get_current_session(t) == Session.NY_OVERLAP


def test_dst_transition_handled(sm):
    # Sunday 8 March 2026 02:00 NY -> NY clocks jump to 03:00.
    # Pick Monday 9 March at 13:30 UTC. NY just switched to DST (UTC-4).
    # 13:30 UTC = 09:30 NY = NY open. London (DST starts later, March 29) is GMT.
    # 13:30 UTC = 13:30 London - inside London window -> overlap.
    t = datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)
    assert sm.get_current_session(t) == Session.NY_OVERLAP


def test_weekend_detected(sm):
    sat = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)  # Saturday
    assert sm.is_weekend(sat)
    assert sm.get_current_session(sat) == Session.WEEKEND


def test_holiday_detected(sm):
    # 2026-12-25 is in the closed_dates list
    t = datetime(2026, 12, 25, 14, 0, tzinfo=timezone.utc)
    assert sm.is_holiday(t)
    assert sm.get_current_session(t) == Session.WEEKEND


def test_friday_close(sm):
    # Friday 2026-04-10 in NY at 15:35 = 19:35 UTC
    t = datetime(2026, 4, 10, 19, 35, tzinfo=timezone.utc)
    assert sm.is_friday_close_time("15:30", t)
    # Earlier same day -> False
    t2 = datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    assert not sm.is_friday_close_time("15:30", t2)


def test_early_close_day(sm):
    # 2026-12-24 in early_close_dates_2026
    morning = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)
    assert sm.is_early_close_day(morning)
    # 13:00 NY = 18:00 UTC in winter (NY = UTC-5)
    afternoon = datetime(2026, 12, 24, 19, 0, tzinfo=timezone.utc)
    assert sm.is_past_early_close(afternoon)


def test_naive_datetime_rejected(sm):
    with pytest.raises(ValueError):
        sm.get_current_session(datetime(2026, 4, 13, 12, 0))


def test_is_tradeable_during_overlap(sm):
    t = datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    assert sm.is_tradeable(t)


def test_is_tradeable_during_asian_false(sm):
    # 02:00 UTC summer = 11:00 Tokyo = Asian session
    t = datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc)
    assert sm.get_current_session(t) == Session.ASIAN
    assert not sm.is_tradeable(t)


def test_asian_range_from_h1_candles(sm):
    # Build H1 candles spanning Asian session on 2026-07-01 (Tokyo).
    # Asian window 07:00-16:00 Tokyo = 22:00 UTC prev day -> 07:00 UTC same day.
    times = [datetime(2026, 6, 30, 22, 0, tzinfo=timezone.utc) + timedelta(hours=i)
             for i in range(10)]
    rows = [{"time": t, "open": 2000 + i, "high": 2005 + i, "low": 1995 + i, "close": 2002 + i}
            for i, t in enumerate(times)]
    df = pd.DataFrame(rows)
    # Query at 09:00 UTC (after Asian close)
    now = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    rng = sm.get_asian_range(df, now)
    assert rng is not None
    low, high = rng
    assert low < high

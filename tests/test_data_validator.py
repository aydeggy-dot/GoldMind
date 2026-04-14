"""Tests for DataValidator — bad data must NEVER pass through."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.data_validator import DataValidator


def _good_df(now: datetime, bars: int = 20, tf_minutes: int = 60) -> pd.DataFrame:
    times = [now - timedelta(minutes=tf_minutes * (bars - i)) for i in range(bars)]
    base = 2000.0
    rows = []
    for i, t in enumerate(times):
        o = base + i * 0.1
        c = o + 0.2
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        rows.append({"time": t, "open": o, "high": h, "low": l, "close": c, "tick_volume": 100})
    return pd.DataFrame(rows)


@pytest.fixture()
def validator() -> DataValidator:
    return DataValidator(max_candle_age_minutes=120, max_price_gap_pct=5.0)


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)


def test_good_data_passes(validator, now):
    df = _good_df(now)
    assert validator.validate_candles(df, "H1", "XAUUSD", now)


def test_empty_rejected(validator, now):
    res = validator.validate_candles(pd.DataFrame(), "H1", "XAUUSD", now)
    assert not res
    assert "empty" in res.reason


def test_nan_rejected(validator, now):
    df = _good_df(now)
    df.loc[5, "close"] = np.nan
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res and "NaN" in res.reason


def test_zero_price_rejected(validator, now):
    df = _good_df(now)
    df.loc[5, "low"] = 0.0
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res


def test_high_less_than_low_rejected(validator, now):
    df = _good_df(now)
    df.loc[5, "high"] = df.loc[5, "low"] - 1
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res and "impossible" in res.reason


def test_stale_data_rejected(validator):
    now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    df = _good_df(now - timedelta(hours=10))
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res and "stale" in res.reason


def test_per_tf_gap_override_allows_d1_jump(now):
    # 10% D1 jump would fail scalar 5% but passes with per-TF D1=15% override.
    v = DataValidator(max_price_gap_pct=5.0,
                      max_price_gap_pct_by_tf={"D1": 15.0})
    df = _good_df(now)
    new_close = df.loc[9, "close"] * 1.10
    df.loc[10, "close"] = new_close
    df.loc[10, "open"] = new_close - 0.2
    df.loc[10, "high"] = new_close + 0.5
    df.loc[10, "low"] = new_close - 1.0
    assert bool(v.validate_candles(df, "D1", "XAUUSD", now))
    assert not bool(v.validate_candles(df, "H1", "XAUUSD", now))


def test_price_gap_rejected(validator, now):
    df = _good_df(now)
    new_close = df.loc[9, "close"] * 1.10  # 10% jump
    df.loc[10, "close"] = new_close
    df.loc[10, "open"] = new_close - 0.2
    df.loc[10, "high"] = new_close + 0.5
    df.loc[10, "low"] = new_close - 1.0
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res and "gap" in res.reason


def test_zero_volume_rejected(validator, now):
    df = _good_df(now)
    df.loc[5, "tick_volume"] = 0
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res


def test_non_monotonic_time_rejected(validator, now):
    df = _good_df(now)
    df.loc[5, "time"], df.loc[6, "time"] = df.loc[6, "time"], df.loc[5, "time"]
    res = validator.validate_candles(df, "H1", "XAUUSD", now)
    assert not res and "monotonic" in res.reason


def test_tick_validation_good(validator, now):
    tick = {"bid": 2000.0, "ask": 2000.20, "time": int(now.timestamp())}
    assert validator.validate_tick(tick, point=0.01, now_utc=now)


def test_tick_inverted_rejected(validator, now):
    tick = {"bid": 2000.50, "ask": 2000.00, "time": int(now.timestamp())}
    res = validator.validate_tick(tick, point=0.01, now_utc=now)
    assert not res and "ask" in res.reason.lower()


def test_tick_stale_rejected(validator, now):
    old_time = int((now - timedelta(seconds=300)).timestamp())
    tick = {"bid": 2000.0, "ask": 2000.20, "time": old_time}
    res = validator.validate_tick(tick, point=0.01, now_utc=now)
    assert not res and "old" in res.reason


def test_tick_huge_spread_rejected(validator, now):
    tick = {"bid": 2000.0, "ask": 2002.0, "time": int(now.timestamp())}  # 200pt spread
    res = validator.validate_tick(tick, point=0.01, now_utc=now)
    assert not res and "spread" in res.reason

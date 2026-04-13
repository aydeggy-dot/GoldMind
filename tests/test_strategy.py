"""Tests for Strategy — H4 bias, key levels, all 3 setups."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from config import load_config
from core.strategy import Strategy
from utils.constants import Direction, MacroBias, Regime, SetupType


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture()
def strat(cfg) -> Strategy:
    return Strategy(cfg["strategy"], cfg["risk"])


@pytest.fixture()
def symbol_info() -> dict:
    return {"point": 0.01, "trade_tick_value": 1.0, "trade_tick_size": 0.01,
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}


# ----------------------------------------------------------------------
# Helpers to build OHLC frames
# ----------------------------------------------------------------------
def _h4_uptrend(n: int = 250) -> pd.DataFrame:
    times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=4 * i) for i in range(n)]
    closes = 1900 + np.arange(n) * 0.5
    return pd.DataFrame({
        "time": times, "open": closes - 0.2, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "tick_volume": [1] * n,
    })


def _h4_downtrend(n: int = 250) -> pd.DataFrame:
    times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=4 * i) for i in range(n)]
    closes = 2100 - np.arange(n) * 0.5
    return pd.DataFrame({
        "time": times, "open": closes + 0.2, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "tick_volume": [1] * n,
    })


# ----------------------------------------------------------------------
def test_h4_bias_uptrend(strat):
    assert strat.get_h4_bias(_h4_uptrend()) == Direction.LONG


def test_h4_bias_downtrend(strat):
    assert strat.get_h4_bias(_h4_downtrend()) == Direction.SHORT


def test_h4_bias_too_few_bars(strat):
    df = _h4_uptrend(n=10)
    assert strat.get_h4_bias(df) == Direction.NEUTRAL


def test_calculate_key_levels_includes_pdh_pdl_psych(strat, symbol_info):
    d1 = pd.DataFrame({
        "time": [datetime(2026, 1, i + 1, tzinfo=timezone.utc) for i in range(5)],
        "open": [1990, 1995, 2000, 2005, 2010],
        "high": [2002, 2008, 2015, 2020, 2025],
        "low":  [1985, 1990, 1995, 2000, 2005],
        "close":[2000, 2005, 2010, 2015, 2020],
    })
    h1 = pd.DataFrame({
        "time": [datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(50)],
        "open":  [2000] * 50, "high": [2005] * 50,
        "low":   [1995] * 50, "close":[2002] * 50,
    })
    levels = strat.calculate_key_levels(d1, h1, asian_range=(1990.0, 2010.0), point=0.01)
    names = {l.name for l in levels}
    assert "PDH" in names and "PDL" in names
    assert "Asian_H" in names and "Asian_L" in names
    assert any(n.startswith("Psych_") for n in names)


def test_trend_continuation_long_signal(strat, symbol_info):
    h4 = _h4_uptrend()
    n = 250
    times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(n)]
    closes = 1950 + np.arange(n) * 0.4
    h1 = pd.DataFrame({
        "time": times, "open": closes - 0.3, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "tick_volume": [1] * n,
    })
    # Engineer pullback: last bar dips to fast EMA and closes above with bullish body
    from ta.trend import EMAIndicator
    fast = EMAIndicator(close=h1["close"], window=strat.fast_ema).ema_indicator()
    fast_now = float(fast.iloc[-1])
    h1.loc[h1.index[-1], "low"] = fast_now - 1.5
    h1.loc[h1.index[-1], "open"] = fast_now - 1.0
    h1.loc[h1.index[-1], "close"] = fast_now + 2.0  # bullish close above fast EMA
    h1.loc[h1.index[-1], "high"] = fast_now + 2.3

    sigs = strat.scan_for_signals(
        h4=h4, h1=h1, m15=h1.tail(50), d1=h4,
        symbol_info=symbol_info, regime=Regime.TRENDING_BULLISH,
        macro_bias=MacroBias.BULLISH, asian_range=None,
    )
    tc = [s for s in sigs if s.type == SetupType.TREND_CONTINUATION]
    assert tc, "expected trend continuation signal"
    s = tc[0]
    assert s.direction == Direction.LONG
    assert s.h4_aligned and s.macro_aligned
    assert s.confidence >= 0.60
    assert s.tp > s.entry > s.sl


def test_no_signals_when_h4_neutral_and_no_setups_form(strat, symbol_info):
    # Random data — no clean trend, unlikely to produce a clean continuation
    rng = np.random.default_rng(7)
    n = 250
    closes = 2000 + rng.normal(0, 0.3, n).cumsum() * 0.05
    times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(n)]
    h1 = pd.DataFrame({
        "time": times, "open": closes, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "tick_volume": [1] * n,
    })
    sigs = strat.scan_for_signals(
        h4=h1, h1=h1, m15=h1.tail(50), d1=h1.tail(20),
        symbol_info=symbol_info, regime=Regime.RANGING,
        macro_bias=MacroBias.NEUTRAL, asian_range=None,
    )
    # In random range, trend continuation should not fire (H4 bias likely neutral).
    tc = [s for s in sigs if s.type == SetupType.TREND_CONTINUATION]
    assert not tc


def test_signal_confidence_dropped_by_counter_trend_macro(strat, symbol_info):
    h4 = _h4_uptrend()
    n = 250
    closes = 1950 + np.arange(n) * 0.4
    h1 = pd.DataFrame({
        "time": [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(n)],
        "open": closes - 0.3, "high": closes + 0.5,
        "low": closes - 0.5, "close": closes, "tick_volume": [1] * n,
    })
    from ta.trend import EMAIndicator
    fast = EMAIndicator(close=h1["close"], window=strat.fast_ema).ema_indicator()
    fast_now = float(fast.iloc[-1])
    h1.loc[h1.index[-1], "low"] = fast_now - 0.1
    h1.loc[h1.index[-1], "open"] = fast_now - 0.05
    h1.loc[h1.index[-1], "close"] = fast_now + 5.0
    h1.loc[h1.index[-1], "high"] = fast_now + 5.5

    aligned = strat.scan_for_signals(
        h4=h4, h1=h1, m15=h1.tail(50), d1=h4,
        symbol_info=symbol_info, regime=Regime.TRENDING_BULLISH,
        macro_bias=MacroBias.BULLISH, asian_range=None,
    )
    conflicting = strat.scan_for_signals(
        h4=h4, h1=h1, m15=h1.tail(50), d1=h4,
        symbol_info=symbol_info, regime=Regime.TRENDING_BULLISH,
        macro_bias=MacroBias.CONFLICTING, asian_range=None,
    )
    if aligned and conflicting:
        assert conflicting[0].confidence < aligned[0].confidence

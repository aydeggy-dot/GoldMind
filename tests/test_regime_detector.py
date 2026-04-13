"""Tests for RegimeDetector with synthetic price series."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.regime_detector import RegimeDetector
from utils.constants import Regime


def _df_from_close(closes: np.ndarray, start: datetime) -> pd.DataFrame:
    times = [start + timedelta(hours=i) for i in range(len(closes))]
    rows = []
    prev = closes[0]
    for t, c in zip(times, closes):
        h = max(prev, c) + 0.5
        l = min(prev, c) - 0.5
        rows.append({"time": t, "open": prev, "high": h, "low": l, "close": c, "tick_volume": 100})
        prev = c
    return pd.DataFrame(rows)


@pytest.fixture()
def detector() -> RegimeDetector:
    return RegimeDetector(adx_period=14, adx_trending_threshold=25,
                          adx_ranging_threshold=20, atr_period=14,
                          atr_spike_multiplier=2.0, confirmation_bars=3,
                          fast_ema=50, slow_ema=200)


def test_unknown_when_too_few_bars(detector):
    df = _df_from_close(np.array([2000.0] * 50), datetime(2026, 1, 1, tzinfo=timezone.utc))
    r = detector.detect(df)
    assert r.regime == Regime.UNKNOWN


def test_strong_uptrend_classified_bullish(detector):
    closes = 2000 + np.arange(300) * 1.5  # steady uptrend
    df = _df_from_close(closes, datetime(2026, 1, 1, tzinfo=timezone.utc))
    r = detector.detect(df)
    assert r.regime == Regime.TRENDING_BULLISH
    assert r.adx > 25


def test_strong_downtrend_classified_bearish(detector):
    closes = 2500 - np.arange(300) * 1.5
    df = _df_from_close(closes, datetime(2026, 1, 1, tzinfo=timezone.utc))
    r = detector.detect(df)
    assert r.regime == Regime.TRENDING_BEARISH


def test_choppy_market_ranging(detector):
    rng = np.random.default_rng(42)
    closes = 2000 + rng.normal(0, 0.5, 300).cumsum() * 0.05  # near-flat
    closes = np.clip(closes, 1995, 2005)
    df = _df_from_close(closes, datetime(2026, 1, 1, tzinfo=timezone.utc))
    r = detector.detect(df)
    assert r.regime in (Regime.RANGING, Regime.TRANSITIONING)


def test_atr_spike_classified_volatile(detector):
    closes = list(2000 + np.arange(290) * 0.05)
    closes += [2010, 1980, 2020, 1970, 2030, 1960, 2040, 1955, 2045, 1950]
    df = _df_from_close(np.array(closes), datetime(2026, 1, 1, tzinfo=timezone.utc))
    r = detector.detect(df)
    assert r.regime == Regime.VOLATILE_CRISIS
    assert r.atr_ratio >= 2.0


def test_should_trade_only_for_trending(detector):
    closes = 2000 + np.arange(300) * 1.5
    df = _df_from_close(closes, datetime(2026, 1, 1, tzinfo=timezone.utc))
    r = detector.detect(df)
    assert RegimeDetector.should_trade(r)

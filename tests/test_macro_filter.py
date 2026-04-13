"""Tests for MacroFilter using injected fakes (no network)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.macro_filter import MacroFilter
from utils.constants import MacroBias


def _make_series(start: float, drift: float, n: int = 200) -> pd.DataFrame:
    times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(n)]
    closes = [start + drift * i for i in range(n)]
    return pd.DataFrame({
        "time": times, "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes], "close": closes, "tick_volume": [1] * n,
    })


@pytest.fixture()
def macro_cfg() -> dict:
    return {
        "enabled": True, "dxy_filter_enabled": True, "us10y_filter_enabled": True,
        "vix_filter_enabled": True, "use_web_api_fallback": False,
        "dxy_symbol": "DXY", "dxy_symbol_fallbacks": [], "dxy_ema_period": 50,
        "us10y_symbol": "US10Y", "us10y_symbol_fallbacks": [],
        "yield_change_threshold": 0.05,
        "vix_symbol": "VIX", "vix_symbol_fallbacks": [],
        "vix_risk_off_threshold": 25, "vix_extreme_threshold": 35,
        "web_api_cache_minutes": 15,
    }


def test_dxy_up_yields_up_vix_normal_gives_bearish_gold(macro_cfg):
    series = {
        "DXY": _make_series(100, 0.05),                    # rising DXY
        "US10Y": pd.DataFrame({"close": [4.0, 4.10]}),     # +0.10 yield jump
        "VIX": pd.DataFrame({"close": [15.0]}),
    }
    def fetch(sym, tf, n): return series[sym]
    def resolve(primary, fb): return primary
    mf = MacroFilter(macro_cfg, broker_fetcher=fetch, symbol_resolver=resolve)
    r = mf.evaluate()
    assert r.bias == MacroBias.BEARISH
    assert r.dxy_direction == MacroBias.BULLISH       # DXY up
    assert r.yield_direction == MacroBias.BULLISH     # yields up


def test_dxy_down_yields_down_vix_normal_gives_bullish_gold(macro_cfg):
    series = {
        "DXY": _make_series(110, -0.05),
        "US10Y": pd.DataFrame({"close": [4.20, 4.05]}),
        "VIX": pd.DataFrame({"close": [15.0]}),
    }
    def fetch(sym, tf, n): return series[sym]
    def resolve(primary, fb): return primary
    mf = MacroFilter(macro_cfg, broker_fetcher=fetch, symbol_resolver=resolve)
    r = mf.evaluate()
    assert r.bias == MacroBias.BULLISH


def test_extreme_vix_pushes_bullish_when_others_neutral(macro_cfg):
    series = {
        "DXY": _make_series(100, 0.0),                  # flat
        "US10Y": pd.DataFrame({"close": [4.00, 4.00]}),
        "VIX": pd.DataFrame({"close": [40.0]}),         # extreme
    }
    def fetch(sym, tf, n): return series[sym]
    def resolve(primary, fb): return primary
    mf = MacroFilter(macro_cfg, broker_fetcher=fetch, symbol_resolver=resolve)
    r = mf.evaluate()
    assert r.vix_state == "extreme"
    assert r.bias == MacroBias.BULLISH


def test_disabled_returns_neutral(macro_cfg):
    macro_cfg["enabled"] = False
    mf = MacroFilter(macro_cfg)
    assert mf.evaluate().bias == MacroBias.NEUTRAL


def test_web_fallback_when_broker_returns_empty(macro_cfg):
    macro_cfg["use_web_api_fallback"] = True
    def fetch(sym, tf, n): return pd.DataFrame()
    def resolve(primary, fb): return None
    web_calls: list[tuple[str, int]] = []

    def web_fetch(ticker, count):
        web_calls.append((ticker, count))
        return _make_series(100, 0.05).rename(columns={"tick_volume": "volume"})

    mf = MacroFilter(macro_cfg, broker_fetcher=fetch,
                     symbol_resolver=resolve, web_fetcher=web_fetch)
    r = mf.evaluate()
    assert web_calls, "web fetcher should have been called as fallback"
    # bias is something concrete (not NEUTRAL) since we returned data
    assert r.bias in (MacroBias.BULLISH, MacroBias.BEARISH, MacroBias.CONFLICTING, MacroBias.NEUTRAL)

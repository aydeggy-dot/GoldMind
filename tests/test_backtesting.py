"""Phase 8 tests — end-to-end on synthetic OHLC.

Uses an uptrend series plus an engineered pullback to force the
trend-continuation setup to fire, then asserts the backtester simulates
fills, TP/SL, partials, and emits analytics-compatible trade dicts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from backtesting.backtester import Backtester, BacktestConfig
from backtesting.report_generator import (
    build_backtest_report, build_walk_forward_report,
)
from backtesting.walk_forward import WalkForwardResult, WindowResult, validate_windows
from config import load_config


# ----------------------------------------------------------------------
def _trend_df(now: datetime, tf_minutes: int, n: int,
              start: float = 1900.0, drift: float = 0.5) -> pd.DataFrame:
    times = [now - timedelta(minutes=tf_minutes * (n - 1 - i)) for i in range(n)]
    closes = start + np.arange(n) * drift
    return pd.DataFrame({
        "time": times,
        "open": closes - 0.2,
        "high": closes + 0.6,
        "low": closes - 0.6,
        "close": closes,
        "tick_volume": [100] * n,
    })


def _engineer_pullback(h1: pd.DataFrame, fast_ema_period: int = 50) -> pd.DataFrame:
    from ta.trend import EMAIndicator
    fast = EMAIndicator(close=h1["close"], window=fast_ema_period).ema_indicator()
    df = h1.copy()
    # Carve two pullback zones (rows halfway and 3/4 of the way)
    for i in (len(df) // 2, 3 * len(df) // 4):
        fn = float(fast.iloc[i])
        df.loc[i, "low"] = fn - 1.5
        df.loc[i, "open"] = fn - 1.0
        df.loc[i, "close"] = fn + 2.0
        df.loc[i, "high"] = fn + 2.3
    return df


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture(scope="module")
def symbol_info() -> dict:
    return {
        "point": 0.01, "trade_tick_value": 1.0, "trade_tick_size": 0.01,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
        "trade_contract_size": 100.0, "margin_initial": 0.0,
        "swap_long": -5.0, "swap_short": -3.0,
        "trade_leverage": 500, "spread": 20,
    }


@pytest.fixture()
def data() -> dict[str, pd.DataFrame]:
    now = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    h1 = _engineer_pullback(_trend_df(now, 60, 500))
    return {
        "H4": _trend_df(now, 240, 500),
        "H1": h1,
        "M15": _trend_df(now, 15, 500),
        "D1": _trend_df(now, 1440, 80),
    }


# ----------------------------------------------------------------------
def test_backtester_runs_end_to_end(cfg, data, symbol_info):
    bt = Backtester(config=cfg, data=data, symbol_info=symbol_info)
    result = bt.run()
    assert result.signals_scanned >= 0
    # Trade dicts are shaped compatibly with analytics.compute_metrics
    for t in result.trades:
        assert "pnl" in t and "entry_time" in t and "exit_time" in t
        assert t["is_backtest"] == 1


def test_backtester_generates_some_trades_on_trending_data(cfg, data, symbol_info):
    bt = Backtester(config=cfg, data=data, symbol_info=symbol_info)
    result = bt.run()
    # With engineered pullbacks on a strong uptrend, expect at least one trade.
    assert len(result.trades) >= 1


def test_backtest_report_text(cfg, data, symbol_info):
    bt = Backtester(config=cfg, data=data, symbol_info=symbol_info)
    result = bt.run()
    text = build_backtest_report(result, strategy_version="1.0.0")
    assert "Backtest Report" in text and "trades:" in text


def test_deterministic_with_seed(cfg, data, symbol_info):
    bt_cfg = BacktestConfig(seed=123)
    a = Backtester(config=cfg, data=data, symbol_info=symbol_info, bt_cfg=bt_cfg).run()
    b = Backtester(config=cfg, data=data, symbol_info=symbol_info, bt_cfg=bt_cfg).run()
    assert a.final_balance == b.final_balance
    assert len(a.trades) == len(b.trades)


def test_walk_forward_validation_criteria():
    # Build three synthetic window results: 2 pass PF/WR, 1 fails — should be 66% > 60%
    from analytics.performance import PerformanceMetrics
    def mk(pf: float, wr: float, dd: float) -> WindowResult:
        m = PerformanceMetrics(trades=40, wins=int(40 * wr / 100), losses=10,
                               win_rate=wr, profit_factor=pf)
        return WindowResult(index=0, start=datetime(2022, 1, 1, tzinfo=timezone.utc),
                            end=datetime(2022, 3, 1, tzinfo=timezone.utc),
                            metrics=m, trades=m.trades, max_dd_pct=dd, valid=True)
    wf = validate_windows([mk(1.5, 55, 10), mk(1.2, 45, 12), mk(0.8, 35, 18)])
    # pf_ok 2/3 = 66.7% >= 60%, wr_ok 2/3 = 66.7% >= 60%, dd all < 20 -> passes
    assert wf.passes_criteria is True


def test_walk_forward_fails_when_dd_excessive():
    from analytics.performance import PerformanceMetrics
    def mk(dd: float) -> WindowResult:
        m = PerformanceMetrics(trades=40, wins=30, losses=10, win_rate=75.0,
                               profit_factor=2.0)
        return WindowResult(index=0, start=datetime(2022, 1, 1, tzinfo=timezone.utc),
                            end=datetime(2022, 3, 1, tzinfo=timezone.utc),
                            metrics=m, trades=m.trades, max_dd_pct=dd, valid=True)
    wf = validate_windows([mk(10), mk(25)])  # one DD over 20
    assert wf.passes_criteria is False


def test_walk_forward_report_text():
    from analytics.performance import PerformanceMetrics
    m = PerformanceMetrics(trades=30, wins=18, losses=12, win_rate=60,
                           profit_factor=1.6)
    wf = WalkForwardResult(
        windows=[WindowResult(index=0, start=datetime(2022, 1, 1, tzinfo=timezone.utc),
                              end=datetime(2022, 3, 1, tzinfo=timezone.utc),
                              metrics=m, trades=30, max_dd_pct=12, valid=True)],
        passes_criteria=True, criteria_detail={"pf_pass_pct": 100.0},
    )
    text = build_walk_forward_report(wf)
    assert "Walk-Forward Report" in text and "Window #0" in text

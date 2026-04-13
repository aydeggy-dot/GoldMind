"""Walk-forward runner.

Our strategy has no tunable parameters that need optimization, so
"walk-forward" here answers the spec question: does the strategy remain
stable across non-overlapping out-of-sample windows?

Slice the H1 dataset into rolling windows sized by `in_sample_months +
out_of_sample_months`. For each window, run the backtester against the
OOS slice (the IS slice exists for the strategy warm-up only, since
nothing is fitted). Collect per-window metrics and validate against the
spec's criteria:

    - OOS PF > 1.0 in 60%+ windows
    - OOS WR > 40% in 60%+ windows
    - OOS max DD < 20% in ALL windows
    - Minimum trades per OOS window
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

import pandas as pd

from analytics.performance import PerformanceMetrics, compute_metrics
from backtesting.backtester import Backtester, BacktestConfig, _as_utc


@dataclass
class WindowResult:
    index: int
    start: datetime
    end: datetime
    metrics: PerformanceMetrics
    trades: int
    max_dd_pct: float
    valid: bool


@dataclass
class WalkForwardResult:
    windows: list[WindowResult] = field(default_factory=list)
    passes_criteria: bool = False
    criteria_detail: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------
def _month_window_bounds(start: datetime, months: int) -> datetime:
    # Calendar-agnostic approximation: 30-day months are fine for slicing.
    return start + timedelta(days=30 * months)


def run_walk_forward(
    *,
    config: Mapping[str, Any],
    data: Mapping[str, pd.DataFrame],
    symbol_info: Mapping[str, Any],
    in_sample_months: int | None = None,
    out_of_sample_months: int | None = None,
    min_trades: int | None = None,
    bt_cfg: BacktestConfig | None = None,
) -> WalkForwardResult:
    wf = config.get("backtesting", {}).get("walk_forward", {})
    in_m = int(in_sample_months if in_sample_months is not None
               else wf.get("in_sample_months", 6))
    oos_m = int(out_of_sample_months if out_of_sample_months is not None
                else wf.get("out_of_sample_months", 2))
    min_tr = int(min_trades if min_trades is not None
                 else wf.get("minimum_trades", 30))

    h1 = data["H1"].copy().reset_index(drop=True)
    if h1.empty:
        return WalkForwardResult()

    h1["_time"] = pd.to_datetime(h1["time"], utc=True)
    first = h1["_time"].iloc[0].to_pydatetime()
    last = h1["_time"].iloc[-1].to_pydatetime()

    windows: list[WindowResult] = []
    cursor = first
    idx = 0
    while True:
        is_end = _month_window_bounds(cursor, in_m)
        oos_end = _month_window_bounds(is_end, oos_m)
        if oos_end > last:
            break

        mask = (h1["_time"] >= cursor) & (h1["_time"] <= oos_end)
        window_h1 = h1.loc[mask].drop(columns=["_time"]).reset_index(drop=True)
        if len(window_h1) < int(config["warm_up"]["required_bars"]["H1"]) + 50:
            cursor = is_end
            idx += 1
            continue

        # Slice other TFs to window too (or pass whole — Strategy only reads them)
        window_data = {"H1": window_h1}
        for tf in ("H4", "M15", "D1"):
            if tf in data and not data[tf].empty:
                tfd = data[tf].copy()
                tfd["_t"] = pd.to_datetime(tfd["time"], utc=True)
                window_data[tf] = (tfd.loc[(tfd["_t"] >= cursor) & (tfd["_t"] <= oos_end)]
                                   .drop(columns=["_t"]).reset_index(drop=True))
            else:
                window_data[tf] = pd.DataFrame()

        bt = Backtester(config=config, data=window_data,
                        symbol_info=symbol_info, bt_cfg=bt_cfg)
        result = bt.run()

        # Filter to OOS portion only (trades entered after is_end)
        oos_trades = [t for t in result.trades
                      if _as_utc(t["entry_time"]) >= is_end]
        metrics = compute_metrics(oos_trades, window=None)
        initial_balance = (bt_cfg.initial_balance if bt_cfg
                           else float(config.get("backtesting", {})
                                      .get("initial_balance", 500.0)))
        max_dd_pct = (100.0 * metrics.max_drawdown / initial_balance
                      if initial_balance > 0 else 0.0)
        valid = metrics.trades >= min_tr

        windows.append(WindowResult(
            index=idx, start=cursor, end=oos_end,
            metrics=metrics, trades=metrics.trades,
            max_dd_pct=max_dd_pct, valid=valid,
        ))
        cursor = is_end
        idx += 1

    return _validate(windows)


# ----------------------------------------------------------------------
def validate_windows(windows: list[WindowResult]) -> WalkForwardResult:
    """Re-run the spec criteria on a prebuilt window list."""
    return _validate(windows)


def _validate(windows: list[WindowResult]) -> WalkForwardResult:
    valid_windows = [w for w in windows if w.valid]
    n = len(valid_windows)
    if n == 0:
        return WalkForwardResult(windows=windows, passes_criteria=False,
                                 criteria_detail={"reason": "no valid windows"})

    pf_ok = sum(1 for w in valid_windows if w.metrics.profit_factor > 1.0)
    wr_ok = sum(1 for w in valid_windows if w.metrics.win_rate > 40.0)
    dd_all = all(w.max_dd_pct < 20.0 for w in valid_windows)

    pf_pct = 100.0 * pf_ok / n
    wr_pct = 100.0 * wr_ok / n
    passes = (pf_pct >= 60.0 and wr_pct >= 60.0 and dd_all)

    return WalkForwardResult(
        windows=windows,
        passes_criteria=passes,
        criteria_detail={
            "pf_pass_pct": pf_pct,
            "wr_pass_pct": wr_pct,
            "dd_all_under_20": dd_all,
            "valid_windows": n,
        },
    )

"""Performance metrics over closed trades.

Pure computation over a list of trade dicts (as returned by the DB layer).
The same shape comes from live trading and from the backtester, so Phase 8
will reuse this module unchanged.

All metrics tolerate empty / partial inputs: return zeros rather than raise,
because an alert "no trades yet" is more useful than a stack trace in
a daily report.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _closed(trades: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return trades that have been closed (have exit_time and pnl)."""
    return [t for t in trades if t.get("exit_time") and t.get("pnl") is not None]


def _pnls(trades: Sequence[Mapping[str, Any]]) -> list[float]:
    return [float(t.get("pnl") or 0.0) for t in trades]


def _safe_mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _safe_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _safe_mean(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


# ----------------------------------------------------------------------
@dataclass
class PerformanceMetrics:
    """Rolling performance report over a window of trades."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0       # avg P&L per trade
    sharpe: float = 0.0           # per-trade sharpe; NOT annualized
    avg_rr_achieved: float = 0.0
    max_drawdown: float = 0.0     # in currency (peak-to-trough on the curve)
    max_drawdown_duration: int = 0  # trades-between-peak-and-recovery
    total_pnl: float = 0.0
    total_swap_costs: float = 0.0
    partial_fill_count: int = 0
    by_session: dict[str, float] = field(default_factory=dict)
    by_setup: dict[str, float] = field(default_factory=dict)
    by_day_of_week: dict[str, float] = field(default_factory=dict)
    by_strategy_version: dict[str, float] = field(default_factory=dict)
    best_session: str | None = None
    worst_session: str | None = None
    best_setup: str | None = None
    worst_setup: str | None = None


# ----------------------------------------------------------------------
def compute_metrics(
    trades: Sequence[Mapping[str, Any]],
    window: int | None = 50,
) -> PerformanceMetrics:
    """Compute metrics over the last `window` closed trades (None = all)."""
    closed = _closed(trades)
    if window is not None and len(closed) > window:
        closed = closed[-window:]

    pnls = _pnls(closed)
    n = len(closed)
    if n == 0:
        return PerformanceMetrics()

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    be = [p for p in pnls if p == 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if wins else 0.0)
    win_rate = 100.0 * len(wins) / n
    expectancy = sum(pnls) / n
    sharpe = expectancy / _safe_std(pnls) if _safe_std(pnls) > 0 else 0.0

    rr_vals = [float(t["rr_achieved"]) for t in closed if t.get("rr_achieved") is not None]
    avg_rr = _safe_mean(rr_vals)

    max_dd, max_dd_duration = _drawdown(pnls)

    total_pnl = sum(pnls)
    total_swap = sum(float(t.get("swap_cost") or 0.0) for t in closed)
    partials = sum(1 for t in closed if int(t.get("partial_fill") or 0) == 1)

    def _group(key: str) -> dict[str, float]:
        d: dict[str, float] = defaultdict(float)
        for t in closed:
            k = t.get(key)
            if k:
                d[str(k)] += float(t.get("pnl") or 0.0)
        return dict(d)

    by_session = _group("session")
    by_setup = _group("setup_type")
    by_version = _group("strategy_version")

    by_dow: dict[str, float] = defaultdict(float)
    for t in closed:
        ts = t.get("entry_time")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        by_dow[dt.strftime("%A")] += float(t.get("pnl") or 0.0)

    best_session = max(by_session, key=by_session.get, default=None) if by_session else None
    worst_session = min(by_session, key=by_session.get, default=None) if by_session else None
    best_setup = max(by_setup, key=by_setup.get, default=None) if by_setup else None
    worst_setup = min(by_setup, key=by_setup.get, default=None) if by_setup else None

    return PerformanceMetrics(
        trades=n, wins=len(wins), losses=len(losses), breakeven=len(be),
        win_rate=win_rate, profit_factor=pf, expectancy=expectancy, sharpe=sharpe,
        avg_rr_achieved=avg_rr, max_drawdown=max_dd,
        max_drawdown_duration=max_dd_duration,
        total_pnl=total_pnl, total_swap_costs=total_swap,
        partial_fill_count=partials,
        by_session=by_session, by_setup=by_setup,
        by_day_of_week=dict(by_dow),
        by_strategy_version=by_version,
        best_session=best_session, worst_session=worst_session,
        best_setup=best_setup, worst_setup=worst_setup,
    )


def _drawdown(pnls: Sequence[float]) -> tuple[float, int]:
    """Max drawdown in currency + duration in trades from peak to recovery.

    If the curve never recovers, duration is trades-from-peak-to-end.
    """
    if not pnls:
        return 0.0, 0
    equity = 0.0
    peak = 0.0
    peak_idx = 0
    max_dd = 0.0
    max_dd_dur = 0
    dd_start_idx = 0
    for i, p in enumerate(pnls):
        equity += p
        if equity > peak:
            peak = equity
            peak_idx = i
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            dd_start_idx = peak_idx
            max_dd_dur = i - dd_start_idx
    return max_dd, max_dd_dur

"""Plain-text reports for a single backtest run and for a walk-forward sweep.

Intentionally plain text (not markdown) — the same function is used for
CLI output, logs, and as the body of a Telegram message. No formatting
surprises when a strategy name contains '_' or '*'.
"""
from __future__ import annotations

from typing import Any

from analytics.performance import PerformanceMetrics, compute_metrics
from backtesting.backtester import BacktestResult
from backtesting.walk_forward import WalkForwardResult, WindowResult


def _fmt_m(m: PerformanceMetrics) -> list[str]:
    return [
        f"  trades: {m.trades}  wins: {m.wins}  losses: {m.losses}",
        f"  win_rate: {m.win_rate:.1f}%  profit_factor: {m.profit_factor:.2f}",
        f"  expectancy: {m.expectancy:.2f}  sharpe: {m.sharpe:.2f}",
        f"  total_pnl: {m.total_pnl:.2f}  max_dd: {m.max_drawdown:.2f}"
        f" (dur={m.max_drawdown_duration} trades)",
        f"  avg_rr_achieved: {m.avg_rr_achieved:.2f}"
        f"  total_swap: {m.total_swap_costs:.2f}"
        f"  partials: {m.partial_fill_count}",
    ]


def build_backtest_report(result: BacktestResult,
                          *, strategy_version: str = "n/a") -> str:
    m = compute_metrics(result.trades, window=None)
    lines = [
        "=== Backtest Report ===",
        f"strategy: v{strategy_version}",
        f"signals scanned: {result.signals_scanned}",
        f"final balance: {result.final_balance:.2f}",
        "",
        "Metrics:",
    ]
    lines.extend(_fmt_m(m))
    lines.append("")
    if m.by_setup:
        lines.append("By setup:")
        for k, v in sorted(m.by_setup.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k}: {v:.2f}")
    if m.by_session:
        lines.append("By session:")
        for k, v in sorted(m.by_session.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k}: {v:.2f}")
    return "\n".join(lines)


def build_walk_forward_report(wf: WalkForwardResult) -> str:
    lines = [
        "=== Walk-Forward Report ===",
        f"windows: {len(wf.windows)}   "
        f"passes_criteria: {wf.passes_criteria}",
    ]
    if wf.criteria_detail:
        for k, v in wf.criteria_detail.items():
            lines.append(f"  {k}: {v}")
    lines.append("")
    for w in wf.windows:
        lines.append(f"Window #{w.index}  "
                     f"{w.start.date()} -> {w.end.date()}  "
                     f"valid={w.valid}  trades={w.trades}  "
                     f"max_dd_pct={w.max_dd_pct:.2f}")
        lines.extend(_fmt_m(w.metrics))
    return "\n".join(lines)

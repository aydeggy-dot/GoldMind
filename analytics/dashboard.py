"""Daily and weekly report builders.

Pure functions that turn trade rows + account context into a `daily_reports`
row dict and into a human-readable string (via notifications.templates).

The engine can call `build_and_persist_daily(...)` at the configured
`daily_report_time` to write one row and push a Telegram message.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from analytics.performance import compute_metrics
from notifications import templates


def build_daily_report(
    *,
    trades: Sequence[Mapping[str, Any]],
    balance: float,
    equity: float,
    margin_level: float,
    regime: str,
    macro_bias: str,
    strategy_version: str,
    circuit_breakers: Sequence[str] = (),
    spread_avg: float = 0.0,
    leverage: float = 0.0,
    memory_mb: float = 0.0,
    disk_free_gb: float = 0.0,
    clock_drift_seconds: float = 0.0,
    broker_spec_changes: Mapping[str, Any] | None = None,
    report_date: date | None = None,
    peak_balance: float = 0.0,
) -> dict[str, Any]:
    """Return a dict shaped like the daily_reports row."""
    rep_date = (report_date or datetime.now(timezone.utc).date()).isoformat()
    m = compute_metrics(trades, window=None)
    dd_from_peak = 0.0
    if peak_balance > 0:
        dd_from_peak = 100.0 * max(0.0, (peak_balance - balance)) / peak_balance
    daily_pnl_pct = 100.0 * m.total_pnl / balance if balance > 0 else 0.0
    return {
        "date": rep_date,
        "balance": balance,
        "equity": equity,
        "margin_level": margin_level,
        "trades_count": m.trades,
        "wins": m.wins,
        "losses": m.losses,
        "daily_pnl": m.total_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "swap_costs_total": m.total_swap_costs,
        "drawdown_from_peak": dd_from_peak,
        "regime": regime,
        "macro_bias": macro_bias,
        "circuit_breakers_triggered": json.dumps(list(circuit_breakers)),
        "strategy_version": strategy_version,
        "spread_avg": spread_avg,
        "leverage": leverage,
        "memory_usage_mb": memory_mb,
        "disk_free_gb": disk_free_gb,
        "clock_drift_seconds": clock_drift_seconds,
        "broker_spec_changes": json.dumps(dict(broker_spec_changes or {})),
        "notes": "",
    }


def daily_message(row: Mapping[str, Any]) -> str:
    return templates.daily_report(
        date=str(row["date"]),
        trades=int(row.get("trades_count") or 0),
        wins=int(row.get("wins") or 0),
        losses=int(row.get("losses") or 0),
        pnl=float(row.get("daily_pnl") or 0.0),
        win_rate=(100.0 * (row.get("wins") or 0) /
                  max(1, row.get("trades_count") or 0)),
        balance=float(row.get("balance") or 0.0),
        max_dd=float(row.get("drawdown_from_peak") or 0.0),
    )


def build_weekly_report(
    *,
    trades: Sequence[Mapping[str, Any]],
    balance: float,
    strategy_version: str,
    week_iso: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if week_iso is None:
        y, w, _ = now.isocalendar()
        week_iso = f"{y}-W{w:02d}"
    m = compute_metrics(trades, window=None)
    return {
        "week": week_iso,
        "trades": m.trades,
        "win_rate": m.win_rate,
        "profit_factor": m.profit_factor,
        "total_pnl": m.total_pnl,
        "balance": balance,
        "strategy_version": strategy_version,
        "by_setup": m.by_setup,
        "by_session": m.by_session,
    }


def weekly_message(row: Mapping[str, Any]) -> str:
    return templates.weekly_report(
        week=str(row["week"]),
        trades=int(row.get("trades") or 0),
        pnl=float(row.get("total_pnl") or 0.0),
        win_rate=float(row.get("win_rate") or 0.0),
        profit_factor=float(row.get("profit_factor") or 0.0),
        balance=float(row.get("balance") or 0.0),
    )


def persist_daily(db: Any, row: Mapping[str, Any]) -> None:
    """Upsert a daily_reports row (unique on date)."""
    cols = ", ".join(row.keys())
    placeholders = ", ".join(["?"] * len(row))
    updates = ", ".join(f"{k}=excluded.{k}" for k in row.keys() if k != "date")
    sql = (
        f"INSERT INTO daily_reports ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET {updates}"
    )
    db.execute(sql, tuple(row.values()))


def fetch_trades_since(db: Any, since: datetime, *, is_backtest: int = 0) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT * FROM trades WHERE is_backtest=? AND entry_time >= ? "
        "ORDER BY id ASC", (is_backtest, since.isoformat()))
    return [dict(r) for r in rows]


def fetch_trades_this_week(db: Any) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return fetch_trades_since(db, start)

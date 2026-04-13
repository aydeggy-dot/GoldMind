"""Telegram message templates.

Each function returns a plain-text block (no HTML/Markdown markup — keeps the
sender dumb and avoids escape bugs with broker strings that contain '.' '_' '*').
Functions are side-effect free so they can be unit-tested without Telegram.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


def _fmt_money(x: float | int | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):,.2f}"


def _fmt_pct(x: float | int | None) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):.2f}%"


def startup(symbol: str, balance: float, strategy_version: str) -> str:
    return (
        "GoldMind started.\n"
        f"Symbol: {symbol}\n"
        f"Balance: {_fmt_money(balance)} USD\n"
        f"Strategy: v{strategy_version}\n"
        "Warm-up complete. Ready to trade."
    )


def shutdown() -> str:
    return "GoldMind engine stopped. State saved. Open positions protected by SL/TP."


def trade_open(
    *,
    direction: str,
    symbol: str,
    lot: float,
    fill_price: float,
    sl: float,
    tp: float,
    setup_type: str,
    confidence: float,
    strategy_version: str,
    partial_fill: bool = False,
    margin_level: float | None = None,
) -> str:
    rr = abs(tp - fill_price) / max(abs(fill_price - sl), 1e-9)
    lines = [
        f"OPEN {direction} {symbol} {lot:.2f} @ {fill_price:.2f}",
        f"SL={sl:.2f}  TP={tp:.2f}  R:R={rr:.2f}",
        f"Setup: {setup_type}  conf={confidence:.2f}  v{strategy_version}",
    ]
    if margin_level is not None:
        lines.append(f"Margin level: {margin_level:.0f}%")
    if partial_fill:
        lines.append("WARNING: partial fill")
    return "\n".join(lines)


def trade_close(
    *,
    ticket: int,
    symbol: str,
    direction: str,
    pnl: float,
    exit_reason: str,
    duration_hours: float | None = None,
    swap_cost: float | None = None,
    rr_achieved: float | None = None,
) -> str:
    lines = [
        f"CLOSE {direction} {symbol} #{ticket}",
        f"P&L: {_fmt_money(pnl)} USD",
        f"Reason: {exit_reason}",
    ]
    if duration_hours is not None:
        lines.append(f"Duration: {duration_hours:.1f}h")
    if swap_cost is not None:
        lines.append(f"Swap: {_fmt_money(swap_cost)}")
    if rr_achieved is not None:
        lines.append(f"R:R achieved: {rr_achieved:.2f}")
    return "\n".join(lines)


def partial_close(*, ticket: int, volume: float, pnl: float | None = None) -> str:
    pnl_s = f"  P&L {_fmt_money(pnl)}" if pnl is not None else ""
    return f"Partial close #{ticket} vol={volume:.2f}{pnl_s}"


def circuit_breaker(*, name: str, detail: str) -> str:
    return f"CIRCUIT BREAKER TRIPPED: {name}\n{detail}"


def kill_switch(*, reason: str, balance: float) -> str:
    return (
        "KILL SWITCH ACTIVATED\n"
        f"Reason: {reason}\n"
        f"Balance: {_fmt_money(balance)} USD\n"
        "All positions closed. Trading disabled."
    )


def sanity_failure(*, lot: float, reason: str) -> str:
    return f"SANITY CHECK FAILED — signal refused.\nLot={lot:.2f}  Reason: {reason}"


def margin_warning(*, level: float, threshold: float) -> str:
    return f"MARGIN WARNING: level {level:.0f}% < {threshold:.0f}%"


def clock_drift(*, seconds: float, paused: bool) -> str:
    status = " — engine PAUSED" if paused else ""
    return f"Clock drift {seconds:.0f}s{status}"


def broker_spec_change(*, symbol: str, diffs: Mapping[str, Any]) -> str:
    rows = "\n".join(f"  {k}: {v[0]} -> {v[1]}" for k, v in diffs.items())
    return f"Broker spec change on {symbol}:\n{rows}"


def spread_regime_change(*, symbol: str, avg_pts: float, current_pts: float) -> str:
    return f"Spread regime change on {symbol}: avg={avg_pts:.0f}pts, current={current_pts:.0f}pts"


def strategy_health_alert(*, metric: str, value: float, threshold: float) -> str:
    return (
        "STRATEGY HEALTH ALERT\n"
        f"{metric}: {value:.2f} (threshold {threshold:.2f})\n"
        "Engine auto-paused."
    )


def daily_report(
    *,
    date: str,
    trades: int,
    wins: int,
    losses: int,
    pnl: float,
    win_rate: float,
    balance: float,
    max_dd: float,
) -> str:
    return (
        f"Daily Report — {date}\n"
        f"Trades: {trades}  W:{wins} L:{losses}\n"
        f"Win rate: {_fmt_pct(win_rate)}\n"
        f"P&L: {_fmt_money(pnl)} USD\n"
        f"Balance: {_fmt_money(balance)} USD\n"
        f"Max DD: {_fmt_pct(max_dd)}"
    )


def weekly_report(
    *,
    week: str,
    trades: int,
    pnl: float,
    win_rate: float,
    profit_factor: float,
    balance: float,
) -> str:
    return (
        f"Weekly Report — {week}\n"
        f"Trades: {trades}\n"
        f"Win rate: {_fmt_pct(win_rate)}\n"
        f"Profit factor: {profit_factor:.2f}\n"
        f"P&L: {_fmt_money(pnl)} USD\n"
        f"Balance: {_fmt_money(balance)} USD"
    )


def status(
    *,
    symbol: str,
    balance: float,
    equity: float,
    free_margin: float,
    margin_level: float,
    positions: int,
    regime: str,
    macro: str,
    paused: bool,
    strategy_version: str,
) -> str:
    return (
        f"Status\n"
        f"Symbol: {symbol}\n"
        f"Balance: {_fmt_money(balance)}  Equity: {_fmt_money(equity)}\n"
        f"Free margin: {_fmt_money(free_margin)}  Level: {margin_level:.0f}%\n"
        f"Open positions: {positions}\n"
        f"Regime: {regime}  Macro: {macro}\n"
        f"Paused: {paused}  v{strategy_version}"
    )


def trades_list(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "No trades recorded."
    out = ["Recent trades:"]
    for r in rows:
        ticket = r.get("ticket", "?")
        setup = r.get("setup_type", "?")
        direction = r.get("type", "?")
        entry = r.get("entry_price", 0) or 0
        exit_ = r.get("exit_price") or 0
        pnl = r.get("pnl")
        pnl_s = _fmt_money(pnl) if pnl is not None else "open"
        out.append(f"#{ticket} {direction} {setup} @ {float(entry):.2f}"
                   f" -> {float(exit_):.2f}  P&L {pnl_s}")
    return "\n".join(out)


def uptime(*, started_at: datetime, last_trade: datetime | None) -> str:
    now = datetime.now(timezone.utc)
    up = now - started_at
    last = f"{last_trade.isoformat()}" if last_trade else "none"
    return f"Uptime: {up}\nLast trade: {last}"


def version(*, version_str: str, notes: str) -> str:
    return f"Strategy v{version_str}\n{notes}"


def margin(*, level: float, free: float, used: float, usage_pct: float) -> str:
    return (
        f"Margin\n"
        f"Level: {level:.0f}%\n"
        f"Free: {_fmt_money(free)}  Used: {_fmt_money(used)}\n"
        f"Usage: {_fmt_pct(usage_pct)}"
    )


def confirm_prompt(cmd: str) -> str:
    return f"Confirm {cmd}? Reply 'yes' within 60s to proceed."


def unauthorized() -> str:
    return "Unauthorized."

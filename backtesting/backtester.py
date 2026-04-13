"""Bar-driven backtester.

Reuses core.strategy + core.regime_detector unchanged (no duplicate strategy
code — per spec). Handles execution simulation only:

  - Entry fill = next H1 bar open +/- half spread +/- random slippage in
    [0, slippage_max_pts] points (sign matches order direction)
  - SL/TP hit detection via each subsequent H1 bar's high/low
  - If both SL and TP fall inside the same bar, SL wins (conservative)
  - Swap cost applied per rollover day held (uses broker swap_long/short)
  - Partial close at 1R: close half, move remainder to BE
  - Trailing stop activates after trailing_activation_rr, follows by
    trailing_stop_distance points
  - Max trade duration: force close at max_trade_duration_hours

News + macro are stubbed out in backtest mode — the spec explicitly scopes
historical replay to execution mechanics (spread/slippage/swap/partial/
trailing), not news feed replay.

Outputs `BacktestResult` containing a list of trade dicts compatible with
analytics/performance.compute_metrics so the same report tooling works for
live + backtest data.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pandas as pd

from core.regime_detector import RegimeDetector
from core.strategy import Signal, Strategy
from utils.constants import Direction, MacroBias, Regime

logger = logging.getLogger("goldmind")


# ----------------------------------------------------------------------
@dataclass
class BacktestConfig:
    initial_balance: float = 500.0
    spread_points: float = 25.0           # simulated spread
    slippage_max_points: float = 3.0      # uniform [0, slippage_max]
    commission_per_lot: float = 0.0
    # Mirror of live keys the simulator needs
    partial_close_pct: float = 50.0       # close % at 1R
    trailing_activation_rr: float = 1.5
    trailing_stop_points: float = 150.0
    move_to_be_at_rr: float = 1.0
    max_trade_duration_hours: int = 24
    # Risk sizing
    risk_per_trade_pct: float = 1.0
    min_lot: float = 0.01
    max_lot: float = 0.10
    lot_step: float = 0.01
    # Macro / news: always allow in backtest
    assume_macro_bias: MacroBias = MacroBias.NEUTRAL
    seed: int | None = 42


# ----------------------------------------------------------------------
@dataclass
class _OpenPosition:
    ticket: int
    direction: Direction
    setup: str
    entry_price: float
    sl: float
    tp: float
    lot: float
    entry_time: datetime
    confidence: float
    regime: str
    session: str
    risk_per_unit: float
    partial_done: bool = False
    trailing_active: bool = False


@dataclass
class BacktestResult:
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    final_balance: float = 0.0
    signals_scanned: int = 0


# ----------------------------------------------------------------------
class Backtester:
    """H1-driven bar replay. H4/D1 are sliced view-wise per step."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any],           # full app config
        data: Mapping[str, pd.DataFrame],    # keyed by "H4","H1","M15","D1"
        symbol_info: Mapping[str, Any],
        bt_cfg: BacktestConfig | None = None,
    ) -> None:
        self.app_cfg = config
        self.data = {tf: df.copy().reset_index(drop=True) for tf, df in data.items()}
        self.symbol_info = dict(symbol_info)
        self.bt_cfg = bt_cfg or _bt_cfg_from_app(config)
        self._rng = random.Random(self.bt_cfg.seed)

        self.strategy = Strategy(config["strategy"], config["risk"])
        self.regime = RegimeDetector.from_config(config["regime"], config["strategy"])
        self.point = float(symbol_info.get("point", 0.01))
        self.contract_size = float(symbol_info.get("trade_contract_size", 100.0))
        self.swap_long = float(symbol_info.get("swap_long", 0.0))
        self.swap_short = float(symbol_info.get("swap_short", 0.0))

        self.balance = self.bt_cfg.initial_balance
        self._next_ticket = 1
        self._open: list[_OpenPosition] = []
        self._trades: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    def run(self) -> BacktestResult:
        h1 = self.data["H1"]
        h4 = self.data.get("H4", pd.DataFrame())
        m15 = self.data.get("M15", pd.DataFrame())
        d1 = self.data.get("D1", pd.DataFrame())

        warm = int(self.app_cfg["warm_up"]["required_bars"]["H1"])
        equity_curve: list[tuple[datetime, float]] = []
        signals_scanned = 0

        for i in range(warm, len(h1) - 1):
            current = h1.iloc[i]
            now = _as_utc(current["time"])
            next_bar = h1.iloc[i + 1]

            # 1) Manage open positions against THIS bar's range
            self._process_bar_for_open_positions(current, now)

            # 2) Apply swap once per rollover (~22:00 UTC proxy)
            if now.hour == 22 and now.minute == 0:
                self._apply_daily_swap()

            # 3) Look for new signals using closed bars up to i (inclusive)
            h1_view = h1.iloc[: i + 1]
            h4_view = _slice_by_time(h4, now) if not h4.empty else h4
            m15_view = _slice_by_time(m15, now) if not m15.empty else m15
            d1_view = _slice_by_time(d1, now) if not d1.empty else d1

            try:
                regime_reading = self.regime.detect(h1_view)
            except Exception:  # noqa: BLE001
                continue
            if regime_reading.regime not in (Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH):
                equity_curve.append((now, self.balance))
                continue

            try:
                signals = self.strategy.scan_for_signals(
                    h4=h4_view, h1=h1_view, m15=m15_view, d1=d1_view,
                    symbol_info=self.symbol_info,
                    regime=regime_reading.regime,
                    macro_bias=self.bt_cfg.assume_macro_bias,
                    asian_range=None,
                )
            except Exception:  # noqa: BLE001
                signals = []
            signals_scanned += len(signals)

            if signals and len(self._open) < int(self.app_cfg["risk"]["max_concurrent_trades"]):
                best = max(signals, key=lambda s: s.confidence)
                self._open_position(best, next_bar, now, regime_reading.regime.value)

            equity_curve.append((now, self.balance))

        # Force-close any lingering positions at the last close
        last = h1.iloc[-1]
        last_time = _as_utc(last["time"])
        for pos in list(self._open):
            self._close_position(pos, float(last["close"]), last_time, "bt_end")

        return BacktestResult(
            trades=self._trades,
            equity_curve=equity_curve,
            final_balance=self.balance,
            signals_scanned=signals_scanned,
        )

    # ------------------------------------------------------------------
    # Execution simulation
    # ------------------------------------------------------------------
    def _open_position(self, sig: Signal, next_bar: pd.Series,
                       now: datetime, regime: str) -> None:
        risk_per_unit = abs(sig.entry - sig.sl)
        if risk_per_unit <= 0:
            return
        # Simple lot sizing — not the live RiskManager (keep backtest pure)
        risk_cash = self.balance * self.bt_cfg.risk_per_trade_pct / 100.0
        point_value_per_lot = (float(self.symbol_info["trade_tick_value"])
                               / float(self.symbol_info["trade_tick_size"])
                               * self.point)
        raw_lot = risk_cash / (risk_per_unit / self.point * point_value_per_lot)
        lot = max(self.bt_cfg.min_lot,
                  min(self.bt_cfg.max_lot,
                      _floor_to_step(raw_lot, self.bt_cfg.lot_step)))

        # Fill price = next bar open +/- half spread +/- random slippage
        half_spread = (self.bt_cfg.spread_points / 2.0) * self.point
        slip_pts = self._rng.uniform(0.0, self.bt_cfg.slippage_max_points)
        slip = slip_pts * self.point
        base = float(next_bar["open"])
        if sig.direction == Direction.LONG:
            fill = base + half_spread + slip
        else:
            fill = base - half_spread - slip

        pos = _OpenPosition(
            ticket=self._next_ticket,
            direction=sig.direction,
            setup=sig.type.value,
            entry_price=fill,
            sl=sig.sl,
            tp=sig.tp,
            lot=lot,
            entry_time=_as_utc(next_bar["time"]),
            confidence=sig.confidence,
            regime=regime,
            session="bt",
            risk_per_unit=risk_per_unit,
        )
        self._next_ticket += 1
        self._open.append(pos)

    def _process_bar_for_open_positions(self, bar: pd.Series, now: datetime) -> None:
        hi = float(bar["high"])
        lo = float(bar["low"])
        close = float(bar["close"])
        for pos in list(self._open):
            # Trailing / BE / partial adjustments
            self._maybe_adjust_management(pos, hi, lo, close)

            # SL vs TP — SL wins ties (conservative)
            sl_hit, tp_hit = _sl_tp_hit(pos, hi, lo)
            if sl_hit:
                self._close_position(pos, pos.sl, now, "stop_loss")
                continue
            if tp_hit:
                self._close_position(pos, pos.tp, now, "take_profit")
                continue

            # Max duration guard
            if now - pos.entry_time >= timedelta(hours=self.bt_cfg.max_trade_duration_hours):
                self._close_position(pos, close, now, "max_duration")

    def _maybe_adjust_management(self, pos: _OpenPosition, hi: float, lo: float,
                                  close: float) -> None:
        sign = 1.0 if pos.direction == Direction.LONG else -1.0
        r = pos.risk_per_unit
        if r <= 0:
            return

        # Partial close at 1R + move remainder to BE
        if not pos.partial_done:
            trigger = pos.entry_price + sign * r * self.bt_cfg.move_to_be_at_rr
            hit = (pos.direction == Direction.LONG and hi >= trigger) or \
                  (pos.direction == Direction.SHORT and lo <= trigger)
            if hit:
                close_lot = _floor_to_step(pos.lot * self.bt_cfg.partial_close_pct / 100.0,
                                           self.bt_cfg.lot_step)
                if close_lot >= self.bt_cfg.min_lot and close_lot < pos.lot:
                    pnl = (trigger - pos.entry_price) * sign * close_lot * self.contract_size
                    self.balance += pnl
                    self._record_partial(pos, trigger, close_lot, pnl)
                    pos.lot = _floor_to_step(pos.lot - close_lot, self.bt_cfg.lot_step)
                    pos.sl = pos.entry_price  # BE
                pos.partial_done = True

        # Trailing activation
        trail_trigger = pos.entry_price + sign * r * self.bt_cfg.trailing_activation_rr
        if not pos.trailing_active:
            reached = (pos.direction == Direction.LONG and hi >= trail_trigger) or \
                      (pos.direction == Direction.SHORT and lo <= trail_trigger)
            if reached:
                pos.trailing_active = True
        if pos.trailing_active:
            trail_dist = self.bt_cfg.trailing_stop_points * self.point
            if pos.direction == Direction.LONG:
                new_sl = hi - trail_dist
                if new_sl > pos.sl:
                    pos.sl = new_sl
            else:
                new_sl = lo + trail_dist
                if new_sl < pos.sl:
                    pos.sl = new_sl

    def _close_position(self, pos: _OpenPosition, exit_price: float,
                        exit_time: datetime, reason: str) -> None:
        if pos not in self._open:
            return
        sign = 1.0 if pos.direction == Direction.LONG else -1.0
        pnl = (exit_price - pos.entry_price) * sign * pos.lot * self.contract_size
        pnl -= pos.lot * self.bt_cfg.commission_per_lot
        self.balance += pnl
        duration_min = int((exit_time - pos.entry_time).total_seconds() / 60)
        rr_achieved = ((exit_price - pos.entry_price) * sign / pos.risk_per_unit) \
            if pos.risk_per_unit > 0 else 0.0
        self._trades.append({
            "ticket": pos.ticket,
            "type": pos.direction.value,
            "setup_type": pos.setup,
            "strategy_version": str(self.app_cfg.get("strategy_version", {})
                                     .get("version", "0.0.0")),
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "stop_loss": pos.sl,
            "take_profit": pos.tp,
            "requested_lot": pos.lot,
            "filled_lot": pos.lot,
            "partial_fill": 0,
            "pnl": pnl,
            "pnl_pct": pnl / self.bt_cfg.initial_balance * 100.0,
            "swap_cost": 0.0,  # accumulated separately via _apply_daily_swap
            "commission": pos.lot * self.bt_cfg.commission_per_lot,
            "rr_achieved": rr_achieved,
            "duration_minutes": duration_min,
            "session": pos.session,
            "regime": pos.regime,
            "macro_bias": self.bt_cfg.assume_macro_bias.value,
            "confidence": pos.confidence,
            "margin_level_at_entry": 0.0,
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": exit_time.isoformat(),
            "exit_reason": reason,
            "is_backtest": 1,
            "notes": "",
        })
        self._open.remove(pos)

    def _record_partial(self, pos: _OpenPosition, price: float, lot: float,
                        pnl: float) -> None:
        self._trades.append({
            "ticket": pos.ticket,
            "type": pos.direction.value,
            "setup_type": pos.setup,
            "strategy_version": str(self.app_cfg.get("strategy_version", {})
                                     .get("version", "0.0.0")),
            "entry_price": pos.entry_price,
            "exit_price": price,
            "stop_loss": pos.sl,
            "take_profit": pos.tp,
            "requested_lot": lot,
            "filled_lot": lot,
            "partial_fill": 1,
            "pnl": pnl,
            "pnl_pct": pnl / self.bt_cfg.initial_balance * 100.0,
            "swap_cost": 0.0, "commission": 0.0,
            "rr_achieved": self.bt_cfg.move_to_be_at_rr,
            "duration_minutes": 0,
            "session": pos.session, "regime": pos.regime,
            "macro_bias": self.bt_cfg.assume_macro_bias.value,
            "confidence": pos.confidence,
            "margin_level_at_entry": 0.0,
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": "partial_at_1R",
            "is_backtest": 1,
            "notes": "",
        })

    def _apply_daily_swap(self) -> None:
        for pos in self._open:
            swap = self.swap_long if pos.direction == Direction.LONG else self.swap_short
            # Swap is broker-specific per-lot per-day — negative is a cost
            charge = swap * pos.lot
            self.balance += charge
            # Attach swap to eventual close trade via a small accumulator field
            # (kept minimal; analytics tolerates missing swap fields).


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round((value // step) * step, 8)


def _as_utc(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    dt = pd.Timestamp(ts).to_pydatetime()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slice_by_time(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if "time" not in df.columns or df.empty:
        return df
    # Row-by-row comparison is fast enough for the sizes we handle
    times = pd.to_datetime(df["time"], utc=True)
    return df.loc[times <= now].reset_index(drop=True)


def _sl_tp_hit(pos: _OpenPosition, hi: float, lo: float) -> tuple[bool, bool]:
    if pos.direction == Direction.LONG:
        return (lo <= pos.sl), (hi >= pos.tp)
    return (hi >= pos.sl), (lo <= pos.tp)


def _bt_cfg_from_app(cfg: Mapping[str, Any]) -> BacktestConfig:
    bt = cfg.get("backtesting", {})
    risk = cfg.get("risk", {})
    tm = cfg.get("trade_management", {})
    return BacktestConfig(
        initial_balance=float(bt.get("initial_balance", 500.0)),
        spread_points=float(bt.get("spread_simulation", 25.0)),
        slippage_max_points=float(bt.get("slippage_simulation_points", 3.0)),
        commission_per_lot=float(bt.get("commission_per_lot", 0.0)),
        partial_close_pct=float(risk.get("partial_close_pct", 50.0)),
        trailing_activation_rr=float(tm.get("trailing_activation_rr", 1.5)),
        trailing_stop_points=float(risk.get("trailing_stop_distance", 150.0)),
        move_to_be_at_rr=float(tm.get("move_to_be_at_rr", 1.0)),
        max_trade_duration_hours=int(tm.get("max_trade_duration_hours", 24)),
        risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 1.0)),
        min_lot=float(risk.get("min_lot_size", 0.01)),
        max_lot=float(risk.get("max_lot_size", 0.10)),
        lot_step=0.01,
    )

"""Risk Manager — capital preservation is job #1.

Layers (each is independent — the lot must survive ALL of them):
  1. can_trade()           — gates: circuit breakers, balance, margin, drawdown
  2. calculate_position_size() — risk%-based, adaptive multipliers, compounding
  3. sanity_check_lot()    — INDEPENDENT ceiling check (catches bugs above)
  4. check_margin_before_order() — wraps mt5.order_calc_margin via connector
  5. validate_signal()     — final R:R, SL bounds, spread, concurrent, duplicate
  6. handle_partial_fill() — reconcile actual vs requested fill volume
  7. update_after_trade()  — counters, P&L, drawdown, breaker state
  8. check_kill_switch()   — terminal condition: balance below floor or DD over

State is persisted to the DB so circuit breakers survive crashes/restarts.
This module performs NO MT5 calls itself — the engine/connector inject
margin and price queries via callable arguments.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from database import DBManager
from utils.constants import CircuitBreaker, Direction
from utils.helpers import clamp, point_value_per_lot, round_to_step

logger = logging.getLogger("goldmind")

STATE_KEY = "risk_manager_state"


@dataclass
class RiskState:
    """Persistent risk state — survives restarts via DB."""
    consecutive_losses: int = 0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    weekly_pnl: float = 0.0
    peak_balance: float = 0.0
    last_loss_at: str | None = None       # ISO timestamp
    cooldown_until: str | None = None     # ISO timestamp
    daily_reset_date: str | None = None   # YYYY-MM-DD
    weekly_reset_iso_week: str | None = None  # YYYY-Www
    breakers_triggered: list[str] = field(default_factory=list)
    kill_switch_active: bool = False
    base_balance: float = 0.0             # for compounding milestones
    current_risk_pct: float = 1.0         # current effective base risk

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RiskState":
        if not data:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class TradeDecision:
    ok: bool
    reason: str
    lot: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    breaker: CircuitBreaker | None = None


# Connector callables expected by the manager (kept narrow for testability)
MarginChecker = Callable[[int, str, float, float], dict[str, Any] | None]
# (order_type, symbol, lot, price) -> {required_margin, free_margin, ...}


class RiskManager:
    """Owns position sizing, circuit breakers, kill switch, and risk state."""

    def __init__(
        self,
        config: Mapping[str, Any],
        db: DBManager,
    ) -> None:
        self.cfg = config
        self.risk = config["risk"]
        self.margin_cfg = config["margin"]
        self.sanity = config["sanity_check"]
        self.cb = config["circuit_breakers"]
        self.adaptive = config["adaptive_sizing"]
        self.compounding = config["compounding"]
        self.db = db

        loaded = db.get_state(STATE_KEY)
        self.state: RiskState = RiskState.from_dict(loaded)
        if self.state.current_risk_pct <= 0:
            self.state.current_risk_pct = float(self.risk["risk_per_trade_pct"])
        self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _save(self) -> None:
        self.db.set_state(STATE_KEY, self.state.to_dict())

    # ------------------------------------------------------------------
    # Top-level gate
    # ------------------------------------------------------------------
    def can_trade(
        self,
        account_info: Mapping[str, Any],
        open_positions: Sequence[Any] = (),
        now_utc: datetime | None = None,
    ) -> TradeDecision:
        """Master gate. Returns ok=False with reason if any check fails."""
        now = now_utc or datetime.now(timezone.utc)

        if self.state.kill_switch_active:
            return TradeDecision(False, "KILL SWITCH ACTIVE", breaker=CircuitBreaker.KILL_SWITCH)

        balance = float(account_info.get("balance", 0))
        if balance < float(self.cb["min_account_balance"]):
            self._trigger_kill_switch(balance, "balance below floor")
            return TradeDecision(False, f"balance {balance} < floor {self.cb['min_account_balance']}",
                                 breaker=CircuitBreaker.MIN_BALANCE)

        # Update peak balance for drawdown math
        if balance > self.state.peak_balance:
            self.state.peak_balance = balance
            self._save()

        # Total drawdown
        if self.state.peak_balance > 0:
            dd_pct = (self.state.peak_balance - balance) / self.state.peak_balance * 100
            if dd_pct >= float(self.cb["max_total_drawdown_pct"]):
                self._trigger_kill_switch(balance, f"total DD {dd_pct:.2f}%")
                return TradeDecision(False, f"total DD {dd_pct:.2f}% >= {self.cb['max_total_drawdown_pct']}%",
                                     breaker=CircuitBreaker.TOTAL_DRAWDOWN)

        # Margin level
        margin_level = float(account_info.get("margin_level", 0) or 0)
        if margin_level and margin_level < float(self.margin_cfg["min_margin_level"]):
            return TradeDecision(False,
                                 f"margin_level {margin_level:.0f}% < min {self.margin_cfg['min_margin_level']}%")

        # Daily loss
        daily_loss_pct = self._daily_loss_pct(balance)
        if daily_loss_pct >= float(self.cb["max_daily_loss_pct"]):
            return TradeDecision(False, f"daily loss {daily_loss_pct:.2f}% >= {self.cb['max_daily_loss_pct']}%",
                                 breaker=CircuitBreaker.DAILY_LOSS)

        # Consecutive losses cooldown
        if self.state.cooldown_until:
            try:
                cooldown_end = datetime.fromisoformat(self.state.cooldown_until)
                if now < cooldown_end:
                    return TradeDecision(False, f"cooldown until {cooldown_end.isoformat()}",
                                         breaker=CircuitBreaker.CONSECUTIVE_LOSSES)
                self.state.cooldown_until = None
                self._save()
            except ValueError:
                self.state.cooldown_until = None

        # Concurrent trades
        if len(open_positions) >= int(self.risk["max_concurrent_trades"]):
            return TradeDecision(False,
                                 f"max concurrent {self.risk['max_concurrent_trades']} reached")

        return TradeDecision(True, "ok")

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    def calculate_position_size(
        self,
        balance: float,
        sl_distance_price: float,
        symbol_info: Mapping[str, Any],
        atr_ratio: float = 1.0,
    ) -> float:
        """Risk-based lot, with adaptive multipliers + compounding, then clamped/rounded.

        sl_distance_price is the price distance between entry and SL (always positive).
        atr_ratio is current ATR / trailing avg ATR (>1 means high vol; reduce sizing).
        Returns 0.0 if size would underflow min_lot.
        """
        if sl_distance_price <= 0 or balance <= 0:
            return 0.0

        # 1. Compounding-adjusted base risk
        base_risk = self._effective_risk_pct(balance)

        # 2. Adaptive multipliers (most conservative wins)
        adaptive_mult = self._adaptive_multiplier(balance, atr_ratio)

        risk_pct = base_risk * adaptive_mult
        risk_pct = min(risk_pct, float(self.risk["max_risk_per_trade_pct"]))
        risk_money = balance * (risk_pct / 100.0)

        # 3. Convert risk money -> lot using fresh point value
        pv_per_lot = point_value_per_lot(symbol_info)
        point = float(symbol_info["point"])
        sl_points = sl_distance_price / point
        if sl_points <= 0 or pv_per_lot <= 0:
            return 0.0
        raw_lot = risk_money / (sl_points * pv_per_lot)

        # 4. Clamp + round to broker step
        step = float(symbol_info.get("volume_step", 0.01))
        vmin = max(float(symbol_info.get("volume_min", 0.01)), float(self.risk["min_lot_size"]))
        vmax = min(float(symbol_info.get("volume_max", 100.0)), float(self.risk["max_lot_size"]))
        lot = round_to_step(clamp(raw_lot, 0.0, vmax), step)
        if lot < vmin:
            return 0.0
        return lot

    def _effective_risk_pct(self, balance: float) -> float:
        """Apply compounding milestones; reset to base after large drawdown."""
        if not self.compounding.get("enabled", True):
            return float(self.risk["risk_per_trade_pct"])

        base = float(self.risk["risk_per_trade_pct"])
        if self.state.base_balance <= 0:
            self.state.base_balance = balance
            self._save()
        starting = self.state.base_balance

        # Reset on large drawdown
        if (self.compounding.get("reset_to_base_after_drawdown")
                and self.state.peak_balance > 0
                and (self.state.peak_balance - balance) / self.state.peak_balance * 100 >= 10.0):
            return base

        scale_at = float(self.compounding.get("scale_up_at_pct", 20)) / 100.0
        increment = float(self.compounding.get("scale_up_increment", 0.25))
        ceiling = float(self.compounding.get("max_risk_per_trade", 2.0))
        gain_pct = (balance - starting) / starting if starting > 0 else 0.0
        if gain_pct < scale_at:
            return base
        milestones = int(gain_pct / scale_at)
        return min(base + milestones * increment, ceiling)

    def _adaptive_multiplier(self, balance: float, atr_ratio: float) -> float:
        if not self.adaptive.get("enabled", True):
            return 1.0
        mult = 1.0
        if self.state.consecutive_losses >= 3:
            mult = min(mult, float(self.adaptive["after_3_losses_multiplier"]))
        elif self.state.consecutive_losses >= 2:
            mult = min(mult, float(self.adaptive["after_2_losses_multiplier"]))
        if atr_ratio >= 1.5:
            mult = min(mult, float(self.adaptive["high_atr_multiplier"]))
        # Drawdown recovery
        if self.state.peak_balance > 0:
            dd = (self.state.peak_balance - balance) / self.state.peak_balance * 100
            if dd >= 5.0:
                mult = min(mult, float(self.adaptive["drawdown_recovery_multiplier"]))
        return mult

    # ------------------------------------------------------------------
    # Independent sanity check — catches bugs in calculation above
    # ------------------------------------------------------------------
    def sanity_check_lot(
        self,
        lot: float,
        balance: float,
        sl_distance_price: float,
        symbol_info: Mapping[str, Any],
        free_margin: float | None = None,
    ) -> tuple[float | None, str]:
        """Returns (safe_lot or None, reason). None means REFUSE TO TRADE."""
        if not self.sanity.get("enabled", True):
            return lot, "sanity check disabled"

        if lot <= 0:
            return None, "lot is zero or negative"

        ceiling = float(self.sanity["max_lot_hard_ceiling"])
        if lot > ceiling:
            return None, f"SANITY: lot {lot} > hard ceiling {ceiling}"

        try:
            pv = point_value_per_lot(symbol_info)
            point = float(symbol_info["point"])
        except (KeyError, ValueError) as e:
            return None, f"SANITY: bad symbol_info ({e})"
        sl_points = sl_distance_price / point
        risk_money = lot * sl_points * pv
        risk_pct = (risk_money / balance * 100) if balance > 0 else 999.0
        max_risk = float(self.sanity["max_risk_pct_per_position"])
        if risk_pct > max_risk:
            return None, f"SANITY: would risk {risk_pct:.2f}% > {max_risk}%"

        return lot, "ok"

    # ------------------------------------------------------------------
    # Margin pre-check (delegates to connector)
    # ------------------------------------------------------------------
    def check_margin_before_order(
        self,
        order_type: int,
        symbol: str,
        lot: float,
        price: float,
        margin_checker: MarginChecker,
    ) -> tuple[bool, str]:
        max_use = float(self.margin_cfg["max_margin_usage_pct"]) / 100.0
        try:
            res = margin_checker(order_type, symbol, lot, price)
        except Exception as e:  # noqa: BLE001
            return False, f"margin check error: {e}"
        if not res:
            return False, "margin check returned no data"
        required = float(res.get("required_margin", 0) or 0)
        free = float(res.get("free_margin", 0) or 0)
        if required <= 0 or free <= 0:
            return False, f"invalid margin numbers (req={required}, free={free})"
        if required > free * max_use:
            return False, (f"margin {required:.2f} > {max_use*100:.0f}% of free {free:.2f}")
        return True, "ok"

    # ------------------------------------------------------------------
    # Final signal validation (R:R, SL bounds, spread, duplicates)
    # ------------------------------------------------------------------
    def validate_signal(
        self,
        signal,                       # core.strategy.Signal (avoid import cycle)
        symbol_info: Mapping[str, Any],
        spread_points: float,
        open_positions: Sequence[Any] = (),
    ) -> tuple[bool, str]:
        if signal.rr_ratio < float(self.risk["min_rr_ratio"]):
            return False, f"R:R {signal.rr_ratio:.2f} < min {self.risk['min_rr_ratio']}"
        point = float(symbol_info["point"])
        sl_points = abs(signal.entry - signal.sl) / point
        if sl_points < float(self.risk["min_sl_points"]):
            return False, f"SL {sl_points:.0f}pts < min {self.risk['min_sl_points']}"
        if sl_points > float(self.risk["max_sl_points"]):
            return False, f"SL {sl_points:.0f}pts > max {self.risk['max_sl_points']}"
        if spread_points > float(self.risk["max_spread_points"]):
            return False, f"spread {spread_points:.0f}pts > max {self.risk['max_spread_points']}"

        # Duplicate direction
        for pos in open_positions:
            pos_type = self._pos_direction(pos)
            if pos_type == signal.direction:
                return False, f"already long/short ({signal.direction.value})"
        return True, "ok"

    @staticmethod
    def _pos_direction(pos: Any) -> Direction:
        # MT5 position type: 0=BUY, 1=SELL
        ptype = getattr(pos, "type", None)
        if ptype is None and isinstance(pos, Mapping):
            ptype = pos.get("type")
        if ptype == 0:
            return Direction.LONG
        if ptype == 1:
            return Direction.SHORT
        return Direction.NEUTRAL

    # ------------------------------------------------------------------
    # Partial fill reconciliation
    # ------------------------------------------------------------------
    def handle_partial_fill(
        self,
        ticket: int,
        requested_lot: float,
        actual_lot: float,
        symbol_info: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """Returns (close_immediately, reason)."""
        vmin = float(symbol_info.get("volume_min", 0.01))
        if actual_lot < vmin:
            logger.error("Partial fill ticket=%s actual=%s < min %s — close immediately",
                         ticket, actual_lot, vmin)
            return True, f"actual {actual_lot} < min {vmin}"
        if actual_lot < requested_lot:
            logger.warning("Partial fill ticket=%s requested=%s actual=%s",
                           ticket, requested_lot, actual_lot)
        return False, "ok"

    # ------------------------------------------------------------------
    # Post-trade update
    # ------------------------------------------------------------------
    def update_after_trade(
        self,
        pnl: float,
        balance_after: float,
        now_utc: datetime | None = None,
    ) -> None:
        now = now_utc or datetime.now(timezone.utc)
        self._reset_periods_if_needed(now)

        self.state.daily_pnl += pnl
        self.state.weekly_pnl += pnl
        self.state.daily_trades += 1

        if pnl < 0:
            self.state.consecutive_losses += 1
            self.state.last_loss_at = now.isoformat()
            if self.state.consecutive_losses >= int(self.cb["max_consecutive_losses"]):
                cd_h = float(self.cb["consecutive_loss_cooldown_hours"])
                self.state.cooldown_until = (now + timedelta(hours=cd_h)).isoformat()
                self._record_breaker(CircuitBreaker.CONSECUTIVE_LOSSES,
                                     f"{self.state.consecutive_losses} losses",
                                     balance_after, now)
        else:
            self.state.consecutive_losses = 0

        if balance_after > self.state.peak_balance:
            self.state.peak_balance = balance_after

        # Weekly drawdown breaker (informational — engine reduces sizing)
        if self.state.peak_balance > 0:
            dd = (self.state.peak_balance - balance_after) / self.state.peak_balance * 100
            if dd >= float(self.cb["max_weekly_drawdown_pct"]):
                self._record_breaker(CircuitBreaker.WEEKLY_DRAWDOWN,
                                     f"DD {dd:.2f}%", balance_after, now)
            if dd >= float(self.cb["max_total_drawdown_pct"]):
                self._trigger_kill_switch(balance_after, f"total DD {dd:.2f}%")

        self._save()

    # ------------------------------------------------------------------
    # Period resets
    # ------------------------------------------------------------------
    def reset_daily(self, now_utc: datetime | None = None) -> None:
        now = now_utc or datetime.now(timezone.utc)
        self.state.daily_pnl = 0.0
        self.state.daily_trades = 0
        self.state.daily_reset_date = now.date().isoformat()
        # Daily-loss breaker clears on new day
        self.state.breakers_triggered = [
            b for b in self.state.breakers_triggered if b != CircuitBreaker.DAILY_LOSS.value
        ]
        self._save()

    def reset_weekly(self, now_utc: datetime | None = None) -> None:
        now = now_utc or datetime.now(timezone.utc)
        iso_year, iso_week, _ = now.isocalendar()
        self.state.weekly_pnl = 0.0
        self.state.weekly_reset_iso_week = f"{iso_year}-W{iso_week:02d}"
        self._save()

    def _reset_periods_if_needed(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.state.daily_reset_date != today:
            self.reset_daily(now)
        iso_year, iso_week, _ = now.isocalendar()
        wkid = f"{iso_year}-W{iso_week:02d}"
        if self.state.weekly_reset_iso_week != wkid:
            self.reset_weekly(now)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    def check_kill_switch(self, balance: float, now_utc: datetime | None = None) -> bool:
        now = now_utc or datetime.now(timezone.utc)
        if balance < float(self.cb["min_account_balance"]):
            self._trigger_kill_switch(balance, "balance below floor")
            return True
        if self.state.peak_balance > 0:
            dd = (self.state.peak_balance - balance) / self.state.peak_balance * 100
            if dd >= float(self.cb["max_total_drawdown_pct"]):
                self._trigger_kill_switch(balance, f"DD {dd:.2f}%")
                return True
        return self.state.kill_switch_active

    def _trigger_kill_switch(self, balance: float, reason: str) -> None:
        if self.state.kill_switch_active:
            return
        self.state.kill_switch_active = True
        self._record_breaker(CircuitBreaker.KILL_SWITCH, reason, balance,
                             datetime.now(timezone.utc))
        logger.critical("KILL SWITCH activated: %s (balance=%s)", reason, balance)
        self._save()

    def reset_kill_switch(self) -> None:
        """Manual recovery — only via Telegram /resume after operator review."""
        self.state.kill_switch_active = False
        self._save()
        logger.warning("Kill switch manually reset")

    def _record_breaker(self, breaker: CircuitBreaker, reason: str,
                        balance: float, now: datetime) -> None:
        if breaker.value not in self.state.breakers_triggered:
            self.state.breakers_triggered.append(breaker.value)
        self.db.insert_circuit_breaker_event({
            "type": breaker.value,
            "triggered_at": now.isoformat(),
            "reason": reason,
            "balance_at_trigger": balance,
            "margin_level_at_trigger": None,
        })

    # ------------------------------------------------------------------
    def _daily_loss_pct(self, balance: float) -> float:
        """Daily loss as % of START-OF-DAY balance (approximated by current+|pnl|)."""
        if self.state.daily_pnl >= 0 or balance <= 0:
            return 0.0
        start_of_day = balance - self.state.daily_pnl  # daily_pnl is negative
        if start_of_day <= 0:
            return 0.0
        return -self.state.daily_pnl / start_of_day * 100

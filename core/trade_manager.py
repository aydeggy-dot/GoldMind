"""Trade Manager — manages OPEN positions placed by the engine.

Action plan generator. Returns a list of high-level Actions that the
engine/connector executes. Keeping execution out of this class makes it
trivial to unit-test without an MT5 connection.

Actions:
- MOVE_TO_BREAKEVEN  (modify SL to entry)
- TIGHTEN_TRAILING   (modify SL to trailing distance)
- PARTIAL_CLOSE      (close % of volume)
- CLOSE_FULL         (max duration / weekend / regime crisis / swap-aware BE)

Triggers driven by config:
- partial_close_pct, move_to_be_after_partial, move_to_be_at_rr
- trailing_activation_rr, trailing_stop_distance
- max_trade_duration_hours, friday_close_time, swap-aware close
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from utils.constants import Direction, ExitReason
from utils.helpers import parse_hhmm

logger = logging.getLogger("goldmind")


class ActionType(str, Enum):
    MOVE_TO_BREAKEVEN = "MOVE_TO_BREAKEVEN"
    TIGHTEN_TRAILING = "TIGHTEN_TRAILING"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    CLOSE_FULL = "CLOSE_FULL"


@dataclass(frozen=True)
class TradeAction:
    type: ActionType
    ticket: int
    new_sl: float | None = None
    close_volume: float | None = None
    reason: str = ""
    exit_reason: ExitReason | None = None


@dataclass
class _PositionState:
    """Mirrors what we care about in an open MT5 position."""
    ticket: int
    direction: Direction
    volume: float
    entry: float
    sl: float
    tp: float
    open_time_utc: datetime
    partial_taken: bool = False
    moved_to_be: bool = False

    @classmethod
    def from_mt5(cls, pos: Any) -> "_PositionState":
        ptype = getattr(pos, "type", None)
        direction = Direction.LONG if ptype == 0 else (
            Direction.SHORT if ptype == 1 else Direction.NEUTRAL)
        ts = getattr(pos, "time", None) or getattr(pos, "time_setup", None)
        if isinstance(ts, datetime):
            open_dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            open_dt = datetime.fromtimestamp(int(ts) if ts else 0, tz=timezone.utc)
        return cls(
            ticket=int(getattr(pos, "ticket", 0)),
            direction=direction,
            volume=float(getattr(pos, "volume", 0)),
            entry=float(getattr(pos, "price_open", 0)),
            sl=float(getattr(pos, "sl", 0) or 0),
            tp=float(getattr(pos, "tp", 0) or 0),
            open_time_utc=open_dt,
        )


class TradeManager:
    """Stateless action generator (per-call). Persistence (partial_taken/be) is
    inferred from current SL vs entry — no DB writes here."""

    def __init__(
        self,
        trade_mgmt_cfg: Mapping[str, Any],
        risk_cfg: Mapping[str, Any],
        sessions_cfg: Mapping[str, Any] | None = None,
    ) -> None:
        self.tm_cfg = trade_mgmt_cfg
        self.risk_cfg = risk_cfg
        self.sessions_cfg = sessions_cfg or {}

        self.partial_pct = float(risk_cfg.get("partial_close_pct", 50)) / 100.0
        self.move_be_after_partial = bool(risk_cfg.get("move_to_be_after_partial", True))
        self.trailing_enabled = bool(risk_cfg.get("trailing_stop_enabled", True))
        self.trailing_distance_pts = float(risk_cfg.get("trailing_stop_distance", 150))
        self.trailing_activation_rr = float(trade_mgmt_cfg.get("trailing_activation_rr", 1.5))
        self.move_be_at_rr = float(trade_mgmt_cfg.get("move_to_be_at_rr", 1.0))
        self.max_duration_hours = float(trade_mgmt_cfg.get("max_trade_duration_hours", 24))
        self.swap_aware = bool(trade_mgmt_cfg.get("swap_aware", True))
        self.swap_rollover = parse_hhmm(trade_mgmt_cfg.get("swap_rollover_time", "17:00"))
        self.close_be_before_swap = bool(trade_mgmt_cfg.get("close_breakeven_before_swap", True))
        self.close_before_weekend = bool(trade_mgmt_cfg.get("close_all_before_weekend", True))
        self.friday_close_hhmm = trade_mgmt_cfg.get("friday_close_time", "15:30")

    # ------------------------------------------------------------------
    def manage(
        self,
        positions: Sequence[Any],
        bid: float,
        ask: float,
        point: float,
        now_utc: datetime | None = None,
    ) -> list[TradeAction]:
        """Compute actions for every open position. Returns [] if nothing to do."""
        if not positions:
            return []
        now = now_utc or datetime.now(timezone.utc)
        out: list[TradeAction] = []
        for raw in positions:
            ps = _PositionState.from_mt5(raw)
            if ps.direction == Direction.NEUTRAL or point <= 0:
                continue
            current = bid if ps.direction == Direction.LONG else ask
            actions = self._actions_for(ps, current, point, now)
            out.extend(actions)
        return out

    # ------------------------------------------------------------------
    def _actions_for(
        self,
        ps: _PositionState,
        current_price: float,
        point: float,
        now: datetime,
    ) -> list[TradeAction]:
        actions: list[TradeAction] = []

        # --- Hard exits first (each terminates the chain for this position) ---
        # 1. Max duration
        age_h = (now - ps.open_time_utc).total_seconds() / 3600.0
        if age_h >= self.max_duration_hours:
            return [TradeAction(ActionType.CLOSE_FULL, ps.ticket,
                                reason=f"max duration {age_h:.1f}h",
                                exit_reason=ExitReason.MAX_DURATION)]

        # 2. Weekend close
        if self.close_before_weekend and self._is_friday_close(now):
            return [TradeAction(ActionType.CLOSE_FULL, ps.ticket,
                                reason="weekend close",
                                exit_reason=ExitReason.WEEKEND_CLOSE)]

        # 3. Swap-aware close: if at/above BE near rollover, close to skip swap
        if self.swap_aware and self._near_swap_rollover(now) and self.close_be_before_swap:
            if self._at_or_above_breakeven(ps, current_price):
                return [TradeAction(ActionType.CLOSE_FULL, ps.ticket,
                                    reason="swap-aware BE close",
                                    exit_reason=ExitReason.SWAP_CLOSE)]

        # --- Soft management (can stack) ---
        risk_pp = abs(ps.entry - ps.sl) / point if ps.sl else 0
        if risk_pp <= 0:
            return actions
        rr_now = self._current_rr(ps, current_price, point, risk_pp)

        # Partial close at >= 1.0 R if not yet taken (and SL not yet at BE)
        if (not ps.partial_taken and not self._at_or_above_breakeven(ps, current_price)
                and rr_now >= self.move_be_at_rr and self.partial_pct > 0):
            close_vol = max(round(ps.volume * self.partial_pct, 2), 0.01)
            if close_vol < ps.volume:
                actions.append(TradeAction(
                    ActionType.PARTIAL_CLOSE, ps.ticket,
                    close_volume=close_vol,
                    reason=f"R:R {rr_now:.2f} >= {self.move_be_at_rr}",
                    exit_reason=ExitReason.PARTIAL_CLOSE,
                ))
                if self.move_be_after_partial:
                    actions.append(TradeAction(
                        ActionType.MOVE_TO_BREAKEVEN, ps.ticket,
                        new_sl=ps.entry,
                        reason="post-partial BE",
                    ))
                return actions

        # Trailing stop after activation R:R reached
        if self.trailing_enabled and rr_now >= self.trailing_activation_rr:
            new_sl = self._trailing_sl(ps, current_price, point)
            if new_sl is not None and self._is_tighter(ps, new_sl):
                actions.append(TradeAction(
                    ActionType.TIGHTEN_TRAILING, ps.ticket,
                    new_sl=new_sl,
                    reason=f"trailing @ R:R {rr_now:.2f}",
                ))
                return actions

        # Move to BE at configured R:R if not done
        if (not self._at_or_above_breakeven(ps, current_price)
                and rr_now >= self.move_be_at_rr):
            actions.append(TradeAction(
                ActionType.MOVE_TO_BREAKEVEN, ps.ticket,
                new_sl=ps.entry,
                reason=f"BE at R:R {rr_now:.2f}",
            ))

        return actions

    # ------------------------------------------------------------------
    @staticmethod
    def _current_rr(ps: _PositionState, current: float, point: float,
                    risk_pp: float) -> float:
        if ps.direction == Direction.LONG:
            move_pp = (current - ps.entry) / point
        else:
            move_pp = (ps.entry - current) / point
        return move_pp / risk_pp if risk_pp > 0 else 0.0

    def _trailing_sl(self, ps: _PositionState, current: float, point: float) -> float | None:
        dist = self.trailing_distance_pts * point
        if ps.direction == Direction.LONG:
            return current - dist
        return current + dist

    @staticmethod
    def _is_tighter(ps: _PositionState, new_sl: float) -> bool:
        if ps.sl == 0:
            return True
        if ps.direction == Direction.LONG:
            return new_sl > ps.sl
        return new_sl < ps.sl

    @staticmethod
    def _at_or_above_breakeven(ps: _PositionState, current: float) -> bool:
        # SL at or beyond entry in trade direction == BE achieved
        if ps.sl == 0:
            return False
        if ps.direction == Direction.LONG:
            return ps.sl >= ps.entry
        return ps.sl <= ps.entry

    # ------------------------------------------------------------------
    def _near_swap_rollover(self, now_utc: datetime) -> bool:
        # Rollover is broker-time but we use NY-tz as approximation (most brokers)
        from zoneinfo import ZoneInfo
        ny_tz = ZoneInfo(self.sessions_cfg.get("new_york", {}).get("timezone", "America/New_York"))
        local = now_utc.astimezone(ny_tz).time()
        # 10-min window before rollover
        roll = self.swap_rollover
        roll_minutes = roll.hour * 60 + roll.minute
        local_minutes = local.hour * 60 + local.minute
        delta = roll_minutes - local_minutes
        return 0 <= delta <= 10

    def _is_friday_close(self, now_utc: datetime) -> bool:
        from zoneinfo import ZoneInfo
        ny_tz = ZoneInfo(self.sessions_cfg.get("new_york", {}).get("timezone", "America/New_York"))
        local = now_utc.astimezone(ny_tz)
        if local.weekday() != 4:
            return False
        return local.time() >= parse_hhmm(self.friday_close_hhmm)

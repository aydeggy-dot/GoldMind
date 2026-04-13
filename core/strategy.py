"""Strategy engine — pure signal generation.

Inputs: CLOSED OHLC DataFrames per timeframe (must have passed DataValidator).
Outputs: List of Signal candidates, each scored with a confidence in [0, 1].

Three setups (all spec'd in GOLDMIND_PROMPT.md):
  A. SWEEP_REVERSAL    — liquidity sweep at key level + M15 structure shift
  B. TREND_CONTINUATION — H1 pullback to fast EMA in H4 bias direction
  C. FLAG_BREAKOUT     — consolidation flag after >2x ATR impulse, breakout

This module is PURE: no MT5 calls, no order placement, no risk math.
The risk_manager validates each signal before execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from utils.constants import Direction, MacroBias, Regime, SetupType

logger = logging.getLogger("goldmind")


@dataclass(frozen=True)
class KeyLevel:
    name: str           # e.g. "PDH", "Asian_H", "Psych_2050"
    price: float
    fresh: bool = True  # True if not touched in the last N candles


@dataclass
class Signal:
    type: SetupType
    direction: Direction
    entry: float
    sl: float
    tp: float
    rr_ratio: float
    confidence: float
    h4_aligned: bool
    macro_aligned: bool
    reasoning: str = ""
    # Diagnostics
    setup_score: dict = field(default_factory=dict)


class Strategy:
    """Stateless strategy engine."""

    PSYCH_STEP = 50.0   # gold psych levels every $50 ($2000, $2050, ...)
    MIN_CONFIDENCE = 0.60

    def __init__(self, strategy_cfg: Mapping, risk_cfg: Mapping) -> None:
        self.cfg = strategy_cfg
        self.risk_cfg = risk_cfg
        self.fast_ema = strategy_cfg["fast_ema"]
        self.slow_ema = strategy_cfg["slow_ema"]
        self.bias_ema_period = strategy_cfg["bias_ema_period"]
        self.level_tolerance_points = strategy_cfg["level_touch_tolerance"]
        self.lookback_days = strategy_cfg["lookback_days"]
        self.min_rr = risk_cfg["min_rr_ratio"]
        self.target_rr = risk_cfg["target_rr_ratio"]
        self.min_sl_points = risk_cfg["min_sl_points"]
        self.max_sl_points = risk_cfg["max_sl_points"]
        self.wiggle_points = risk_cfg["wiggle_room_points"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_h4_bias(self, h4: pd.DataFrame) -> Direction:
        """Determine H4 bias from price vs 200 EMA, with 3-bar confirmation."""
        if h4 is None or len(h4) < self.bias_ema_period + 3:
            return Direction.NEUTRAL
        ema = EMAIndicator(close=h4["close"].astype(float),
                           window=self.bias_ema_period, fillna=False).ema_indicator()
        last = h4["close"].iloc[-3:]
        ema_last = ema.iloc[-3:]
        if (last > ema_last).all():
            return Direction.LONG
        if (last < ema_last).all():
            return Direction.SHORT
        return Direction.NEUTRAL

    def calculate_key_levels(
        self,
        d1: pd.DataFrame,
        h1: pd.DataFrame,
        asian_range: tuple[float, float] | None,
        point: float,
    ) -> list[KeyLevel]:
        """PDH/PDL + Asian H/L + recent swings + psychological levels."""
        levels: list[KeyLevel] = []

        if d1 is not None and len(d1) >= 2:
            prev = d1.iloc[-2]
            levels.append(KeyLevel("PDH", float(prev["high"])))
            levels.append(KeyLevel("PDL", float(prev["low"])))

        if asian_range:
            lo, hi = asian_range
            levels.append(KeyLevel("Asian_L", lo))
            levels.append(KeyLevel("Asian_H", hi))

        if h1 is not None and len(h1) >= 20:
            recent = h1.tail(self.lookback_days * 24)
            swing_high = float(recent["high"].max())
            swing_low = float(recent["low"].min())
            levels.append(KeyLevel("Swing_H", swing_high))
            levels.append(KeyLevel("Swing_L", swing_low))

        # Psychological levels around current price
        if h1 is not None and not h1.empty:
            now_price = float(h1["close"].iloc[-1])
            base = round(now_price / self.PSYCH_STEP) * self.PSYCH_STEP
            for offset in (-2, -1, 0, 1, 2):
                p = base + offset * self.PSYCH_STEP
                levels.append(KeyLevel(f"Psych_{int(p)}", p))

        # Mark fresh = not touched in last 5 H1 bars
        if h1 is not None and len(h1) >= 5:
            recent = h1.tail(5)
            tol = self.level_tolerance_points * point
            for i, lvl in enumerate(levels):
                touched = ((recent["high"] >= lvl.price - tol) & (recent["low"] <= lvl.price + tol)).any()
                if touched:
                    levels[i] = KeyLevel(lvl.name, lvl.price, fresh=False)
        return levels

    def scan_for_signals(
        self,
        h4: pd.DataFrame,
        h1: pd.DataFrame,
        m15: pd.DataFrame,
        d1: pd.DataFrame,
        symbol_info: Mapping,
        regime: Regime,
        macro_bias: MacroBias,
        asian_range: tuple[float, float] | None,
    ) -> list[Signal]:
        """Run every enabled setup and return scored signals (>= MIN_CONFIDENCE)."""
        point = float(symbol_info["point"])
        h4_bias = self.get_h4_bias(h4)
        levels = self.calculate_key_levels(d1, h1, asian_range, point)
        atr = self._atr(h1, period=14)

        signals: list[Signal] = []

        if self.cfg.get("enable_sweep_reversal", True):
            sig = self._setup_sweep_reversal(m15, levels, point, atr, h4_bias, macro_bias, regime)
            if sig:
                signals.append(sig)

        if self.cfg.get("enable_trend_continuation", True):
            sig = self._setup_trend_continuation(h1, point, atr, h4_bias, macro_bias, regime)
            if sig:
                signals.append(sig)

        if self.cfg.get("enable_flag_breakout", True):
            sig = self._setup_flag_breakout(h1, point, atr, h4_bias, macro_bias, regime)
            if sig:
                signals.append(sig)

        return [s for s in signals if s.confidence >= self.MIN_CONFIDENCE]

    # ------------------------------------------------------------------
    # Setup A: Sweep & Reversal
    # ------------------------------------------------------------------
    def _setup_sweep_reversal(
        self,
        m15: pd.DataFrame,
        levels: list[KeyLevel],
        point: float,
        atr_h1: float,
        h4_bias: Direction,
        macro: MacroBias,
        regime: Regime,
    ) -> Signal | None:
        if m15 is None or len(m15) < 10 or not levels or atr_h1 <= 0:
            return None
        last = m15.iloc[-1]
        prev = m15.iloc[-2]
        tol = self.level_tolerance_points * point
        body_top = max(last["open"], last["close"])
        body_bot = min(last["open"], last["close"])

        for lvl in levels:
            # Bullish sweep: wick BELOW level, body closes ABOVE it
            if (last["low"] < lvl.price - tol) and (body_bot > lvl.price - tol):
                # Structure shift: close > prior swing high (last 5 bars)
                prior_high = m15["high"].iloc[-6:-1].max()
                if last["close"] > prior_high:
                    entry = float(last["close"])
                    sl = float(last["low"]) - self.wiggle_points * point
                    sl_dist = entry - sl
                    if not self._sl_within_bounds(sl_dist, point):
                        continue
                    tp = entry + sl_dist * self.target_rr
                    return self._build_signal(
                        SetupType.SWEEP_REVERSAL, Direction.LONG, entry, sl, tp,
                        h4_bias, macro, regime, lvl, fresh_zone=lvl.fresh,
                        reasoning=f"Bullish sweep of {lvl.name} @ {lvl.price:.2f}",
                    )

            # Bearish sweep: wick ABOVE level, body closes BELOW it
            if (last["high"] > lvl.price + tol) and (body_top < lvl.price + tol):
                prior_low = m15["low"].iloc[-6:-1].min()
                if last["close"] < prior_low:
                    entry = float(last["close"])
                    sl = float(last["high"]) + self.wiggle_points * point
                    sl_dist = sl - entry
                    if not self._sl_within_bounds(sl_dist, point):
                        continue
                    tp = entry - sl_dist * self.target_rr
                    return self._build_signal(
                        SetupType.SWEEP_REVERSAL, Direction.SHORT, entry, sl, tp,
                        h4_bias, macro, regime, lvl, fresh_zone=lvl.fresh,
                        reasoning=f"Bearish sweep of {lvl.name} @ {lvl.price:.2f}",
                    )
        return None

    # ------------------------------------------------------------------
    # Setup B: Trend Continuation (EMA pullback)
    # ------------------------------------------------------------------
    def _setup_trend_continuation(
        self,
        h1: pd.DataFrame,
        point: float,
        atr_h1: float,
        h4_bias: Direction,
        macro: MacroBias,
        regime: Regime,
    ) -> Signal | None:
        if h1 is None or len(h1) < self.slow_ema + 5 or h4_bias == Direction.NEUTRAL or atr_h1 <= 0:
            return None
        close = h1["close"].astype(float)
        fast = EMAIndicator(close=close, window=self.fast_ema, fillna=False).ema_indicator()
        slow = EMAIndicator(close=close, window=self.slow_ema, fillna=False).ema_indicator()
        last = h1.iloc[-1]
        fast_last = float(fast.iloc[-1])

        if h4_bias == Direction.LONG and float(slow.iloc[-1]) < fast_last:
            # Pullback: low touches/penetrates fast EMA, close back above with bullish body
            if last["low"] <= fast_last and last["close"] > fast_last and last["close"] > last["open"]:
                entry = float(last["close"])
                sl = float(last["low"]) - self.wiggle_points * point
                sl_dist = entry - sl
                if not self._sl_within_bounds(sl_dist, point):
                    return None
                tp = entry + sl_dist * self.target_rr
                return self._build_signal(
                    SetupType.TREND_CONTINUATION, Direction.LONG, entry, sl, tp,
                    h4_bias, macro, regime, key_level=None, fresh_zone=False,
                    reasoning=f"Bullish pullback to H1 EMA{self.fast_ema}",
                )

        if h4_bias == Direction.SHORT and float(slow.iloc[-1]) > fast_last:
            if last["high"] >= fast_last and last["close"] < fast_last and last["close"] < last["open"]:
                entry = float(last["close"])
                sl = float(last["high"]) + self.wiggle_points * point
                sl_dist = sl - entry
                if not self._sl_within_bounds(sl_dist, point):
                    return None
                tp = entry - sl_dist * self.target_rr
                return self._build_signal(
                    SetupType.TREND_CONTINUATION, Direction.SHORT, entry, sl, tp,
                    h4_bias, macro, regime, key_level=None, fresh_zone=False,
                    reasoning=f"Bearish pullback to H1 EMA{self.fast_ema}",
                )
        return None

    # ------------------------------------------------------------------
    # Setup C: Flag Breakout
    # ------------------------------------------------------------------
    def _setup_flag_breakout(
        self,
        h1: pd.DataFrame,
        point: float,
        atr_h1: float,
        h4_bias: Direction,
        macro: MacroBias,
        regime: Regime,
    ) -> Signal | None:
        if h1 is None or len(h1) < 20 or atr_h1 <= 0:
            return None

        # Look for impulse > 2x ATR over 1-2 bars, then 3-6 bar consolidation, then breakout
        for impulse_idx in range(-10, -4):  # impulse ends 4-10 bars ago
            impulse_bar = h1.iloc[impulse_idx]
            move = float(impulse_bar["close"] - impulse_bar["open"])
            if abs(move) < 2 * atr_h1:
                continue
            direction = Direction.LONG if move > 0 else Direction.SHORT
            flag = h1.iloc[impulse_idx + 1:-1]
            if len(flag) < 3:
                continue
            flag_high = float(flag["high"].max())
            flag_low = float(flag["low"].min())
            flag_range = flag_high - flag_low
            if flag_range > abs(move) * 0.6:   # consolidation must be tight
                continue

            last = h1.iloc[-1]
            if direction == Direction.LONG and last["close"] > flag_high:
                entry = float(last["close"])
                sl = flag_low - self.wiggle_points * point
                sl_dist = entry - sl
                if not self._sl_within_bounds(sl_dist, point):
                    continue
                tp = entry + sl_dist * self.target_rr
                return self._build_signal(
                    SetupType.FLAG_BREAKOUT, Direction.LONG, entry, sl, tp,
                    h4_bias, macro, regime, key_level=None, fresh_zone=True,
                    reasoning=f"Bullish flag breakout above {flag_high:.2f}",
                )
            if direction == Direction.SHORT and last["close"] < flag_low:
                entry = float(last["close"])
                sl = flag_high + self.wiggle_points * point
                sl_dist = sl - entry
                if not self._sl_within_bounds(sl_dist, point):
                    continue
                tp = entry - sl_dist * self.target_rr
                return self._build_signal(
                    SetupType.FLAG_BREAKOUT, Direction.SHORT, entry, sl, tp,
                    h4_bias, macro, regime, key_level=None, fresh_zone=True,
                    reasoning=f"Bearish flag breakout below {flag_low:.2f}",
                )
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_signal(
        self,
        setup: SetupType,
        direction: Direction,
        entry: float,
        sl: float,
        tp: float,
        h4_bias: Direction,
        macro: MacroBias,
        regime: Regime,
        key_level: KeyLevel | None,
        fresh_zone: bool,
        reasoning: str,
    ) -> Signal:
        h4_aligned = (h4_bias == direction)
        macro_aligned = self._macro_aligned(direction, macro)
        rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)

        score = {"base": 0.5}
        if h4_aligned:
            score["h4_align"] = 0.15
        else:
            score["counter_trend"] = -0.20
        if macro_aligned:
            score["macro_align"] = 0.15
        elif macro == MacroBias.CONFLICTING:
            score["macro_conflict"] = -0.15
        if fresh_zone:
            score["fresh_zone"] = 0.10
        if regime in (Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH) and h4_aligned:
            score["trend_confluence"] = 0.10

        confidence = float(np.clip(sum(score.values()), 0.0, 1.0))
        return Signal(
            type=setup, direction=direction, entry=entry, sl=sl, tp=tp,
            rr_ratio=rr, confidence=confidence,
            h4_aligned=h4_aligned, macro_aligned=macro_aligned,
            reasoning=reasoning, setup_score=score,
        )

    def _macro_aligned(self, direction: Direction, macro: MacroBias) -> bool:
        if direction == Direction.LONG:
            return macro == MacroBias.BULLISH
        if direction == Direction.SHORT:
            return macro == MacroBias.BEARISH
        return False

    def _sl_within_bounds(self, sl_distance_price: float, point: float) -> bool:
        sl_points = sl_distance_price / point
        return self.min_sl_points <= sl_points <= self.max_sl_points

    @staticmethod
    def _atr(h1: pd.DataFrame, period: int = 14) -> float:
        if h1 is None or len(h1) < period + 1:
            return 0.0
        ind = AverageTrueRange(high=h1["high"].astype(float),
                               low=h1["low"].astype(float),
                               close=h1["close"].astype(float),
                               window=period, fillna=False)
        v = ind.average_true_range().iloc[-1]
        return float(v) if not pd.isna(v) else 0.0

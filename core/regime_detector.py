"""Regime detection — classify market state from ADX + ATR + EMA alignment.

Regimes drive whether to trade at all:
- TRENDING_BULLISH/BEARISH: trade in trend direction
- RANGING: skip new trades (mean-reversion strategies not in scope)
- VOLATILE_CRISIS: skip + tighten existing stops
- TRANSITIONING: A+ setups only, reduced confidence
- UNKNOWN: not enough data — refuse to trade
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange

from utils.constants import Regime

logger = logging.getLogger("goldmind")


@dataclass(frozen=True)
class RegimeReading:
    regime: Regime
    adx: float
    atr: float
    atr_ratio: float        # current ATR vs trailing average
    ema_fast: float
    ema_slow: float
    confirmation_bars: int  # how many recent bars satisfy the regime
    reason: str


class RegimeDetector:
    """Stateless regime classifier (caller persists last reading if needed)."""

    def __init__(
        self,
        adx_period: int = 14,
        adx_trending_threshold: float = 25.0,
        adx_ranging_threshold: float = 20.0,
        atr_period: int = 14,
        atr_spike_multiplier: float = 2.0,
        confirmation_bars: int = 3,
        fast_ema: int = 50,
        slow_ema: int = 200,
    ) -> None:
        self.adx_period = adx_period
        self.adx_trending = adx_trending_threshold
        self.adx_ranging = adx_ranging_threshold
        self.atr_period = atr_period
        self.atr_spike_multiplier = atr_spike_multiplier
        self.confirmation_bars = confirmation_bars
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema

    @classmethod
    def from_config(cls, regime_cfg: Mapping, strategy_cfg: Mapping) -> "RegimeDetector":
        return cls(
            adx_period=regime_cfg["adx_period"],
            adx_trending_threshold=regime_cfg["adx_trending_threshold"],
            adx_ranging_threshold=regime_cfg["adx_ranging_threshold"],
            atr_period=regime_cfg["atr_period"],
            atr_spike_multiplier=regime_cfg["atr_spike_multiplier"],
            confirmation_bars=regime_cfg["regime_confirmation_bars"],
            fast_ema=strategy_cfg["fast_ema"],
            slow_ema=strategy_cfg["slow_ema"],
        )

    # ------------------------------------------------------------------
    def detect(self, df: pd.DataFrame) -> RegimeReading:
        """Classify regime from a CLOSED-candles OHLC DataFrame.

        Expects columns: open, high, low, close. Use H1 or H4 typically.
        """
        min_bars = max(self.slow_ema, self.adx_period * 3, self.atr_period * 3)
        if df is None or len(df) < min_bars:
            return RegimeReading(Regime.UNKNOWN, 0, 0, 0, 0, 0, 0,
                                 f"insufficient bars ({len(df) if df is not None else 0}/{min_bars})")

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        adx_ind = ADXIndicator(high=high, low=low, close=close,
                               window=self.adx_period, fillna=False)
        adx = adx_ind.adx()
        atr_ind = AverageTrueRange(high=high, low=low, close=close,
                                   window=self.atr_period, fillna=False)
        atr = atr_ind.average_true_range()
        ema_fast = EMAIndicator(close=close, window=self.fast_ema, fillna=False).ema_indicator()
        ema_slow = EMAIndicator(close=close, window=self.slow_ema, fillna=False).ema_indicator()

        adx_now = float(adx.iloc[-1])
        atr_now = float(atr.iloc[-1])
        atr_avg = float(atr.tail(self.atr_period * 3).mean())
        atr_ratio = atr_now / atr_avg if atr_avg > 0 else 0.0
        fast_now = float(ema_fast.iloc[-1])
        slow_now = float(ema_slow.iloc[-1])
        close_now = float(close.iloc[-1])

        # 1. Volatility crisis takes precedence
        if atr_ratio >= self.atr_spike_multiplier:
            return RegimeReading(
                Regime.VOLATILE_CRISIS, adx_now, atr_now, atr_ratio,
                fast_now, slow_now, 0,
                f"ATR spike {atr_ratio:.2f}x avg",
            )

        # 2. Confirmation: last N bars must agree on trending vs ranging
        last_adx = adx.tail(self.confirmation_bars)
        trending_bars = int((last_adx > self.adx_trending).sum())
        ranging_bars = int((last_adx < self.adx_ranging).sum())

        # 3. Trending
        if trending_bars >= self.confirmation_bars:
            if close_now > slow_now and fast_now > slow_now:
                return RegimeReading(
                    Regime.TRENDING_BULLISH, adx_now, atr_now, atr_ratio,
                    fast_now, slow_now, trending_bars,
                    "ADX>thresh + price>EMA200 + fast>slow",
                )
            if close_now < slow_now and fast_now < slow_now:
                return RegimeReading(
                    Regime.TRENDING_BEARISH, adx_now, atr_now, atr_ratio,
                    fast_now, slow_now, trending_bars,
                    "ADX>thresh + price<EMA200 + fast<slow",
                )
            return RegimeReading(
                Regime.TRANSITIONING, adx_now, atr_now, atr_ratio,
                fast_now, slow_now, trending_bars,
                "ADX trending but EMAs not aligned",
            )

        # 4. Ranging
        if ranging_bars >= self.confirmation_bars:
            return RegimeReading(
                Regime.RANGING, adx_now, atr_now, atr_ratio,
                fast_now, slow_now, ranging_bars,
                f"ADX<{self.adx_ranging} for {ranging_bars} bars",
            )

        # 5. Otherwise transitioning
        return RegimeReading(
            Regime.TRANSITIONING, adx_now, atr_now, atr_ratio,
            fast_now, slow_now, 0,
            f"ADX={adx_now:.1f} between thresholds",
        )

    @staticmethod
    def should_trade(reading: RegimeReading) -> bool:
        """True if the regime allows opening new trades."""
        return reading.regime in (Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH)

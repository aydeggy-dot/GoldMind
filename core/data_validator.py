"""Candle + tick data validator.

Run BEFORE any strategy calculation. Garbage in = bad signals = lost money.
Returns (ok, reason). The caller MUST refuse to trade on a False result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger("goldmind")


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


# Approximate timeframe -> minutes
_TF_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440, "W1": 10080,
}


class DataValidator:
    """Validates OHLCV DataFrames and ticks before they reach strategy logic."""

    OHLC_COLS = ("open", "high", "low", "close")

    def __init__(
        self,
        max_candle_age_minutes: int = 30,
        max_price_gap_pct: float = 5.0,
        reject_zero_prices: bool = True,
        reject_nan_values: bool = True,
        reject_high_less_than_low: bool = True,
        require_volume: bool = True,
        max_tick_age_seconds: int = 60,
        max_tick_spread_points: int = 100,
    ) -> None:
        self.max_candle_age_minutes = max_candle_age_minutes
        self.max_price_gap_pct = max_price_gap_pct
        self.reject_zero_prices = reject_zero_prices
        self.reject_nan_values = reject_nan_values
        self.reject_high_less_than_low = reject_high_less_than_low
        self.require_volume = require_volume
        self.max_tick_age_seconds = max_tick_age_seconds
        self.max_tick_spread_points = max_tick_spread_points

    # ------------------------------------------------------------------
    # Candle DataFrame validation
    # ------------------------------------------------------------------
    def validate_candles(
        self,
        df: pd.DataFrame,
        timeframe: str,
        symbol: str,
        now_utc: datetime | None = None,
    ) -> ValidationResult:
        """Return (False, reason) if data is unusable for trading."""
        if df is None or df.empty:
            return self._fail(symbol, timeframe, "empty dataframe")

        for col in self.OHLC_COLS:
            if col not in df.columns:
                return self._fail(symbol, timeframe, f"missing column: {col}")
        if "time" not in df.columns:
            return self._fail(symbol, timeframe, "missing 'time' column")

        ohlc = df[list(self.OHLC_COLS)]

        if self.reject_nan_values and ohlc.isna().any().any():
            return self._fail(symbol, timeframe, "NaN in OHLC")

        if self.reject_zero_prices and (ohlc <= 0).any().any():
            return self._fail(symbol, timeframe, "zero or negative price")

        if self.reject_high_less_than_low:
            if (df["high"] < df["low"]).any():
                return self._fail(symbol, timeframe, "high < low (impossible candle)")
            if (df["high"] < df["open"]).any() or (df["high"] < df["close"]).any():
                return self._fail(symbol, timeframe, "high < open/close")
            if (df["low"] > df["open"]).any() or (df["low"] > df["close"]).any():
                return self._fail(symbol, timeframe, "low > open/close")

        if self.require_volume and "tick_volume" in df.columns:
            if (df["tick_volume"] <= 0).any():
                return self._fail(symbol, timeframe, "zero tick_volume (dead feed)")

        # Monotonic timestamps
        times = pd.to_datetime(df["time"], utc=True)
        if not times.is_monotonic_increasing:
            return self._fail(symbol, timeframe, "timestamps not monotonic")

        # Stale-data check
        now_utc = now_utc or datetime.now(timezone.utc)
        last_bar = times.iloc[-1].to_pydatetime()
        bar_minutes = _TF_MINUTES.get(timeframe, 60)
        max_age = max(self.max_candle_age_minutes, bar_minutes * 2)
        age_minutes = (now_utc - last_bar).total_seconds() / 60.0
        if age_minutes > max_age:
            return self._fail(
                symbol, timeframe,
                f"stale data: last bar {age_minutes:.1f}min old (max {max_age})",
            )

        # Gap check on consecutive closes
        closes = df["close"].astype(float).to_numpy()
        if len(closes) >= 2:
            prev = closes[:-1]
            curr = closes[1:]
            with np.errstate(divide="ignore", invalid="ignore"):
                pct = np.where(prev > 0, np.abs(curr - prev) / prev * 100.0, 0.0)
            if np.any(pct > self.max_price_gap_pct):
                idx = int(np.argmax(pct))
                return self._fail(
                    symbol, timeframe,
                    f"price gap {pct[idx]:.2f}% > {self.max_price_gap_pct}% at row {idx}",
                )

        return ValidationResult(True, "ok")

    # ------------------------------------------------------------------
    # Tick validation
    # ------------------------------------------------------------------
    def validate_tick(
        self,
        tick: Mapping[str, Any] | None,
        point: float,
        now_utc: datetime | None = None,
    ) -> ValidationResult:
        """Validate a single tick before using it for order placement."""
        if tick is None:
            return ValidationResult(False, "tick is None")

        bid = float(tick.get("bid", 0) or 0)
        ask = float(tick.get("ask", 0) or 0)
        if bid <= 0 or ask <= 0:
            return ValidationResult(False, f"non-positive bid/ask (bid={bid}, ask={ask})")
        if ask < bid:
            return ValidationResult(False, f"ask {ask} < bid {bid}")

        if point > 0:
            spread_points = (ask - bid) / point
            if spread_points > self.max_tick_spread_points:
                return ValidationResult(
                    False, f"tick spread {spread_points:.0f}pts > {self.max_tick_spread_points}",
                )

        ts = tick.get("time")
        if ts:
            now_utc = now_utc or datetime.now(timezone.utc)
            tick_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            age = (now_utc - tick_dt).total_seconds()
            if age > self.max_tick_age_seconds:
                return ValidationResult(
                    False, f"tick {age:.0f}s old (max {self.max_tick_age_seconds})",
                )

        return ValidationResult(True, "ok")

    # ------------------------------------------------------------------
    def _fail(self, symbol: str, timeframe: str, reason: str) -> ValidationResult:
        logger.error("Data validation failed [%s %s]: %s", symbol, timeframe, reason)
        return ValidationResult(False, reason)

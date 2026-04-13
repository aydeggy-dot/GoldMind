"""Macro context filter — DXY, US10Y, VIX.

Gold's price is heavily driven by:
- USD strength (DXY, inverse correlation)
- Real yields (US10Y, inverse correlation)
- Risk sentiment (VIX, mixed — risk-on usually weak gold, risk-off strong gold)

Symbol discovery cascade per macro asset:
  primary -> fallbacks -> yfinance web API (cached)

Returns a synthesized MacroBias used to gate signals in the strategy engine.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import pandas as pd
from ta.trend import EMAIndicator

from utils.constants import MacroBias

logger = logging.getLogger("goldmind")


@dataclass
class MacroReading:
    bias: MacroBias
    dxy_direction: MacroBias = MacroBias.NEUTRAL
    yield_direction: MacroBias = MacroBias.NEUTRAL
    vix_level: float = 0.0
    vix_state: str = "normal"
    sources: dict[str, str] = field(default_factory=dict)
    reason: str = ""


class _Cache:
    """Tiny TTL cache for web-API macro values."""
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, pd.DataFrame]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl_seconds: float) -> pd.DataFrame | None:
        with self._lock:
            entry = self._data.get(key)
        if not entry:
            return None
        ts, df = entry
        if time.time() - ts > ttl_seconds:
            return None
        return df

    def put(self, key: str, df: pd.DataFrame) -> None:
        with self._lock:
            self._data[key] = (time.time(), df)


# Type alias for an MT5-shaped fetcher: (symbol, timeframe, count) -> DataFrame
BrokerFetcher = Callable[[str, str, int], pd.DataFrame]


class MacroFilter:
    """Reads macro context using broker symbols, falling back to yfinance."""

    YF_TICKERS = {
        "dxy": "DX-Y.NYB",
        "us10y": "^TNX",
        "vix": "^VIX",
    }

    def __init__(
        self,
        macro_cfg: Mapping,
        broker_fetcher: BrokerFetcher | None = None,
        symbol_resolver: Callable[[str, Sequence[str]], str | None] | None = None,
        web_fetcher: Callable[[str, int], pd.DataFrame] | None = None,
    ) -> None:
        self.cfg = macro_cfg
        self._broker_fetch = broker_fetcher
        self._resolve = symbol_resolver
        self._web_fetch = web_fetcher or self._default_web_fetch
        self._cache = _Cache()
        self._cache_ttl_s = float(macro_cfg.get("web_api_cache_minutes", 15)) * 60

        self._resolved: dict[str, tuple[str, str]] = {}  # asset -> (symbol, source)

    # ------------------------------------------------------------------
    def evaluate(self) -> MacroReading:
        """Synthesize a MacroBias from DXY, yields, VIX."""
        if not self.cfg.get("enabled", True):
            return MacroReading(MacroBias.NEUTRAL, reason="macro disabled")

        dxy_dir = self._eval_dxy() if self.cfg.get("dxy_filter_enabled", True) else MacroBias.NEUTRAL
        yld_dir = self._eval_yield() if self.cfg.get("us10y_filter_enabled", True) else MacroBias.NEUTRAL
        vix_level, vix_state = self._eval_vix() if self.cfg.get("vix_filter_enabled", True) else (0.0, "normal")

        # Map to gold bias.
        # DXY up   -> gold bearish | DXY down -> gold bullish
        # Yield up -> gold bearish | Yield down -> gold bullish
        # VIX extreme -> gold bullish (safe haven) | VIX risk-off -> bullish | normal -> neutral
        gold_dxy = self._invert(dxy_dir)
        gold_yld = self._invert(yld_dir)
        gold_vix = MacroBias.BULLISH if vix_state in ("risk_off", "extreme") else MacroBias.NEUTRAL

        votes = [b for b in (gold_dxy, gold_yld, gold_vix) if b != MacroBias.NEUTRAL]
        if not votes:
            bias = MacroBias.NEUTRAL
            reason = "all macro inputs neutral"
        else:
            bull = votes.count(MacroBias.BULLISH)
            bear = votes.count(MacroBias.BEARISH)
            if bull and bear:
                bias = MacroBias.CONFLICTING
                reason = f"{bull} bullish vs {bear} bearish votes"
            elif bull >= bear:
                bias = MacroBias.BULLISH
                reason = f"{bull} bullish votes"
            else:
                bias = MacroBias.BEARISH
                reason = f"{bear} bearish votes"

        return MacroReading(
            bias=bias,
            dxy_direction=dxy_dir,
            yield_direction=yld_dir,
            vix_level=vix_level,
            vix_state=vix_state,
            sources={k: v[1] for k, v in self._resolved.items()},
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Per-asset evaluators
    # ------------------------------------------------------------------
    def _eval_dxy(self) -> MacroBias:
        df = self._fetch("dxy",
                         self.cfg["dxy_symbol"], self.cfg.get("dxy_symbol_fallbacks", []),
                         "H1", count=max(self.cfg["dxy_ema_period"] * 3, 100))
        if df is None or df.empty:
            return MacroBias.NEUTRAL
        ema = EMAIndicator(close=df["close"].astype(float),
                           window=self.cfg["dxy_ema_period"], fillna=False).ema_indicator()
        if pd.isna(ema.iloc[-1]):
            return MacroBias.NEUTRAL
        return MacroBias.BULLISH if df["close"].iloc[-1] > ema.iloc[-1] else MacroBias.BEARISH

    def _eval_yield(self) -> MacroBias:
        df = self._fetch("us10y",
                         self.cfg["us10y_symbol"], self.cfg.get("us10y_symbol_fallbacks", []),
                         "D1", count=10)
        if df is None or len(df) < 2:
            return MacroBias.NEUTRAL
        prev = float(df["close"].iloc[-2])
        now = float(df["close"].iloc[-1])
        threshold = float(self.cfg.get("yield_change_threshold", 0.05))
        if now - prev > threshold:
            return MacroBias.BULLISH       # yields up
        if prev - now > threshold:
            return MacroBias.BEARISH       # yields down
        return MacroBias.NEUTRAL

    def _eval_vix(self) -> tuple[float, str]:
        df = self._fetch("vix",
                         self.cfg["vix_symbol"], self.cfg.get("vix_symbol_fallbacks", []),
                         "D1", count=5)
        if df is None or df.empty:
            return 0.0, "unknown"
        level = float(df["close"].iloc[-1])
        if level >= float(self.cfg.get("vix_extreme_threshold", 35)):
            return level, "extreme"
        if level >= float(self.cfg.get("vix_risk_off_threshold", 25)):
            return level, "risk_off"
        return level, "normal"

    # ------------------------------------------------------------------
    # Fetch with broker-first, web-fallback
    # ------------------------------------------------------------------
    def _fetch(self, asset: str, primary: str, fallbacks: Sequence[str],
               timeframe: str, count: int) -> pd.DataFrame | None:
        # 1. Broker
        if self._broker_fetch and self._resolve:
            cached = self._resolved.get(asset)
            if cached:
                sym, _ = cached
                df = self._safe_call(self._broker_fetch, sym, timeframe, count)
                if df is not None and not df.empty:
                    return df
            # Resolve fresh
            resolved_sym = self._safe_call(self._resolve, primary, list(fallbacks))
            if resolved_sym:
                df = self._safe_call(self._broker_fetch, resolved_sym, timeframe, count)
                if df is not None and not df.empty:
                    self._resolved[asset] = (resolved_sym, "broker")
                    return df

        # 2. Web fallback
        if not self.cfg.get("use_web_api_fallback", True):
            return None
        ticker = self.YF_TICKERS.get(asset)
        if not ticker:
            return None
        cache_key = f"{asset}:{timeframe}:{count}"
        cached_df = self._cache.get(cache_key, self._cache_ttl_s)
        if cached_df is not None:
            self._resolved[asset] = (ticker, "yfinance(cached)")
            return cached_df
        df = self._safe_call(self._web_fetch, ticker, count)
        if df is not None and not df.empty:
            self._cache.put(cache_key, df)
            self._resolved[asset] = (ticker, "yfinance")
            return df
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _invert(direction: MacroBias) -> MacroBias:
        if direction == MacroBias.BULLISH:
            return MacroBias.BEARISH
        if direction == MacroBias.BEARISH:
            return MacroBias.BULLISH
        return direction

    @staticmethod
    def _safe_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning("macro fetch error in %s: %s", getattr(fn, "__name__", fn), e)
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _default_web_fetch(ticker: str, count: int) -> pd.DataFrame:
        """Default yfinance fetcher. Imported lazily to avoid hard dep at import time."""
        import yfinance as yf  # type: ignore
        period = "60d" if count <= 60 else "180d"
        data = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        if data is None or data.empty:
            return pd.DataFrame()
        df = data.reset_index().rename(columns={
            "Date": "time", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "tick_volume",
        })
        return df.tail(count).reset_index(drop=True)

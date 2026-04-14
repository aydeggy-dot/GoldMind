"""MT5 connector — sole owner of the MetaTrader5 API surface.

No other module may import MetaTrader5 directly. All MT5 calls are
serialized via a lock (the MT5 Python API is not thread-safe) and
wrapped in try/except to keep the engine alive across transient errors.

Surface:
- connect / disconnect / reconnect (exponential backoff)
- discover_symbol (with fallbacks + brute-force search)
- get_account_info / get_symbol_info / get_tick
- get_closed_candles (signal-safe — drops the in-progress bar)
- get_clock_drift
- check_margin_for_order (wraps order_calc_margin)
- place_order (with partial-fill detection)
- modify_position / close_position / close_all_positions
- get_positions (filtered by magic number) / get_history
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pandas as pd

try:  # MT5 is Windows-only; allow import to fail in test environments
    import MetaTrader5 as mt5  # type: ignore
    _MT5_AVAILABLE = True
except Exception:  # pragma: no cover - allows non-Windows dev
    mt5 = None  # type: ignore
    _MT5_AVAILABLE = False

from utils.constants import (
    MT5_FILLING,
    MT5_TIMEFRAMES,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_DONE_PARTIAL,
)

logger = logging.getLogger("goldmind")


class MT5Error(Exception):
    """Raised for unrecoverable MT5 errors at the connector boundary."""


class MT5Connector:
    """Thread-safe wrapper around MetaTrader5 Python API."""

    def __init__(
        self,
        account: int,
        password: str,
        server: str,
        terminal_path: str | None = None,
        max_reconnect_attempts: int = 5,
        reconnect_delay_seconds: int = 30,
    ) -> None:
        if not _MT5_AVAILABLE:
            raise MT5Error("MetaTrader5 package not available on this system")
        self.account = int(account)
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self._lock = threading.RLock()
        self._connected = False
        self._symbol: str | None = None
        self._broker_offset_cache: timedelta | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        with self._lock:
            init_kwargs: dict[str, Any] = {}
            if self.terminal_path:
                init_kwargs["path"] = self.terminal_path
            ok = mt5.initialize(**init_kwargs) if init_kwargs else mt5.initialize()
            if not ok:
                err = mt5.last_error()
                logger.error("MT5 initialize failed: %s", err)
                return False
            login_ok = mt5.login(self.account, password=self.password, server=self.server)
            if not login_ok:
                err = mt5.last_error()
                logger.error("MT5 login failed for account %s: %s", self.account, err)
                mt5.shutdown()
                return False
            self._connected = True
            info = mt5.terminal_info()
            logger.info("MT5 connected: terminal=%s, build=%s",
                        getattr(info, "name", "?"), getattr(info, "build", "?"))
            return True

    def disconnect(self) -> None:
        with self._lock:
            try:
                mt5.shutdown()
            except Exception as e:  # pragma: no cover
                logger.warning("MT5 shutdown error: %s", e)
            finally:
                self._connected = False

    def is_connected(self) -> bool:
        with self._lock:
            if not self._connected:
                return False
            info = mt5.terminal_info()
            return bool(info and getattr(info, "connected", False))

    def reconnect(self) -> bool:
        """Exponential backoff reconnect; returns True on success."""
        for attempt in range(1, self.max_reconnect_attempts + 1):
            logger.warning("MT5 reconnect attempt %d/%d", attempt, self.max_reconnect_attempts)
            self.disconnect()
            if self.connect():
                return True
            delay = self.reconnect_delay_seconds * (2 ** (attempt - 1))
            time.sleep(min(delay, 300))
        logger.error("MT5 reconnect exhausted after %d attempts", self.max_reconnect_attempts)
        return False

    # ------------------------------------------------------------------
    # Symbol discovery
    # ------------------------------------------------------------------
    def discover_symbol(self, preferred: str, fallbacks: Sequence[str] = ()) -> str | None:
        """Find the broker's symbol name for gold (or any tradable).

        Tries preferred -> each fallback -> brute force scan for XAU+USD.
        On success, selects symbol into Market Watch and caches it.
        """
        with self._lock:
            for candidate in (preferred, *fallbacks):
                if not candidate:
                    continue
                if self._symbol_usable(candidate):
                    self._symbol = candidate
                    logger.info("Symbol discovered: %s", candidate)
                    return candidate

            # Brute force: any visible symbol matching XAU+USD
            symbols = mt5.symbols_get() or []
            for s in symbols:
                name = getattr(s, "name", "")
                upper = name.upper()
                if "XAU" in upper and "USD" in upper and self._symbol_usable(name):
                    self._symbol = name
                    logger.info("Symbol discovered via scan: %s", name)
                    return name

            logger.error("No XAUUSD symbol found on this broker")
            return None

    def _symbol_usable(self, name: str) -> bool:
        info = mt5.symbol_info(name)
        if info is None:
            return False
        if not info.visible and not mt5.symbol_select(name, True):
            return False
        return True

    @property
    def symbol(self) -> str | None:
        return self._symbol

    # ------------------------------------------------------------------
    # Account / symbol / tick
    # ------------------------------------------------------------------
    def get_account_info(self) -> dict[str, Any] | None:
        with self._lock:
            info = mt5.account_info()
            if info is None:
                logger.error("account_info() returned None: %s", mt5.last_error())
                return None
            return info._asdict()

    def get_symbol_info(self, symbol: str | None = None) -> dict[str, Any] | None:
        """Fetch FRESH symbol info — call before every position size calc."""
        with self._lock:
            sym = symbol or self._symbol
            if not sym:
                raise MT5Error("No symbol set; call discover_symbol() first")
            info = mt5.symbol_info(sym)
            if info is None:
                logger.error("symbol_info(%s) failed: %s", sym, mt5.last_error())
                return None
            return info._asdict()

    def get_tick(self, symbol: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            sym = symbol or self._symbol
            if not sym:
                raise MT5Error("No symbol set")
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                return None
            return tick._asdict()

    # ------------------------------------------------------------------
    # Candle data
    # ------------------------------------------------------------------
    def get_closed_candles(
        self,
        timeframe: str,
        count: int,
        symbol: str | None = None,
    ) -> pd.DataFrame:
        """Fetch the last `count` CLOSED candles (drops in-progress bar).

        ALL signal logic must use this method. Never use the raw current bar.
        """
        with self._lock:
            sym = symbol or self._symbol
            if not sym:
                raise MT5Error("No symbol set")
            tf_code = MT5_TIMEFRAMES.get(timeframe)
            if tf_code is None:
                raise ValueError(f"Unknown timeframe: {timeframe}")
            # Fetch count+1, drop the still-forming last bar.
            rates = mt5.copy_rates_from_pos(sym, tf_code, 0, count + 1)
            if rates is None or len(rates) == 0:
                logger.warning("No rates for %s %s: %s", sym, timeframe, mt5.last_error())
                return pd.DataFrame()
            df = pd.DataFrame(rates)
            # MT5 returns bar times as broker-server wall clock encoded as
            # seconds-since-epoch. Treating them as UTC yields broker-local
            # time, not real UTC. Apply broker offset so downstream (stale
            # checks, session windows, Asian range) work with true UTC.
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True) \
                + self._broker_utc_offset()
            return df.iloc[:-1].reset_index(drop=True)

    # ------------------------------------------------------------------
    # Clock drift / broker timezone
    # ------------------------------------------------------------------
    def get_clock_drift(self) -> timedelta | None:
        """Return system_clock - broker_server_clock as a timedelta.

        Positive => VPS clock is ahead of broker server.
        Returns None if no tick is available.
        """
        with self._lock:
            sym = self._symbol
            if not sym:
                return None
            tick = mt5.symbol_info_tick(sym)
            if tick is None or not tick.time:
                return None
            broker_utc = datetime.fromtimestamp(tick.time, tz=timezone.utc)
            return datetime.now(timezone.utc) - broker_utc

    def _broker_utc_offset(self) -> timedelta:
        """Broker wall-clock offset vs UTC, rounded to the nearest hour.

        MT5 timestamps ARE broker wall clock seconds interpreted as UTC,
        so `real_utc = wall_as_utc + offset` where `offset = drift rounded`.
        We round to whole hours to strip tiny VPS-NTP jitter; broker
        timezones are always on whole-hour (or half-hour) offsets in
        practice. Half-hour brokers would need 1800s rounding — add later
        if we hit one. Cached across calls to avoid a tick round-trip
        per candle fetch.
        """
        if self._broker_offset_cache is not None:
            return self._broker_offset_cache
        drift = self.get_clock_drift()
        if drift is None:
            return timedelta(0)
        offset_hours = round(drift.total_seconds() / 3600)
        self._broker_offset_cache = timedelta(hours=offset_hours)
        logger.info("Broker UTC offset inferred: %+d hours", offset_hours)
        return self._broker_offset_cache

    def refresh_broker_offset(self) -> None:
        """Clear the cached broker offset. Call after daily reset / DST shift."""
        self._broker_offset_cache = None

    # ------------------------------------------------------------------
    # Margin pre-check
    # ------------------------------------------------------------------
    def check_margin_for_order(
        self,
        order_type: int,
        symbol: str,
        lot: float,
        price: float,
    ) -> dict[str, Any] | None:
        """Wraps mt5.order_calc_margin. Returns required + free + sufficient."""
        with self._lock:
            try:
                required = mt5.order_calc_margin(order_type, symbol, lot, price)
            except Exception as e:  # noqa: BLE001
                logger.error("order_calc_margin error: %s", e)
                return None
            if required is None:
                logger.warning("order_calc_margin returned None: %s", mt5.last_error())
                return None
            acct = mt5.account_info()
            free = float(acct.margin_free) if acct else 0.0
            return {
                "required_margin": float(required),
                "free_margin": free,
                "margin_usage_pct": (required / free * 100) if free > 0 else 0.0,
                "sufficient": required > 0 and required < free,
            }

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    def place_order(
        self,
        order_type: int,
        lot: float,
        sl: float,
        tp: float,
        comment: str = "",
        symbol: str | None = None,
        deviation: int = 20,
        filling_type: str = "IOC",
        magic_number: int = 0,
    ) -> dict[str, Any]:
        """Place a market order. Returns:
            {success, ticket, fill_price, fill_volume, partial_fill, retcode, comment}
        Caller must call risk_manager.handle_partial_fill on partial_fill=True.
        """
        with self._lock:
            sym = symbol or self._symbol
            if not sym:
                return self._fail_order("no symbol set")
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                return self._fail_order("no tick")
            price = tick.ask if order_type == ORDER_TYPE_BUY else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": float(lot),
                "type": int(order_type),
                "price": float(price),
                "sl": float(sl),
                "tp": float(tp),
                "deviation": int(deviation),
                "magic": int(magic_number),
                "comment": comment[:31],  # MT5 max 31 chars
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": MT5_FILLING.get(filling_type, MT5_FILLING["IOC"]),
            }
            result = mt5.order_send(request)
            if result is None:
                return self._fail_order(f"order_send None: {mt5.last_error()}")

            retcode = int(result.retcode)
            success = retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL)
            actual_volume = float(getattr(result, "volume", 0))
            partial = success and actual_volume > 0 and actual_volume < float(lot) - 1e-9

            payload = {
                "success": success,
                "ticket": int(getattr(result, "order", 0) or getattr(result, "deal", 0)),
                "fill_price": float(getattr(result, "price", price)),
                "fill_volume": actual_volume,
                "requested_volume": float(lot),
                "partial_fill": partial,
                "retcode": retcode,
                "comment": str(getattr(result, "comment", "")),
            }
            if not success:
                logger.warning("order_send rejected: retcode=%s comment=%s",
                               retcode, payload["comment"])
            elif partial:
                logger.warning("Partial fill: requested=%s actual=%s ticket=%s",
                               lot, actual_volume, payload["ticket"])
            else:
                logger.info("Order filled: ticket=%s vol=%s @ %s",
                            payload["ticket"], actual_volume, payload["fill_price"])
            return payload

    @staticmethod
    def _fail_order(reason: str) -> dict[str, Any]:
        logger.error("Order failed: %s", reason)
        return {
            "success": False, "ticket": 0, "fill_price": 0.0,
            "fill_volume": 0.0, "requested_volume": 0.0,
            "partial_fill": False, "retcode": -1, "comment": reason,
        }

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def get_positions(self, magic_number: int | None = None,
                      symbol: str | None = None) -> list[Any]:
        """Return open positions, optionally filtered by magic number / symbol."""
        with self._lock:
            sym = symbol or self._symbol
            try:
                if sym:
                    positions = mt5.positions_get(symbol=sym) or []
                else:
                    positions = mt5.positions_get() or []
            except Exception as e:  # noqa: BLE001
                logger.error("positions_get error: %s", e)
                return []
            if magic_number is None:
                return list(positions)
            return [p for p in positions if int(getattr(p, "magic", 0)) == magic_number]

    def modify_position(
        self,
        ticket: int,
        sl: float | None = None,
        tp: float | None = None,
        symbol: str | None = None,
    ) -> bool:
        """Modify SL/TP of an open position. NEVER widens — caller enforces."""
        with self._lock:
            sym = symbol or self._symbol
            if not sym:
                return False
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "symbol": sym,
            }
            if sl is not None:
                request["sl"] = float(sl)
            if tp is not None:
                request["tp"] = float(tp)
            result = mt5.order_send(request)
            if result is None:
                logger.error("modify_position None: %s", mt5.last_error())
                return False
            ok = int(result.retcode) == TRADE_RETCODE_DONE
            if not ok:
                logger.warning("modify_position retcode=%s: %s",
                               result.retcode, getattr(result, "comment", ""))
            return ok

    def close_position(
        self,
        ticket: int,
        volume: float | None = None,
        deviation: int = 20,
        magic_number: int = 0,
        comment: str = "close",
    ) -> dict[str, Any]:
        """Close (fully or partially) an open position."""
        with self._lock:
            positions = mt5.positions_get(ticket=int(ticket)) or []
            if not positions:
                return self._fail_order(f"no position ticket={ticket}")
            pos = positions[0]
            sym = pos.symbol
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                return self._fail_order("no tick at close")
            close_type = ORDER_TYPE_SELL if pos.type == ORDER_TYPE_BUY else ORDER_TYPE_BUY
            close_price = tick.bid if pos.type == ORDER_TYPE_BUY else tick.ask
            vol = float(volume) if volume is not None else float(pos.volume)
            vol = min(vol, float(pos.volume))
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": vol,
                "type": close_type,
                "position": int(ticket),
                "price": float(close_price),
                "deviation": int(deviation),
                "magic": int(magic_number),
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": MT5_FILLING["IOC"],
            }
            result = mt5.order_send(request)
            if result is None:
                return self._fail_order(f"close None: {mt5.last_error()}")
            ok = int(result.retcode) in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL)
            return {
                "success": ok,
                "ticket": int(ticket),
                "fill_price": float(getattr(result, "price", close_price)),
                "fill_volume": float(getattr(result, "volume", vol)),
                "retcode": int(result.retcode),
                "comment": str(getattr(result, "comment", "")),
            }

    def close_all_positions(self, magic_number: int | None = None,
                            symbol: str | None = None) -> list[dict[str, Any]]:
        results = []
        for pos in self.get_positions(magic_number=magic_number, symbol=symbol):
            results.append(self.close_position(
                ticket=int(pos.ticket),
                magic_number=magic_number or 0,
                comment="close_all",
            ))
        return results

    def get_history(
        self,
        from_dt: datetime,
        to_dt: datetime | None = None,
        magic_number: int | None = None,
    ) -> list[Any]:
        """Closed deal history. Filters by magic if provided."""
        with self._lock:
            to_dt = to_dt or datetime.now(timezone.utc)
            try:
                deals = mt5.history_deals_get(from_dt, to_dt) or []
            except Exception as e:  # noqa: BLE001
                logger.error("history_deals_get error: %s", e)
                return []
            if magic_number is None:
                return list(deals)
            return [d for d in deals if int(getattr(d, "magic", 0)) == magic_number]

    # ------------------------------------------------------------------
    # Broker timezone (kept at bottom)
    # ------------------------------------------------------------------
    def get_broker_timezone_offset_hours(self) -> float | None:
        """Estimate broker server UTC offset in hours from a fresh tick.

        Uses MT5's tick.time (epoch seconds, server time interpreted as UTC by MT5)
        compared to local clock. For most brokers this returns approximately zero
        because MT5 already normalizes; brokers that quote in their own tz may
        show a non-zero value.
        """
        drift = self.get_clock_drift()
        if drift is None:
            return None
        return -drift.total_seconds() / 3600.0

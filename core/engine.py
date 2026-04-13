"""Engine — main orchestrator.

Wires every other module together:
    config + creds -> connector + DB -> validator + sessions + regime + macro +
    news + strategy + risk + trade_manager -> Telegram (Phase 6) + analytics (Phase 7).

Designed so subsystems are injectable (notifier, health_monitor are optional
protocols). Tests substitute fakes and drive the loop one tick at a time.

The loop body runs the full 13-step sequence from GOLDMIND_PROMPT.md. Each step
is small, side-effect-light, and guarded with try/except so a transient error
in one subsystem cannot kill the whole loop.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

import pandas as pd

from core.data_validator import DataValidator
from core.macro_filter import MacroFilter
from core.mt5_connector import MT5Connector
from core.news_filter import NewsFilter
from core.regime_detector import RegimeDetector
from core.risk_manager import RiskManager
from core.session_manager import SessionManager
from core.strategy import Signal, Strategy
from core.trade_manager import ActionType, TradeAction, TradeManager
from database import DBManager
from utils.constants import (
    Direction,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    Regime,
    SetupType,
)
from utils.helpers import points_distance

logger = logging.getLogger("goldmind")


# ----------------------------------------------------------------------
# Optional collaborators (no-op defaults — Phase 6/7 will replace)
# ----------------------------------------------------------------------
class Notifier(Protocol):
    def notify(self, category: str, message: str, urgent: bool = False) -> None: ...


class HealthMonitor(Protocol):
    def heartbeat(self, account_info: Mapping[str, Any]) -> None: ...
    def on_trade_closed(self, trade: Mapping[str, Any]) -> dict[str, Any]: ...


class _NullNotifier:
    def notify(self, category: str, message: str, urgent: bool = False) -> None:
        logger.info("[%s] %s%s", category, "URGENT " if urgent else "", message)


class _NullHealthMonitor:
    def heartbeat(self, account_info): pass
    def on_trade_closed(self, trade): return {}


# ----------------------------------------------------------------------
# Engine state (in-memory mirror of paused/heartbeat — persistence in DB)
# ----------------------------------------------------------------------
@dataclass
class EngineState:
    paused: bool = False
    last_heartbeat: datetime | None = None
    last_daily_reset: str | None = None        # YYYY-MM-DD
    last_baseline_check: str | None = None     # YYYY-MM-DD
    warm_up_complete: bool = False
    consecutive_errors: int = 0


@dataclass
class _MarketSnapshot:
    h4: pd.DataFrame = field(default_factory=pd.DataFrame)
    h1: pd.DataFrame = field(default_factory=pd.DataFrame)
    m15: pd.DataFrame = field(default_factory=pd.DataFrame)
    m5: pd.DataFrame = field(default_factory=pd.DataFrame)
    d1: pd.DataFrame = field(default_factory=pd.DataFrame)


# ----------------------------------------------------------------------
class Engine:
    """The trading engine. Construct, then call start() (blocking)."""

    ENGINE_STATE_KEY = "engine_state"

    def __init__(
        self,
        config: Mapping[str, Any],
        connector: MT5Connector,
        db: DBManager,
        notifier: Notifier | None = None,
        health_monitor: HealthMonitor | None = None,
        macro_filter: MacroFilter | None = None,
        news_filter: NewsFilter | None = None,
    ) -> None:
        self.cfg = config
        self.connector = connector
        self.db = db
        self.notifier = notifier or _NullNotifier()
        self.health = health_monitor or _NullHealthMonitor()

        self.validator = DataValidator(**self._validator_kwargs(config["data_validation"]))
        self.sessions = SessionManager(config["sessions"], config["holidays"])
        self.regime = RegimeDetector.from_config(config["regime"], config["strategy"])
        self.macro = macro_filter or MacroFilter(
            config["macro"],
            broker_fetcher=lambda sym, tf, n: connector.get_closed_candles(tf, n, symbol=sym),
            symbol_resolver=lambda primary, fb: connector.discover_symbol(primary, fb),
        )
        self.news = news_filter or NewsFilter(config["news"], event_fetcher=None)
        self.strategy = Strategy(config["strategy"], config["risk"])
        self.risk = RiskManager(config, db)
        self.tm = TradeManager(config["trade_management"], config["risk"], config["sessions"])

        self.symbol: str | None = connector.symbol
        self.state = EngineState()
        loaded = db.get_state(self.ENGINE_STATE_KEY) or {}
        self.state.paused = bool(loaded.get("paused", False))
        self.state.last_daily_reset = loaded.get("last_daily_reset")
        self.state.last_baseline_check = loaded.get("last_baseline_check")

        self._stop = False
        self._strategy_version = str(config.get("strategy_version", {}).get("version", "0.0.0"))

    @staticmethod
    def _validator_kwargs(cfg: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "max_candle_age_minutes": cfg.get("max_candle_age_minutes", 30),
            "max_price_gap_pct": cfg.get("max_price_gap_pct", 5.0),
            "reject_zero_prices": cfg.get("reject_zero_prices", True),
            "reject_nan_values": cfg.get("reject_nan_values", True),
            "reject_high_less_than_low": cfg.get("reject_high_less_than_low", True),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Blocking. Returns when stop() is called."""
        self._startup()
        try:
            while not self._stop:
                try:
                    self.tick()
                    self.state.consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001
                    self.state.consecutive_errors += 1
                    logger.exception("Tick error: %s", exc)
                    self.notifier.notify("error", f"Tick error: {exc}", urgent=True)
                    if self.state.consecutive_errors >= 3:
                        self.state.paused = True
                        self._save_state()
                        self.notifier.notify("error", "3 consecutive tick errors — paused",
                                             urgent=True)
                time.sleep(self._sleep_seconds())
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._stop = True

    def _sleep_seconds(self) -> int:
        return 60 if self._is_active_session() else 300

    # ------------------------------------------------------------------
    def _startup(self) -> None:
        logger.info("=== GoldMind Engine starting ===")
        if not self.connector.is_connected():
            if not self.connector.connect():
                raise RuntimeError("MT5 connection failed at startup")
        self.symbol = self.symbol or self.connector.discover_symbol(
            self.cfg["mt5"]["symbol"], self.cfg["mt5"].get("symbol_fallbacks", []))
        if not self.symbol:
            raise RuntimeError("XAUUSD symbol could not be discovered")

        # Capture broker spec baseline + clock drift on first run
        self._upsert_baseline_if_changed()
        self._check_clock_drift()

        # Warm-up: pre-fetch every timeframe and validate
        self._warm_up()

        # Reconcile open positions (in case of crash recovery)
        self._reconcile_positions()

        self.notifier.notify("system", "GoldMind warm-up complete. Engine running.")
        logger.info("Startup complete. Symbol=%s", self.symbol)

    def _shutdown(self) -> None:
        logger.info("=== GoldMind Engine shutting down ===")
        self._save_state()
        self.notifier.notify("system", "GoldMind engine stopped (state saved, positions intact).")

    # ------------------------------------------------------------------
    # The 13-step loop
    # ------------------------------------------------------------------
    def tick(self, now_utc: datetime | None = None) -> None:
        now = now_utc or datetime.now(timezone.utc)

        # 1. SYSTEM HEALTH
        if not self._system_health_ok(now):
            return

        # 2. DAILY RESET
        self._maybe_daily_reset(now)

        # 3. SESSION
        if not self._is_active_session(now):
            self._manage_existing_only(now)
            return
        if self.state.paused:
            self._manage_existing_only(now)
            return

        # 4. NEWS
        news_check = self.news.is_blocked(now)
        if news_check.blocked:
            self.notifier.notify("system", news_check.reason)
            self._manage_existing_only(now)
            return

        # 5. CIRCUIT BREAKERS gate runs inside risk.can_trade

        # 8. MANAGE EXISTING (run BEFORE looking for new setups)
        self._manage_open_positions(now)

        # 9. SCAN
        snap = self._fetch_snapshot()
        if not self._validate_snapshot(snap):
            return

        # 6. REGIME
        regime_reading = self.regime.detect(snap.h1)
        if regime_reading.regime == Regime.UNKNOWN:
            return
        if not RegimeDetector_should_trade(regime_reading.regime):
            return

        # 7. MACRO
        macro_reading = self.macro.evaluate()

        # Asian range (used by sweep setup)
        asian_range = self.sessions.get_asian_range(snap.h1, now)

        symbol_info = self.connector.get_symbol_info(self.symbol) or {}
        if not symbol_info:
            self.notifier.notify("error", "symbol_info unavailable", urgent=True)
            return

        # 9b. SCAN strategy setups
        signals: list[Signal] = self.strategy.scan_for_signals(
            h4=snap.h4, h1=snap.h1, m15=snap.m15, d1=snap.d1,
            symbol_info=symbol_info,
            regime=regime_reading.regime,
            macro_bias=macro_reading.bias,
            asian_range=asian_range,
        )
        if not signals:
            return

        # 10-12. Validate, size, execute (best signal first)
        best = max(signals, key=lambda s: s.confidence)
        self._consider_signal(best, symbol_info, regime_reading, macro_reading, now)

    # ------------------------------------------------------------------
    # System / health / reset
    # ------------------------------------------------------------------
    def _system_health_ok(self, now: datetime) -> bool:
        if not self.connector.is_connected():
            self.notifier.notify("error", "MT5 disconnected — reconnecting", urgent=True)
            if not self.connector.reconnect():
                self.state.paused = True
                self._save_state()
                self.notifier.notify("error", "MT5 reconnect failed — engine paused",
                                     urgent=True)
                return False

        acct = self.connector.get_account_info() or {}
        if not acct:
            return False

        if self.risk.check_kill_switch(float(acct.get("balance", 0)), now):
            self.connector.close_all_positions(magic_number=int(self.cfg["mt5"]["magic_number"]),
                                               symbol=self.symbol)
            self.notifier.notify("kill", "KILL SWITCH activated. Closing all.", urgent=True)
            return False

        margin_level = float(acct.get("margin_level", 0) or 0)
        danger = float(self.cfg["margin"]["danger_margin_level"])
        if margin_level and margin_level < danger:
            self._close_weakest_position()
            self.notifier.notify("margin", f"Margin {margin_level:.0f}% < {danger}% — closed weakest",
                                 urgent=True)

        # Heartbeat every N minutes
        hb_interval = timedelta(minutes=int(self.cfg["health"]["heartbeat_interval_minutes"]))
        if not self.state.last_heartbeat or now - self.state.last_heartbeat >= hb_interval:
            self.state.last_heartbeat = now
            self.health.heartbeat(acct)

        return True

    def _maybe_daily_reset(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.state.last_daily_reset == today:
            return
        self.risk.reset_daily(now)
        self._upsert_baseline_if_changed()
        self._check_clock_drift()
        self.state.last_daily_reset = today
        self._save_state()
        self.notifier.notify("system", f"Daily reset complete for {today}")

    def _upsert_baseline_if_changed(self) -> None:
        info = self.connector.get_symbol_info(self.symbol)
        if not info:
            return
        baseline = self.db.get_broker_baseline(self.symbol or "")
        current = {
            "contract_size": float(info.get("trade_contract_size", 0) or 0),
            "margin_initial": float(info.get("margin_initial", 0) or 0),
            "margin_maintenance": float(info.get("margin_maintenance", 0) or 0),
            "volume_min": float(info.get("volume_min", 0) or 0),
            "volume_max": float(info.get("volume_max", 0) or 0),
            "volume_step": float(info.get("volume_step", 0) or 0),
            "swap_long": float(info.get("swap_long", 0) or 0),
            "swap_short": float(info.get("swap_short", 0) or 0),
            "leverage": int(info.get("trade_leverage", 0) or 0),
        }
        if baseline:
            diffs = {k: (baseline.get(k), v) for k, v in current.items()
                     if baseline.get(k) is not None and baseline.get(k) != v}
            if diffs:
                self.notifier.notify("broker",
                                     f"Broker spec change for {self.symbol}: {diffs}",
                                     urgent=True)
        self.db.upsert_broker_baseline(self.symbol or "", current)

    def _check_clock_drift(self) -> None:
        drift = self.connector.get_clock_drift()
        if drift is None:
            return
        secs = abs(drift.total_seconds())
        pause_at = float(self.cfg["health"]["pause_on_clock_drift_seconds"])
        warn_at = float(self.cfg["health"]["max_clock_drift_seconds"])
        if secs >= pause_at:
            self.state.paused = True
            self._save_state()
            self.notifier.notify("clock", f"Clock drift {secs:.0f}s — engine paused",
                                 urgent=True)
        elif secs >= warn_at:
            self.notifier.notify("clock", f"Clock drift {secs:.0f}s")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def _is_active_session(self, now_utc: datetime | None = None) -> bool:
        return self.sessions.is_tradeable(now_utc)

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------
    def _manage_existing_only(self, now: datetime) -> None:
        self._manage_open_positions(now)

    def _manage_open_positions(self, now: datetime) -> None:
        magic = int(self.cfg["mt5"]["magic_number"])
        positions = self.connector.get_positions(magic_number=magic, symbol=self.symbol)
        if not positions:
            return
        tick = self.connector.get_tick(self.symbol)
        info = self.connector.get_symbol_info(self.symbol)
        if not tick or not info:
            return
        bid = float(tick["bid"])
        ask = float(tick["ask"])
        point = float(info["point"])
        actions = self.tm.manage(positions, bid=bid, ask=ask, point=point, now_utc=now)
        for action in actions:
            self._apply_action(action, magic)

    def _apply_action(self, action: TradeAction, magic: int) -> None:
        if action.type == ActionType.MOVE_TO_BREAKEVEN:
            ok = self.connector.modify_position(action.ticket, sl=action.new_sl)
            if ok:
                self.notifier.notify("trade", f"Ticket {action.ticket} -> BE @ {action.new_sl}")
        elif action.type == ActionType.TIGHTEN_TRAILING:
            ok = self.connector.modify_position(action.ticket, sl=action.new_sl)
            if ok:
                self.notifier.notify("trade", f"Ticket {action.ticket} trailing -> {action.new_sl:.2f}")
        elif action.type == ActionType.PARTIAL_CLOSE:
            res = self.connector.close_position(action.ticket, volume=action.close_volume,
                                                 magic_number=magic, comment="partial")
            if res.get("success"):
                self.notifier.notify("trade",
                                     f"Partial close ticket={action.ticket} vol={action.close_volume}")
        elif action.type == ActionType.CLOSE_FULL:
            res = self.connector.close_position(action.ticket, magic_number=magic,
                                                 comment=action.exit_reason.value if action.exit_reason else "close")
            if res.get("success"):
                self._record_trade_close(
                    ticket=int(action.ticket),
                    exit_price=float(res.get("fill_price") or 0.0),
                    exit_reason=action.exit_reason.value if action.exit_reason else "close",
                )
                self.notifier.notify("trade",
                                     f"Closed ticket={action.ticket}: {action.reason}")

    def _record_trade_close(self, *, ticket: int, exit_price: float,
                            exit_reason: str) -> None:
        """Update trades row, run risk update, run health check, maybe pause."""
        row = self.db.fetchone(
            "SELECT * FROM trades WHERE ticket=? AND exit_time IS NULL "
            "ORDER BY id DESC LIMIT 1", (ticket,))
        if row is None:
            return
        trade = dict(row)
        entry = float(trade.get("entry_price") or 0.0)
        lot = float(trade.get("filled_lot") or 0.0)
        direction = str(trade.get("type") or "").upper()
        sign = 1.0 if direction == "LONG" else -1.0

        info = self.connector.get_symbol_info(self.symbol) or {}
        contract_size = float(info.get("trade_contract_size", 100.0) or 100.0)
        pnl = (exit_price - entry) * sign * lot * contract_size

        entry_ts = trade.get("entry_time")
        now = datetime.now(timezone.utc)
        duration_min = 0
        if entry_ts:
            try:
                entry_dt = datetime.fromisoformat(str(entry_ts))
                duration_min = int((now - entry_dt).total_seconds() / 60)
            except ValueError:
                pass

        sl = float(trade.get("stop_loss") or 0.0)
        risk_per_unit = abs(entry - sl) if sl else 0.0
        rr_achieved = ((exit_price - entry) * sign / risk_per_unit) if risk_per_unit else 0.0

        self.db.execute(
            "UPDATE trades SET exit_price=?, pnl=?, exit_time=?, exit_reason=?, "
            "rr_achieved=?, duration_minutes=? WHERE id=?",
            (exit_price, pnl, now.isoformat(), exit_reason, rr_achieved,
             duration_min, trade["id"]),
        )

        acct = self.connector.get_account_info() or {}
        balance_after = float(acct.get("balance", 0) or 0)
        try:
            self.risk.update_after_trade(pnl, balance_after, now)
        except Exception:  # noqa: BLE001
            logger.exception("risk.update_after_trade failed")

        try:
            reading = self.health.on_trade_closed({**trade, "pnl": pnl,
                                                   "exit_price": exit_price,
                                                   "exit_reason": exit_reason})
        except Exception:  # noqa: BLE001
            logger.exception("health.on_trade_closed failed")
            reading = None

        if reading and getattr(reading, "pause", False):
            self.state.paused = True
            self._save_state()
            self.notifier.notify(
                "strategy",
                f"Auto-paused: {'; '.join(getattr(reading, 'alerts', []))}",
                urgent=True,
            )

    def _close_weakest_position(self) -> None:
        magic = int(self.cfg["mt5"]["magic_number"])
        positions = self.connector.get_positions(magic_number=magic, symbol=self.symbol)
        if not positions:
            return
        weakest = min(positions, key=lambda p: float(getattr(p, "profit", 0.0)))
        self.connector.close_position(int(weakest.ticket), magic_number=magic,
                                       comment="margin_protect")

    # ------------------------------------------------------------------
    # Signal pipeline
    # ------------------------------------------------------------------
    def _consider_signal(
        self,
        signal: Signal,
        symbol_info: Mapping[str, Any],
        regime_reading,
        macro_reading,
        now: datetime,
    ) -> None:
        magic = int(self.cfg["mt5"]["magic_number"])
        positions = self.connector.get_positions(magic_number=magic, symbol=self.symbol)
        acct = self.connector.get_account_info() or {}

        # Persist signal regardless of outcome
        signal_row = {
            "type": signal.type.value, "direction": signal.direction.value,
            "entry_price": signal.entry, "stop_loss": signal.sl, "take_profit": signal.tp,
            "confidence": signal.confidence, "was_traded": 0,
            "timestamp": now.isoformat(),
            "regime": regime_reading.regime.value, "macro_bias": macro_reading.bias.value,
            "session": self.sessions.get_current_session(now).value,
            "strategy_version": self._strategy_version,
        }

        gate = self.risk.can_trade(acct, open_positions=positions, now_utc=now)
        if not gate.ok:
            signal_row["skip_reason"] = gate.reason
            self.db.insert_signal(signal_row)
            return

        # Spread check
        tick = self.connector.get_tick(self.symbol)
        spread_pts = points_distance(float(tick["ask"]), float(tick["bid"]),
                                     float(symbol_info["point"])) if tick else 999

        ok, reason = self.risk.validate_signal(signal, symbol_info, spread_pts,
                                                open_positions=positions)
        if not ok:
            signal_row["skip_reason"] = reason
            self.db.insert_signal(signal_row)
            return

        sl_distance = abs(signal.entry - signal.sl)
        atr_ratio = max(regime_reading.atr_ratio, 1.0)
        balance = float(acct.get("balance", 0))
        lot = self.risk.calculate_position_size(balance, sl_distance, symbol_info, atr_ratio)
        if lot <= 0:
            signal_row["skip_reason"] = "size underflow"
            self.db.insert_signal(signal_row)
            return

        safe_lot, sanity_reason = self.risk.sanity_check_lot(
            lot, balance, sl_distance, symbol_info,
            free_margin=float(acct.get("free_margin", 0)),
        )
        if safe_lot is None:
            signal_row["skip_reason"] = sanity_reason
            self.db.insert_signal(signal_row)
            self.notifier.notify("error", f"Sanity check FAILED: {sanity_reason}",
                                 urgent=True)
            return

        order_type = ORDER_TYPE_BUY if signal.direction == Direction.LONG else ORDER_TYPE_SELL
        margin_ok, margin_reason = self.risk.check_margin_before_order(
            order_type, self.symbol or "", safe_lot, signal.entry,
            margin_checker=self.connector.check_margin_for_order,
        )
        if not margin_ok:
            signal_row["skip_reason"] = margin_reason
            self.db.insert_signal(signal_row)
            return

        # 12. EXECUTE
        comment = f"{signal.type.value[:8]}|v{self._strategy_version}|{int(now.timestamp())}"[:31]
        result = self.connector.place_order(
            order_type=order_type, lot=safe_lot,
            sl=signal.sl, tp=signal.tp, comment=comment,
            symbol=self.symbol,
            deviation=int(self.cfg["mt5"]["deviation"]),
            filling_type=str(self.cfg["mt5"]["filling_type"]),
            magic_number=magic,
        )
        if not result.get("success"):
            signal_row["skip_reason"] = f"order rejected ({result.get('retcode')})"
            self.db.insert_signal(signal_row)
            self.notifier.notify("error", f"Order rejected: {result}", urgent=True)
            return

        if result.get("partial_fill"):
            close, _ = self.risk.handle_partial_fill(
                ticket=result["ticket"],
                requested_lot=result["requested_volume"],
                actual_lot=result["fill_volume"],
                symbol_info=symbol_info,
            )
            if close:
                self.connector.close_position(result["ticket"], magic_number=magic,
                                               comment="partial_underflow")
                signal_row["skip_reason"] = "partial fill below min — auto-closed"
                self.db.insert_signal(signal_row)
                return

        signal_row["was_traded"] = 1
        self.db.insert_signal(signal_row)

        self.db.insert_trade({
            "ticket": result["ticket"],
            "type": signal.direction.value,
            "setup_type": signal.type.value,
            "strategy_version": self._strategy_version,
            "entry_price": result["fill_price"],
            "stop_loss": signal.sl,
            "take_profit": signal.tp,
            "requested_lot": result["requested_volume"],
            "filled_lot": result["fill_volume"],
            "partial_fill": 1 if result.get("partial_fill") else 0,
            "confidence": signal.confidence,
            "regime": regime_reading.regime.value,
            "macro_bias": macro_reading.bias.value,
            "session": self.sessions.get_current_session(now).value,
            "margin_level_at_entry": float(acct.get("margin_level", 0) or 0),
            "entry_time": now.isoformat(),
            "is_backtest": 0,
            "notes": signal.reasoning,
        })
        self.notifier.notify(
            "trade",
            (f"OPEN {signal.direction.value} {self.symbol} {result['fill_volume']} "
             f"@ {result['fill_price']:.2f} SL={signal.sl:.2f} TP={signal.tp:.2f} "
             f"({signal.type.value}, conf={signal.confidence:.2f})"),
        )

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def _fetch_snapshot(self) -> _MarketSnapshot:
        warm = self.cfg["warm_up"]["required_bars"]
        return _MarketSnapshot(
            h4=self.connector.get_closed_candles("H4", int(warm["H4"])),
            h1=self.connector.get_closed_candles("H1", int(warm["H1"])),
            m15=self.connector.get_closed_candles("M15", int(warm["M15"])),
            m5=self.connector.get_closed_candles("M5", int(warm["M5"])),
            d1=self.connector.get_closed_candles("D1", int(warm["D1"])),
        )

    def _validate_snapshot(self, snap: _MarketSnapshot) -> bool:
        for tf, df in (("H4", snap.h4), ("H1", snap.h1), ("M15", snap.m15), ("D1", snap.d1)):
            res = self.validator.validate_candles(df, tf, self.symbol or "")
            if not res:
                logger.error("Snapshot validation failed [%s]: %s", tf, res.reason)
                return False
        return True

    def _warm_up(self) -> None:
        snap = self._fetch_snapshot()
        if not self._validate_snapshot(snap):
            raise RuntimeError("warm-up validation failed")
        self.state.warm_up_complete = True

    def _reconcile_positions(self) -> None:
        magic = int(self.cfg["mt5"]["magic_number"])
        positions = self.connector.get_positions(magic_number=magic, symbol=self.symbol)
        logger.info("Reconciled %d open positions for magic=%d", len(positions), magic)

    # ------------------------------------------------------------------
    def _save_state(self) -> None:
        self.db.set_state(self.ENGINE_STATE_KEY, {
            "paused": self.state.paused,
            "last_daily_reset": self.state.last_daily_reset,
            "last_baseline_check": self.state.last_baseline_check,
        })

    # ------------------------------------------------------------------
    # Manual control (Telegram-driven in Phase 6)
    # ------------------------------------------------------------------
    def pause(self) -> None:
        self.state.paused = True
        self._save_state()

    def resume(self) -> None:
        self.state.paused = False
        self._save_state()

    def close_all(self) -> list[dict[str, Any]]:
        magic = int(self.cfg["mt5"]["magic_number"])
        return self.connector.close_all_positions(magic_number=magic, symbol=self.symbol)


def RegimeDetector_should_trade(regime: Regime) -> bool:
    """Wrapper to avoid circular static-method import."""
    return regime in (Regime.TRENDING_BULLISH, Regime.TRENDING_BEARISH)

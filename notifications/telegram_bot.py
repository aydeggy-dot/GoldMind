"""Telegram bot — notifications + command interface.

Runs in TWO background daemon threads:

  1. Sender: drains a thread-safe queue, respects a 10/min token bucket.
  2. Listener: long-polls getUpdates, dispatches /commands to the Engine.

Destructive commands (/closeall, /kill) require a 'yes' reply within 60s.
All Telegram HTTP is pluggable via the `http` kwarg so tests can inject a
fake client with no network or threads.

The class implements the Engine's `Notifier` Protocol via `notify()` and so
can be passed directly to `Engine(..., notifier=TelegramNotifier(...))`.

We intentionally do NOT depend on python-telegram-bot's async runtime — a
straight HTTP adapter is lighter, easier to reason about, and matches the
"separate thread, queued, rate limited" spec from GOLDMIND_PROMPT.md.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from notifications import templates

logger = logging.getLogger("goldmind")

BASE_URL = "https://api.telegram.org/bot{token}"
LONG_POLL_TIMEOUT = 25
DESTRUCTIVE_CONFIRM_TTL_SECONDS = 60
DESTRUCTIVE_COMMANDS = frozenset({"/closeall", "/kill"})


# ----------------------------------------------------------------------
# HTTP adapter (the only thing tests override)
# ----------------------------------------------------------------------
class TelegramHTTP:
    """Minimal Telegram Bot API client — just the two methods we call."""

    def __init__(self, bot_token: str) -> None:
        import requests  # local import so module is importable without requests
        self._base = BASE_URL.format(token=bot_token)
        self._sess = requests.Session()

    def post(self, method: str, payload: Mapping[str, Any], timeout: int = 30) -> dict[str, Any]:
        r = self._sess.post(f"{self._base}/{method}", json=payload, timeout=timeout)
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "description": f"non-json {r.status_code}"}


# ----------------------------------------------------------------------
# Token bucket (10/min default)
# ----------------------------------------------------------------------
class _TokenBucket:
    def __init__(self, rate_per_minute: int) -> None:
        self.capacity = float(rate_per_minute)
        self.tokens = float(rate_per_minute)
        self.refill_per_sec = rate_per_minute / 60.0
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, now: float | None = None) -> float:
        """Return seconds to wait before a token is available (0 if ready)."""
        with self._lock:
            now = now if now is not None else time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            return (1.0 - self.tokens) / self.refill_per_sec


# ----------------------------------------------------------------------
@dataclass
class _PendingConfirm:
    cmd: str
    expires_at: float


class TelegramNotifier:
    """Telegram notifications + command router. Matches Engine.Notifier protocol."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str | int,
        engine: Any = None,
        db: Any = None,
        config: Mapping[str, Any] | None = None,
        http: TelegramHTTP | None = None,
        rate_limit_per_minute: int = 10,
        started_at: datetime | None = None,
    ) -> None:
        self.chat_id = str(chat_id)
        self.engine = engine
        self.db = db
        self.cfg = config or {}
        self.http = http or TelegramHTTP(bot_token)
        self.started_at = started_at or datetime.now(timezone.utc)

        self._queue: queue.Queue[tuple[str, bool]] = queue.Queue()
        self._bucket = _TokenBucket(rate_limit_per_minute)
        self._stop = threading.Event()
        self._sender_thread: threading.Thread | None = None
        self._listener_thread: threading.Thread | None = None
        self._update_offset = 0
        self._pending: _PendingConfirm | None = None
        self._lock = threading.Lock()
        self._last_trade_at: datetime | None = None

    # ------------------------------------------------------------------
    # Public API (engine-facing)
    # ------------------------------------------------------------------
    def notify(self, category: str, message: str, urgent: bool = False) -> None:
        """Engine hook — enqueues for the sender thread. Never blocks."""
        prefix = "[URGENT] " if urgent else ""
        text = f"{prefix}[{category}] {message}"
        self._queue.put((text, urgent))
        if category == "trade" and message.startswith("OPEN"):
            self._last_trade_at = datetime.now(timezone.utc)

    def start(self) -> None:
        """Launch sender + listener daemon threads. Idempotent."""
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._stop.clear()
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="telegram-sender", daemon=True)
        self._listener_thread = threading.Thread(
            target=self._listener_loop, name="telegram-listener", daemon=True)
        self._sender_thread.start()
        self._listener_thread.start()
        logger.info("TelegramNotifier threads started")

    def stop(self) -> None:
        self._stop.set()
        # Unblock sender via a sentinel
        try:
            self._queue.put_nowait(("__stop__", False))
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # Sender
    # ------------------------------------------------------------------
    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            try:
                text, _urgent = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if text == "__stop__":
                return
            wait = self._bucket.take()
            if wait > 0:
                # Respect rate limit — re-queue if shutting down
                if self._stop.wait(wait):
                    return
            self._send_raw(text)

    def _send_raw(self, text: str) -> bool:
        try:
            resp = self.http.post("sendMessage",
                                  {"chat_id": self.chat_id, "text": text})
            if not resp.get("ok"):
                logger.warning("Telegram send failed: %s", resp.get("description"))
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram send exception: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------
    def _listener_loop(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self.http.post(
                    "getUpdates",
                    {"offset": self._update_offset, "timeout": LONG_POLL_TIMEOUT},
                    timeout=LONG_POLL_TIMEOUT + 5,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram poll error: %s", exc)
                if self._stop.wait(5):
                    return
                continue
            if not resp.get("ok"):
                if self._stop.wait(5):
                    return
                continue
            for upd in resp.get("result", []):
                self._update_offset = int(upd["update_id"]) + 1
                self._dispatch_update(upd)

    def _dispatch_update(self, upd: Mapping[str, Any]) -> None:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != self.chat_id:
            self._send_raw(templates.unauthorized())
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        reply = self._handle_command(text)
        if reply:
            self._send_raw(reply)

    # ------------------------------------------------------------------
    # Command router (extracted so tests can call directly)
    # ------------------------------------------------------------------
    def _handle_command(self, text: str) -> str:
        with self._lock:
            if self._pending and self._pending.expires_at < time.monotonic():
                self._pending = None

            lower = text.lower().strip()

            # Confirmation flow — must come BEFORE destructive dispatch
            if self._pending is not None:
                if lower in ("yes", "y"):
                    cmd, self._pending = self._pending.cmd, None
                    return self._execute_destructive(cmd)
                self._pending = None
                return "Cancelled."

            parts = text.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd in DESTRUCTIVE_COMMANDS:
                self._pending = _PendingConfirm(
                    cmd=cmd,
                    expires_at=time.monotonic() + DESTRUCTIVE_CONFIRM_TTL_SECONDS,
                )
                return templates.confirm_prompt(cmd)

            return self._execute_safe(cmd, args)

    def _execute_safe(self, cmd: str, args: list[str]) -> str:
        try:
            if cmd == "/status":
                return self._cmd_status()
            if cmd == "/pause":
                if self.engine:
                    self.engine.pause()
                return "Paused. Managing existing positions only."
            if cmd == "/resume":
                if self.engine:
                    self.engine.resume()
                return "Resumed."
            if cmd == "/report":
                return self._cmd_report()
            if cmd == "/risk":
                return self._cmd_set_risk(args)
            if cmd == "/maxlot":
                return self._cmd_set_maxlot(args)
            if cmd == "/health":
                return self._cmd_health()
            if cmd == "/trades":
                return self._cmd_trades()
            if cmd == "/uptime":
                return templates.uptime(started_at=self.started_at,
                                        last_trade=self._last_trade_at)
            if cmd == "/version":
                sv = dict(self.cfg.get("strategy_version", {}))
                return templates.version(version_str=str(sv.get("version", "?")),
                                         notes=str(sv.get("version_notes", "")))
            if cmd == "/margin":
                return self._cmd_margin()
            if cmd in ("/start", "/help"):
                return ("Commands: /status /pause /resume /closeall /report "
                        "/risk N /maxlot N /kill /health /trades /uptime /version /margin")
            return f"Unknown command: {cmd}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Command error")
            return f"Command error: {exc}"

    def _execute_destructive(self, cmd: str) -> str:
        if cmd == "/closeall":
            if not self.engine:
                return "No engine attached."
            results = self.engine.close_all()
            return f"close_all issued. {len(results)} positions processed."
        if cmd == "/kill":
            if self.engine and hasattr(self.engine, "risk"):
                self.engine.risk.state.kill_switch_active = True
                self.engine.risk._save()  # type: ignore[attr-defined]
                self.engine.pause()
                self.engine.close_all()
            return "Kill switch engaged. All positions closed. Trading disabled."
        return f"Unknown destructive command: {cmd}"

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------
    def _cmd_status(self) -> str:
        if not self.engine:
            return "No engine attached."
        acct = self.engine.connector.get_account_info() or {}
        positions = self.engine.connector.get_positions(
            magic_number=int(self.cfg.get("mt5", {}).get("magic_number", 0)),
            symbol=self.engine.symbol)
        regime = "unknown"
        try:
            snap = self.engine._fetch_snapshot()  # noqa: SLF001
            regime = self.engine.regime.detect(snap.h1).regime.value
        except Exception:  # noqa: BLE001
            pass
        macro = "unknown"
        try:
            macro = self.engine.macro.evaluate().bias.value
        except Exception:  # noqa: BLE001
            pass
        return templates.status(
            symbol=self.engine.symbol or "?",
            balance=float(acct.get("balance", 0) or 0),
            equity=float(acct.get("equity", 0) or 0),
            free_margin=float(acct.get("free_margin", 0) or 0),
            margin_level=float(acct.get("margin_level", 0) or 0),
            positions=len(positions),
            regime=regime,
            macro=macro,
            paused=bool(self.engine.state.paused),
            strategy_version=str(self.cfg.get("strategy_version", {}).get("version", "?")),
        )

    def _cmd_report(self) -> str:
        if not self.db:
            return "No database attached."
        today = datetime.now(timezone.utc).date().isoformat()
        rows = self.db.fetchall(
            "SELECT pnl, filled_lot FROM trades WHERE is_backtest=0 AND entry_time >= ?",
            (today,))
        trades = len(rows)
        pnl = sum(float(r["pnl"] or 0) for r in rows)
        wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
        losses = sum(1 for r in rows if (r["pnl"] or 0) < 0)
        win_rate = (100.0 * wins / trades) if trades else 0.0
        balance = 0.0
        if self.engine:
            balance = float((self.engine.connector.get_account_info() or {})
                            .get("balance", 0) or 0)
        return templates.daily_report(
            date=today, trades=trades, wins=wins, losses=losses,
            pnl=pnl, win_rate=win_rate, balance=balance, max_dd=0.0)

    def _cmd_trades(self) -> str:
        if not self.db:
            return "No database attached."
        rows = self.db.fetchall(
            "SELECT ticket, type, setup_type, entry_price, exit_price, pnl "
            "FROM trades WHERE is_backtest=0 ORDER BY id DESC LIMIT 10")
        return templates.trades_list([dict(r) for r in rows])

    def _cmd_margin(self) -> str:
        if not self.engine:
            return "No engine attached."
        acct = self.engine.connector.get_account_info() or {}
        free = float(acct.get("free_margin", 0) or 0)
        used = float(acct.get("margin", 0) or 0)
        usage_pct = (100.0 * used / (free + used)) if (free + used) else 0.0
        return templates.margin(
            level=float(acct.get("margin_level", 0) or 0),
            free=free, used=used, usage_pct=usage_pct,
        )

    def _cmd_health(self) -> str:
        # Minimal placeholder until Phase 7 wires analytics.health_monitor.
        if not self.engine:
            return "No engine attached."
        acct = self.engine.connector.get_account_info() or {}
        risk_state = getattr(self.engine.risk, "state", None)
        consec = getattr(risk_state, "consecutive_losses", 0)
        breakers = getattr(risk_state, "breakers_triggered", []) or []
        kill = getattr(risk_state, "kill_switch_active", False)
        return (
            f"Health\n"
            f"Balance: {float(acct.get('balance', 0) or 0):,.2f}\n"
            f"Margin level: {float(acct.get('margin_level', 0) or 0):.0f}%\n"
            f"Consecutive losses: {consec}\n"
            f"Breakers: {breakers or 'none'}\n"
            f"Kill switch: {kill}\n"
            f"Paused: {self.engine.state.paused}"
        )

    def _cmd_set_risk(self, args: list[str]) -> str:
        if not args:
            cur = self.cfg.get("risk", {}).get("risk_per_trade_pct", "?")
            return f"Current risk_per_trade_pct: {cur}"
        try:
            val = float(args[0])
        except ValueError:
            return "Usage: /risk <number>"
        maxv = float(self.cfg.get("risk", {}).get("max_risk_per_trade_pct", 2.0))
        if val <= 0 or val > maxv:
            return f"Risk must be in (0, {maxv}]"
        self.cfg.setdefault("risk", {})["risk_per_trade_pct"] = val
        if self.engine and hasattr(self.engine, "risk"):
            self.engine.risk.state.current_risk_pct = val
            try:
                self.engine.risk._save()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return f"risk_per_trade_pct set to {val}"

    def _cmd_set_maxlot(self, args: list[str]) -> str:
        if not args:
            cur = self.cfg.get("risk", {}).get("max_lot_size", "?")
            return f"Current max_lot_size: {cur}"
        try:
            val = float(args[0])
        except ValueError:
            return "Usage: /maxlot <number>"
        ceiling = float(self.cfg.get("sanity_check", {}).get("max_lot_hard_ceiling", 1.0))
        if val <= 0 or val > ceiling:
            return f"max_lot must be in (0, {ceiling}]"
        self.cfg.setdefault("risk", {})["max_lot_size"] = val
        return f"max_lot_size set to {val}"

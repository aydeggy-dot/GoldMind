"""Telegram notifier tests.

No real network or real threads — we drive the sender and listener paths
synchronously via internal methods and inject a FakeHTTP transport.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config import load_config
from database import DBManager
from notifications import templates
from notifications.telegram_bot import TelegramNotifier, _TokenBucket


# ----------------------------------------------------------------------
class FakeHTTP:
    """In-memory Telegram API. Captures sends, serves queued getUpdates."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._updates: list[dict[str, Any]] = []

    def post(self, method: str, payload: dict, timeout: int = 30) -> dict:
        if method == "sendMessage":
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        if method == "getUpdates":
            out = self._updates
            self._updates = []
            return {"ok": True, "result": out}
        return {"ok": False, "description": "unknown"}

    def queue_message(self, text: str, chat_id: str, update_id: int = 1) -> None:
        self._updates.append({
            "update_id": update_id,
            "message": {"chat": {"id": int(chat_id)}, "text": text},
        })


# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config("config/config.example.yaml")


@pytest.fixture()
def db(tmp_path: Path) -> DBManager:
    d = DBManager(tmp_path / "tg.db")
    yield d
    d.close()


def _fake_engine(cfg: dict, db: DBManager):
    """Minimal engine stand-in covering the attributes TelegramNotifier touches."""
    class Connector:
        def get_account_info(self):
            return {"balance": 500.0, "equity": 505.0, "free_margin": 490.0,
                    "margin": 10.0, "margin_level": 5000.0}
        def get_positions(self, magic_number=None, symbol=None):
            return []

    class RiskStub:
        state = SimpleNamespace(kill_switch_active=False, consecutive_losses=0,
                                breakers_triggered=[], current_risk_pct=1.0)
        def _save(self): pass

    engine = SimpleNamespace(
        connector=Connector(),
        symbol="XAUUSD",
        state=SimpleNamespace(paused=False),
        risk=RiskStub(),
        cfg=cfg,
        paused_flag=False,
    )
    engine.pause = lambda: setattr(engine.state, "paused", True)
    engine.resume = lambda: setattr(engine.state, "paused", False)
    engine.close_all = lambda: [{"ticket": 1, "success": True}]
    return engine


def _notifier(cfg, db, engine, http) -> TelegramNotifier:
    return TelegramNotifier(
        bot_token="x", chat_id="42",
        engine=engine, db=db, config=cfg, http=http,
        rate_limit_per_minute=10,
    )


# ----------------------------------------------------------------------
# Templates are pure
# ----------------------------------------------------------------------
def test_template_trade_open_contains_rr():
    msg = templates.trade_open(
        direction="LONG", symbol="XAUUSD", lot=0.04, fill_price=2050.0,
        sl=2045.0, tp=2060.0, setup_type="trend_continuation",
        confidence=0.72, strategy_version="1.0.0")
    assert "OPEN LONG" in msg and "R:R=" in msg and "1.0.0" in msg


def test_template_kill_switch_urgent_content():
    msg = templates.kill_switch(reason="daily loss", balance=95.0)
    assert "KILL SWITCH" in msg and "95.00" in msg


# ----------------------------------------------------------------------
# Token bucket
# ----------------------------------------------------------------------
def test_token_bucket_rate_limits_after_burst():
    b = _TokenBucket(rate_per_minute=10)
    # 10 tokens available; 11th should require a wait
    for _ in range(10):
        assert b.take() == 0.0
    wait = b.take()
    assert wait > 0.0


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def test_status_command_returns_summary(cfg, db):
    eng = _fake_engine(cfg, db)
    # Bypass regime/macro calls via stubs
    eng._fetch_snapshot = lambda: SimpleNamespace(h1=None)
    class RegStub:
        def detect(self, h1):
            return SimpleNamespace(regime=SimpleNamespace(value="TRENDING_BULLISH"))
    class MacStub:
        def evaluate(self):
            return SimpleNamespace(bias=SimpleNamespace(value="BULLISH"))
    eng.regime = RegStub()
    eng.macro = MacStub()
    n = _notifier(cfg, db, eng, FakeHTTP())
    reply = n._handle_command("/status")
    assert "Status" in reply and "XAUUSD" in reply


def test_pause_and_resume_update_engine_state(cfg, db):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    n._handle_command("/pause")
    assert eng.state.paused is True
    n._handle_command("/resume")
    assert eng.state.paused is False


def test_closeall_requires_confirmation(cfg, db):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    first = n._handle_command("/closeall")
    assert "Confirm" in first
    # Non-yes reply cancels
    assert "Cancelled" in n._handle_command("no")
    # Re-issue + yes executes
    n._handle_command("/closeall")
    result = n._handle_command("yes")
    assert "close_all issued" in result


def test_kill_requires_confirmation_and_engages(cfg, db):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    n._handle_command("/kill")
    reply = n._handle_command("yes")
    assert "Kill switch engaged" in reply
    assert eng.risk.state.kill_switch_active is True
    assert eng.state.paused is True


def test_confirmation_expires_after_ttl(cfg, db, monkeypatch):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    n._handle_command("/closeall")
    # Fast-forward the internal monotonic clock
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 999)
    reply = n._handle_command("/status")
    # Pending should have been cleared; /status works normally (no "Confirm")
    assert "Confirm" not in reply


def test_risk_command_validates_bounds(cfg, db):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    assert "must be" in n._handle_command("/risk 99").lower()
    ok = n._handle_command("/risk 1.5")
    assert "1.5" in ok
    assert cfg["risk"]["risk_per_trade_pct"] == 1.5


def test_maxlot_command_enforces_ceiling(cfg, db):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    ceiling = cfg["sanity_check"]["max_lot_hard_ceiling"]
    assert "must be" in n._handle_command(f"/maxlot {ceiling + 1}").lower()
    assert "set to" in n._handle_command("/maxlot 0.05").lower()


def test_trades_command_reads_db(cfg, db):
    eng = _fake_engine(cfg, db)
    db.insert_trade({
        "ticket": 7, "type": "LONG", "setup_type": "trend_continuation",
        "strategy_version": "1.0.0", "entry_price": 2050.0, "stop_loss": 2045.0,
        "take_profit": 2060.0, "requested_lot": 0.04, "filled_lot": 0.04,
        "partial_fill": 0, "confidence": 0.7, "regime": "TRENDING_BULLISH",
        "macro_bias": "BULLISH", "session": "NY_OVERLAP",
        "margin_level_at_entry": 5000.0, "entry_time": "2026-04-13T12:00:00+00:00",
        "is_backtest": 0, "notes": "",
    })
    n = _notifier(cfg, db, eng, FakeHTTP())
    reply = n._handle_command("/trades")
    assert "#7" in reply and "LONG" in reply


def test_unauthorized_chat_id_rejected(cfg, db):
    eng = _fake_engine(cfg, db)
    http = FakeHTTP()
    n = _notifier(cfg, db, eng, http)
    http.queue_message("/status", chat_id="999")  # wrong chat
    # Manually drive one poll cycle by calling _dispatch_update
    n._dispatch_update({"update_id": 1,
                        "message": {"chat": {"id": 999}, "text": "/status"}})
    assert http.sent and "Unauthorized" in http.sent[-1]["text"]


# ----------------------------------------------------------------------
# Sender — enqueue goes through token bucket + HTTP
# ----------------------------------------------------------------------
def test_notify_sends_via_http(cfg, db):
    eng = _fake_engine(cfg, db)
    http = FakeHTTP()
    n = _notifier(cfg, db, eng, http)
    n.notify("trade", "OPEN LONG test", urgent=False)
    # Drain manually (simulates one sender iteration)
    text, _ = n._queue.get_nowait()
    n._send_raw(text)
    assert http.sent and "OPEN LONG test" in http.sent[-1]["text"]


def test_unknown_command_returns_message(cfg, db):
    eng = _fake_engine(cfg, db)
    n = _notifier(cfg, db, eng, FakeHTTP())
    assert "Unknown command" in n._handle_command("/wat")

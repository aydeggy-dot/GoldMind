# CLAUDE.md — GoldMind project context

> Read this first. Compact-resilient context for any Claude session working on this repo.

## What this is
Autonomous Python bot trading XAUUSD on MetaTrader 5, unattended on Windows VPS.
$500 starting balance. **Capital preservation is job #1, profit is #2.**
Full product spec: `GOLDMIND_PROMPT.md`. Build progress: `tasks/todo.md`.

## Status
- **All 9 phases complete.** Code-complete; remaining work is operational.
- **160 tests green.** Run with `python -m pytest tests/` from project root.
- Next step is operational: run `scripts\install.bat` on the VPS,
  then follow `scripts\go_live_checklist.md` (2-week demo, then 0.01 lots).

## Architectural invariants (do not violate)

| Rule | Why |
|------|-----|
| Only `core/mt5_connector.py` may `import MetaTrader5`. | Single point of MT5 ownership; lockable; testable. |
| MT5 import is wrapped in `try/except` (`_MT5_AVAILABLE` flag). | Tests run on non-Windows / without MT5 installed. |
| All MT5 calls go through `self._lock` (RLock). | The MT5 Python API is not thread-safe. |
| Always use `get_closed_candles(...)` for signals — never raw `copy_rates_from_pos`. | The current bar is incomplete; using it produces false signals. |
| Never hardcode pip / point value. Use `utils.helpers.point_value_per_lot(symbol_info)` = `(tick_value/tick_size)*point`. | Broker specs vary; XAUUSD ≠ XAUUSDm. |
| Naive datetimes are banned. `SessionManager` and `to_tz()` reject them. | DST and timezone bugs lose money. |
| Never hardcode UTC offsets. Use `ZoneInfo("America/New_York")` etc. | London/NY/Tokyo each DST-shift on different dates. |
| Always fetch FRESH `symbol_info()` before position sizing. | Specs can change mid-session; cached values lie. |
| The independent **sanity check** layer is mandatory, not optional. | It catches bugs in `calculate_position_size()` itself. |
| Validate ALL candle data with `DataValidator` before any signal calculation. | One NaN → false signal → real loss. |
| Never widen SL — only tighten or move to BE. | The whole risk model assumes worst-case = original SL. |
| Persist risk + engine state to DB on every change. | The bot WILL crash; it must resume without re-arming breakers. |
| SQLite WAL mode is mandatory (`PRAGMA journal_mode=WAL`). | Survives unclean shutdown. |
| **Do not** add `detect_types=sqlite3.PARSE_DECLTYPES` — its TIMESTAMP converter chokes on ISO 'T' format. We store ISO strings as TEXT. | Burned us in Phase 1; don't reintroduce. |

## Module ownership

```
config/__init__.py        — load + validate config.yaml + credentials.yaml
core/mt5_connector.py     — ONLY module touching MetaTrader5
core/data_validator.py    — guards all candle/tick data
core/session_manager.py   — DST-safe; broker tz; Asian range; friday close
core/regime_detector.py   — ADX + ATR + EMA → Regime enum
core/macro_filter.py      — DXY/US10Y/VIX, broker→yfinance fallback, TTL cache
core/news_filter.py       — high-impact event window; FOMC longer
core/strategy.py          — pure signals; 3 setups; confidence scoring
core/risk_manager.py      — sizing + sanity + breakers + kill switch (DB-persisted)
core/trade_manager.py     — pure action generator; engine executes the actions
core/engine.py            — orchestrator; 13-step tick loop
database/db_manager.py    — SQLite WAL; KV state; trades/signals/baselines
notifications/telegram_bot.py — queued sender + long-poll listener; 13 commands
notifications/templates.py    — pure text formatters for every alert
analytics/performance.py      — metrics over trade dicts (live + backtest share shape)
analytics/health_monitor.py   — strategy/system/broker checks; returns HealthReading
analytics/dashboard.py        — daily/weekly report builders + persist_daily
backtesting/backtester.py     — bar-driven simulator reusing live Strategy
backtesting/walk_forward.py   — IS/OOS windows + spec validity criteria
backtesting/report_generator.py — plain-text reports for CLI/log/Telegram
scripts/watchdog.py           — process watchdog (injection-friendly)
scripts/preflight.py          — 8 pre-flight checks before go-live
scripts/install.bat           — idempotent VPS setup
main.py                   — entry point + signal handlers (no panic close on shutdown)
```

## Conventions

- Type hints everywhere; docstrings on every public function.
- New module → matching `tests/test_<name>.py`. Aim for behavior tests, not coverage theatre.
- Risk paths get extra sanity tests (sanity_check rejection, kill switch, partial fill).
- `Notifier` and `HealthMonitor` are `Protocol` types — engine accepts no-op defaults; Phase 6/7 will inject real ones.
- Don't add features the spec didn't ask for. Don't refactor outside the phase.
- Comments only when WHY is non-obvious (a workaround, an invariant). The code says WHAT.

## Gotchas already learned

1. **`config.example.yaml` had `warm_up.required_bars.H1: 100`.** The slow EMA is 200 → must be ≥ 250. Bumped H1→300, M15→300, M5→200, D1→60. Don't lower these.
2. **Adaptive sizing reduces lot to 0.5× when ATR ratio ≥ 1.5.** On a $500 account with ~400-pt SL this can underflow `min_lot`. Engine reports `"size underflow"` to the signals table — that's correct, it's the safety layer working. Tests use $5000 to exercise the happy path.
3. **`order_send` partial fills** — `result.volume < requested` does happen on small accounts. Always call `risk.handle_partial_fill()`. If actual < `volume_min`, close the position immediately.
4. **MT5 server time vs system time** — `connector.get_clock_drift()` returns `system_now - broker_now`. > 30s warns; > 120s pauses trading.
5. **`positions_get(magic=...)` filter is wrong on some MT5 builds** — we filter in Python via `[p for p in positions if int(p.magic) == magic_number]`.

## Run commands

```bash
# Tests (must work on the dev box without MT5 installed)
python -m pytest tests/ -v

# Single module
python -m pytest tests/test_risk_manager.py -v

# Smoke check that config loads + DB initializes
python -c "from config import load_all; from database import DBManager; load_all(); print('OK')"

# On a Windows VPS with MT5 installed:
python main.py
```

## Where things live

- Active task list: `tasks/todo.md`
- Lessons / corrections: `tasks/lessons.md` (create only when needed)
- Logs: `logs/goldmind.log` (rotating, 10MB × 5)
- DB: `data/goldmind.db` (gitignored, WAL files too)
- User config: `config/config.yaml` (copy of `.example.yaml` initially)
- User secrets: `config/credentials.yaml` (gitignored — never commit)

## When in doubt
Do nothing rather than something risky. The bot's job is to refuse to trade
when uncertain. Every "skip_reason" in the signals table is a feature.

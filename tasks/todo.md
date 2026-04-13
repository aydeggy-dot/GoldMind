# GoldMind Build Plan

## Phase 1: Foundation ✅ COMPLETE
- [x] Directory structure
- [x] requirements.txt, .gitignore, README.md
- [x] config/config.example.yaml (full spec from prompt)
- [x] config/credentials.example.yaml
- [x] config loader with validation (config/__init__.py)
- [x] utils/logger.py — rotating file handler
- [x] utils/constants.py — enums, magic constants
- [x] utils/helpers.py — timezone-safe time, dynamic pip/point calc
- [x] database/models.py — schema definitions
- [x] database/db_manager.py — SQLite WAL mode, CRUD, state
- [x] core/mt5_connector.py — connection, discovery, data fetch, clock drift
- [x] tests/test_config.py, test_db.py, test_helpers.py — 20/20 passing
- [ ] **User verification on VPS**: pip install -r requirements.txt; configure
      credentials.yaml; run `python -c "from core.mt5_connector import MT5Connector; ..."`
      to connect, discover XAUUSD, fetch H4 candles, log clock drift.

## Review (Phase 1)
Built foundation: config/credentials with schema validation, rotating logger,
SQLite WAL DB with state KV + trades/signals/baseline tables, and MT5 connector
(connection, symbol discovery with fallbacks + brute-force, fresh symbol_info,
closed-candles-only fetch, clock drift detection). Bug caught and fixed:
PARSE_DECLTYPES + ISO timestamps clashed with deprecated default converter.
20 unit tests pass without MT5 installed (connector imports defensively).

## Phase 2: Safety Layer ✅ COMPLETE
- [x] core/data_validator.py — empty, NaN, zero, impossible-candle, stale,
      price-gap, dead-feed, non-monotonic; tick: inverted, stale, huge spread
- [x] core/session_manager.py — DST-safe sessions (Asian/London/NY/overlap/
      dead/weekend), holidays, early close, friday close, Asian range
- [x] tests: 13 validator + 13 session tests, all DST transitions covered
- [x] 46/46 total tests passing

## Phase 3: Intelligence ✅ COMPLETE
- [x] core/regime_detector.py — ADX/ATR + EMA alignment;
      TRENDING_BULLISH/BEARISH/RANGING/VOLATILE_CRISIS/TRANSITIONING/UNKNOWN
- [x] core/macro_filter.py — DXY/US10Y/VIX with broker-first symbol resolver,
      yfinance fallback (TTL cache), MacroBias synthesis with vote logic
- [x] core/news_filter.py — high-impact event block windows, FOMC longer windows,
      pluggable EventFetcher; MT5 calendar adapter included
- [x] core/strategy.py — H4 bias, key levels (PDH/PDL/Asian/Swing/Psych),
      3 setups (sweep_reversal, trend_continuation, flag_breakout),
      confidence scoring, SL bounds enforcement, R:R targeting
- [x] tests: 6 regime + 5 macro + 8 news + 7 strategy = 26 new
- [x] 72/72 total tests passing

## Phase 4: Protection ✅ COMPLETE
- [x] core/risk_manager.py — RiskState (DB-persisted), can_trade gate (kill switch,
      balance, drawdown, margin level, daily loss, cooldown, concurrent),
      calculate_position_size (compounding + adaptive multipliers),
      sanity_check_lot (independent ceiling), check_margin_before_order,
      validate_signal (R:R / SL bounds / spread / duplicate),
      handle_partial_fill, update_after_trade, period resets, kill switch
- [x] core/trade_manager.py — TradeAction generator (no execution side effects):
      MAX_DURATION close, friday close, swap-aware BE close, partial @ 1R + BE,
      trailing stop after activation R:R, soft BE move
- [x] tests: 24 risk + 8 trade = 32 new
- [x] 104/104 total tests passing

## Phase 5: Execution ✅ COMPLETE
- [x] mt5_connector — check_margin_for_order, place_order (partial-fill detect),
      modify_position, close_position, close_all_positions, get_positions/history
- [x] core/engine.py — Engine orchestrator: Notifier/HealthMonitor protocols,
      startup (connect→discover→baseline→clock→warm-up→reconcile),
      tick (13-step loop), system health + auto-reconnect + kill switch,
      daily reset + broker spec change diff + clock drift, manage existing
      via TradeManager actions, signal pipeline (validate→size→sanity→margin→
      execute→partial-fill→persist), pause/resume/close_all hooks
- [x] main.py — entry point, signal handlers (SIGINT/SIGTERM/SIGBREAK),
      DB integrity check, no panic close on shutdown
- [x] scripts/run_bot.bat
- [x] tests: 9 engine integration tests with FakeConnector
- [x] 113/113 total tests passing
- [x] Config update: warm_up.required_bars.H1 bumped from 100 -> 300 (the spec
      value was below slow_ema=200 + ADX(42) headroom). M15: 200->300, M5: 100->200, D1: 30->60.

## Phase 6: Communication ✅ COMPLETE
- [x] notifications/templates.py — pure formatters for trade open/close,
      partial close, circuit breaker, kill switch, sanity failure, margin/clock
      warnings, broker spec change, spread regime change, strategy health,
      daily/weekly report, status/trades/uptime/version/margin
- [x] notifications/telegram_bot.py — TelegramNotifier (engine Notifier protocol):
      queued sender thread + token-bucket rate limit (10/min default),
      long-poll listener thread (getUpdates), 13 commands
      (/status /pause /resume /closeall /report /risk /maxlot /kill /health
      /trades /uptime /version /margin), confirmation flow with 60s TTL for
      /closeall + /kill, unauthorized chat rejection, pluggable HTTP adapter
- [x] main.py wires TelegramNotifier from credentials.yaml when telegram.enabled
- [x] tests: 14 telegram tests (templates, token bucket, every command path,
      confirmation flow, TTL expiry, unauthorized chat, send path)
- [x] 127/127 total tests passing

## Review (Phase 6)
TelegramNotifier implements the engine's Notifier protocol so it plugs in with
no engine changes. Sender and listener run as daemon threads and share a single
HTTP adapter (requests.Session by default; tests inject a FakeHTTP). Destructive
commands require a "yes" reply within 60 seconds — tested for execute, cancel,
and TTL expiry. /risk and /maxlot mutate the live config dict and propagate to
the RiskManager state so changes take effect on the next tick. Chose a plain
HTTP adapter over python-telegram-bot's asyncio runtime because it fits the
"separate thread, queued, rate limited" spec without importing an event loop.

## Phase 7: Analytics & Health ✅ COMPLETE
- [x] analytics/performance.py — rolling 50-trade metrics: win_rate,
      profit_factor, expectancy, sharpe, avg_rr_achieved, max_drawdown
      (currency + duration), total_swap_costs, partial_fill_count,
      groupings by session/setup/day-of-week/strategy_version,
      best/worst breakouts
- [x] analytics/health_monitor.py — HealthMonitor (engine's HealthMonitor
      protocol): heartbeat (psutil mem/cpu/disk with graceful fallback),
      on_trade_closed (6 strategy checks — pause when 2+ fail),
      broker_health_check (spread regime change)
- [x] analytics/dashboard.py — build_daily_report / build_weekly_report
      (pure), daily_message / weekly_message (via templates),
      persist_daily (UPSERT on date)
- [x] engine._record_trade_close — updates trades row (exit_price, pnl,
      exit_time, exit_reason, rr_achieved, duration_minutes), calls
      risk.update_after_trade, feeds HealthMonitor, auto-pauses on degradation
- [x] main.py wires HealthMonitor
- [x] tests: 14 analytics (performance/metrics, health on_close, heartbeat,
      broker spread regime, dashboard upsert/format)
- [x] 141/141 total tests passing

## Review (Phase 7)
Analytics is a pure module — all metric functions take a list of trade dicts
and return a PerformanceMetrics dataclass. The same shape comes from live
trades and the backtester, so Phase 8 will reuse this unchanged.
HealthMonitor returns a `HealthReading` (pause flag + alerts) — the engine
decides what to do, which keeps the monitor trivially testable without
mocking an Engine. psutil is imported defensively so tests stay lightweight
on machines without it. The Phase 5 engine never called
`risk.update_after_trade` because trade closes were never recorded; fixed
as part of this phase via `_record_trade_close` which also drives the
strategy-health auto-pause.

## Phase 8: Backtesting ✅ COMPLETE
- [x] backtesting/backtester.py — bar-driven simulator reusing core.strategy
      + core.regime_detector unchanged. Simulates spread, random 0..N point
      slippage, SL/TP detection on bar high/low (SL wins ties), partial
      close at 1R + move-to-BE, trailing activation, max duration, daily
      swap. Emits trade dicts compatible with analytics.compute_metrics.
- [x] backtesting/walk_forward.py — rolling IS/OOS windows with spec
      criteria: OOS PF > 1.0 in 60%+, OOS WR > 40% in 60%+, OOS DD < 20%
      in ALL; minimum_trades gate per window
- [x] backtesting/report_generator.py — plain-text reports for single
      backtest + walk-forward (same text works for CLI/log/Telegram)
- [x] tests: 7 backtesting (end-to-end, deterministic seed, criteria
      pass/fail, text reports)
- [x] 148/148 total tests passing

## Review (Phase 8)
Backtester reuses the live Strategy and RegimeDetector with zero duplication;
only execution mechanics (spread/slippage/SL-TP detection/swap/partial/
trailing) are simulated. Output trade dicts are the same shape as live
trades, so analytics/performance works for both unchanged. Walk-forward
answers the stability question — our strategy has no fitted parameters, so
each window just validates that live-like behavior holds on unseen slices.
News + macro are intentionally stubbed in backtest mode; the spec scopes
historical replay to execution, not news feed reconstruction.

## Phase 9: Production ✅ COMPLETE
- [x] scripts/install.bat — idempotent VPS setup: requires admin,
      resolves project dir, disables screen lock / auto-reboot / screensaver,
      configures NTP + forced resync, creates 3 scheduled tasks
      (GoldMind Bot onstart, Watchdog every 30 min, NTP Sync hourly),
      prefers venv Python if present
- [x] scripts/watchdog.py — psutil process scan; if python.exe running
      main.py is missing, relaunches in a detached console. Injection
      points (process_lister + launcher) so tests run without psutil
- [x] scripts/preflight.py — PreflightReport with 8 checks (config load,
      credential placeholders, DB integrity, connector init, MT5 connect,
      symbol discovery, broker spec sanity, balance floor, clock drift).
      connector_factory injection for tests
- [x] scripts/go_live_checklist.md — 8-section operator checklist covering
      VPS provisioning, config, pre-flight, install.bat, 2-week demo soak,
      go-live with 0.01 lots, scale-up, incident playbook
- [x] tests: 12 scripts tests (watchdog process matching, restart path,
      launch failure, preflight happy/placeholder/balance/drift/connect
      fail paths, report rendering)
- [x] 160/160 total tests passing

## Review (Phase 9)
Scripts are written defensively — install.bat requires admin and is
idempotent; watchdog does not import Engine/DB so it cannot fail from
whatever broke the main process; preflight uses injection so `pytest`
runs green on a dev box with no MT5 installed. The go-live checklist is
the operational contract: a 2-week demo soak is mandatory before any
live cent, and the first 3 live trades are watched manually to confirm
server-side SL/TP. Hardcoded in the USDT-deposit warning in section 6
because the spec and the original README both flagged this as a
real-money loss vector at fund time.

## ALL PHASES COMPLETE
Phases 1–9 delivered. 160 tests green. The bot is code-complete; the
remaining work is operational (VPS provisioning, the 2-week demo, then
go-live with 0.01 lots per the checklist).

## Notes
- All credentials in credentials.yaml (gitignored)
- SQLite WAL mode mandatory
- NEVER hardcode pip values, UTC offsets, or symbol names
- Type hints + docstrings on everything

# PROJECT: AUTONOMOUS XAUUSD GOLD TRADING BOT — "GOLDMIND"

## ROLE & CONTEXT

You are the world's most elite quantitative trading systems engineer with 25+ years of experience building institutional-grade autonomous trading systems. You have deep expertise in Python, MetaTrader 5 API integration, algorithmic trading, risk management, macro-economic analysis, market microstructure, and production-grade software engineering. You are building this system for a retail trader who will run it unattended on a Windows VPS with $500 USD starting capital trading XAUUSD (Gold) CFDs on MetaTrader 5.

**IMPORTANT:** The user may hold funds in USDT. MT5 accounts are denominated in fiat (USD, EUR, etc.), not stablecoins. The user must convert USDT to USD and deposit via their broker's supported payment methods before the bot can trade. Document this clearly in the README.

---

## PROJECT OVERVIEW

Build a **fully autonomous, unattended** Python-based trading bot called **"GoldMind"** that:

1. Connects to MetaTrader 5 and trades XAUUSD (Gold CFDs) autonomously 24/5
2. Implements a multi-layered institutional-grade strategy with macro awareness
3. Manages risk through adaptive position sizing, margin monitoring, and multi-level circuit breakers
4. Detects market regimes and adapts behavior accordingly
5. Sends real-time trade notifications and daily reports via Telegram
6. Logs every decision, trade, and metric to a database for analytics
7. Provides a Telegram command interface for remote human override
8. Runs reliably on a Windows VPS without human intervention
9. Includes comprehensive backtesting and walk-forward testing capabilities
10. Self-monitors for strategy degradation, broker changes, and system health — alerts when edge is lost
11. Validates all incoming data, order fills, and position sizes with independent safety checks
12. Manages account growth through controlled compounding rules

---

## TECH STACK (Mandatory)

- **Language:** Python 3.10+
- **Trading Platform:** MetaTrader 5 via `MetaTrader5` Python package
- **Database:** SQLite with WAL mode (for crash-safe writes)
- **Notifications:** Telegram Bot API via `python-telegram-bot` library
- **Data Analysis:** pandas, numpy
- **Technical Indicators:** `ta` library (or pandas_ta)
- **Scheduling:** `schedule` library + custom event loop
- **Configuration:** YAML config files (never hardcode credentials or parameters)
- **Logging:** Python `logging` module with rotating file handlers
- **Timezone:** `pytz` or `zoneinfo` (Python 3.9+) for DST-safe time handling
- **System Monitoring:** `psutil` for memory, CPU, disk monitoring
- **Macro Data Fallback:** `yfinance` + `requests` for DXY/VIX/Yields if broker lacks symbols
- **Environment:** Windows 10/11 or Windows Server 2019/2022

---

## REQUIREMENTS.TXT

```
MetaTrader5>=5.0.45
pandas>=2.0.0
numpy>=1.24.0
ta>=0.11.0
python-telegram-bot>=20.0
PyYAML>=6.0
pytz>=2024.1
schedule>=1.2.0
psutil>=5.9.0
requests>=2.31.0
yfinance>=0.2.30
pytest>=7.4.0
pytest-asyncio>=0.21.0
```

---

## DIRECTORY STRUCTURE

```
goldmind/
├── config/
│   ├── config.yaml
│   ├── config.example.yaml
│   ├── credentials.yaml              # gitignored
│   └── credentials.example.yaml
├── core/
│   ├── __init__.py
│   ├── engine.py                     # Main orchestrator
│   ├── mt5_connector.py              # MT5 connection, data, orders
│   ├── strategy.py                   # Signal generation
│   ├── risk_manager.py               # Position sizing, circuit breakers, margin monitor
│   ├── regime_detector.py            # Market regime classification
│   ├── macro_filter.py               # DXY, Yields, VIX analysis
│   ├── news_filter.py                # Economic calendar + holidays
│   ├── session_manager.py            # DST-safe session timing
│   ├── trade_manager.py              # Trailing stops, partials, BE, swap management
│   └── data_validator.py             # Candle data integrity checks
├── notifications/
│   ├── __init__.py
│   ├── telegram_bot.py
│   └── templates.py
├── analytics/
│   ├── __init__.py
│   ├── performance.py
│   ├── dashboard.py
│   └── health_monitor.py             # Strategy + system + broker health
├── backtesting/
│   ├── __init__.py
│   ├── backtester.py
│   ├── walk_forward.py
│   └── report_generator.py
├── database/
│   ├── __init__.py
│   ├── db_manager.py                 # SQLite with WAL mode for crash safety
│   └── models.py
├── utils/
│   ├── __init__.py
│   ├── logger.py
│   ├── helpers.py
│   └── constants.py
├── tests/
│   ├── test_strategy.py
│   ├── test_risk_manager.py
│   ├── test_regime_detector.py
│   ├── test_position_sizing.py
│   ├── test_circuit_breakers.py
│   ├── test_data_validator.py
│   └── test_sanity_checks.py
├── scripts/
│   ├── install.bat                   # VPS setup (registry, Task Scheduler, NTP)
│   ├── run_bot.bat
│   ├── watchdog.py
│   ├── run_backtest.py
│   └── setup_telegram.py
├── logs/
├── data/
├── main.py                           # Entry point with graceful shutdown
├── requirements.txt
├── .gitignore
└── README.md
```

---

## .GITIGNORE

```
config/credentials.yaml
data/*.db
data/*.db-journal
data/*.db-wal
logs/
__pycache__/
*.pyc
*.pyo
venv/
.venv/
.vscode/
.idea/
Thumbs.db
.DS_Store
data/backtest_results/
data/cache/
```

---

## CREDENTIALS TEMPLATE (credentials.example.yaml)

```yaml
# Copy to credentials.yaml. NEVER commit credentials.yaml.
mt5:
  account: 12345678
  password: "your_password_here"
  server: "YourBroker-Live"
  terminal_path: "C:\\Program Files\\MetaTrader 5\\terminal64.exe"

telegram:
  bot_token: "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
  chat_id: "987654321"
```

---

## CONFIGURATION FILE (config.yaml) — ALL PARAMETERS

```yaml
# ============================================================
# GOLDMIND — XAUUSD AUTONOMOUS TRADING BOT
# ============================================================

# --- STRATEGY VERSION ---
strategy_version:
  version: "1.0.0"
  version_notes: "Initial — 3 setups, macro filter, regime detection"

# --- MT5 ---
mt5:
  symbol: "XAUUSD"
  symbol_fallbacks: ["XAUUSDm", "XAUUSD.", "XAUUSD.i", "GOLD", "Gold", "GOLDm"]
  magic_number: 123456
  deviation: 20
  filling_type: "IOC"

# --- GOLD CONTRACT ---
# Defaults only — bot MUST verify via symbol_info() on startup.
# NEVER hardcode pip values. Always: (tick_value / tick_size) * point
gold_specs:
  typical_contract_size: 100
  typical_spread_points: 20

# --- STRATEGY ---
strategy:
  bias_timeframe: "H4"
  bias_ema_period: 200
  structure_timeframe: "H1"
  entry_timeframe: "M15"
  precision_timeframe: "M5"
  fast_ema: 50
  slow_ema: 200
  enable_sweep_reversal: true
  enable_trend_continuation: true
  enable_flag_breakout: true
  lookback_days: 10
  level_touch_tolerance: 50
  psychological_levels: true

# --- SESSIONS (DST-safe — NEVER hardcode UTC offsets) ---
sessions:
  broker_timezone: "auto"
  asian:
    timezone: "Asia/Tokyo"
    start: "07:00"
    end: "16:00"
  london:
    timezone: "Europe/London"
    start: "08:00"
    end: "16:30"
  new_york:
    timezone: "America/New_York"
    start: "08:00"
    end: "17:00"
  trade_sessions: ["london", "ny_overlap"]
  dead_zone_start: "16:00"
  dead_zone_end: "19:00"

# --- RISK MANAGEMENT ---
risk:
  risk_per_trade_pct: 1.0
  max_risk_per_trade_pct: 2.0
  min_lot_size: 0.01
  max_lot_size: 0.10
  max_concurrent_trades: 2
  min_rr_ratio: 2.0
  target_rr_ratio: 2.5
  min_sl_points: 200
  max_sl_points: 500
  wiggle_room_points: 30
  partial_close_pct: 50
  move_to_be_after_partial: true
  trailing_stop_enabled: true
  trailing_stop_distance: 150
  max_spread_points: 40

# --- MARGIN MONITORING ---
margin:
  min_margin_level: 200         # Never open new trade if margin level < 200%
  warning_margin_level: 150     # Alert at 150%
  danger_margin_level: 100      # Close weakest position at 100%
  # Always verify margin BEFORE placing order:
  # required_margin = mt5.order_calc_margin(type, symbol, lot, price)
  # if required_margin > free_margin * 0.5 → SKIP (keep 50% margin buffer)
  max_margin_usage_pct: 50      # Never use more than 50% of free margin

# --- LOT SIZE SANITY CHECK ---
# Independent safety layer that runs AFTER position sizing calculation.
# Catches bugs in the calculation logic itself.
sanity_check:
  enabled: true
  max_lot_hard_ceiling: 1.0     # NEVER allow > 1.0 lot on a small account
  max_risk_pct_per_position: 5  # Even with a bug, never risk >5% on one trade
  # If calculated lot fails sanity check → fall back to min_lot or refuse to trade

# --- CIRCUIT BREAKERS ---
circuit_breakers:
  max_daily_loss_pct: 3.0
  max_consecutive_losses: 3
  consecutive_loss_cooldown_hours: 4
  max_weekly_drawdown_pct: 7.0
  max_total_drawdown_pct: 15.0
  min_account_balance: 100.0

# --- ADAPTIVE POSITION SIZING ---
adaptive_sizing:
  enabled: true
  after_2_losses_multiplier: 0.5
  after_3_losses_multiplier: 0.25
  high_atr_multiplier: 0.5
  drawdown_recovery_multiplier: 0.5

# --- COMPOUNDING & ACCOUNT GROWTH ---
compounding:
  enabled: true
  # Only increase base risk after sustained growth
  scale_up_at_pct: 20            # Increase risk after +20% from starting balance
  scale_up_increment: 0.25       # Add 0.25% risk per milestone
  max_risk_per_trade: 2.0        # Absolute ceiling regardless of growth
  reset_to_base_after_drawdown: true  # If DD >10%, reset to original 1% risk
  # Withdrawal safety
  track_withdrawals: true        # Log withdrawals, don't count as drawdown
  min_balance_after_withdrawal: 600  # Never withdraw below this (started at $500)

# --- REGIME DETECTION ---
regime:
  enabled: true
  adx_period: 14
  adx_trending_threshold: 25
  adx_ranging_threshold: 20
  atr_period: 14
  atr_spike_multiplier: 2.0
  regime_check_interval_minutes: 15
  regime_confirmation_bars: 3

# --- MACRO FILTERS ---
macro:
  enabled: true
  dxy_symbol: "USDX"
  dxy_symbol_fallbacks: ["DXY", "USDX.F", "DX.F", "Dollar_Index"]
  dxy_ema_period: 50
  dxy_filter_enabled: true
  dxy_strong_trend_adx: 30
  us10y_symbol: "US10Y.F"
  us10y_symbol_fallbacks: ["US10YY.F", "TNX", "US10Y"]
  us10y_filter_enabled: true
  yield_change_threshold: 0.05
  vix_symbol: "VIX.F"
  vix_symbol_fallbacks: ["VIX", "VIXM", "VIX.i"]
  vix_filter_enabled: true
  vix_risk_off_threshold: 25
  vix_extreme_threshold: 35
  use_web_api_fallback: true
  web_api_source: "yahoo_finance"
  web_api_cache_minutes: 15

# --- NEWS / ECONOMIC CALENDAR ---
news:
  enabled: true
  source: "mql5_calendar"
  block_events:
    - "NonFarm Payrolls"
    - "FOMC Rate Decision"
    - "CPI"
    - "PPI"
    - "Fed Chair Speech"
    - "GDP"
    - "Retail Sales"
    - "Initial Jobless Claims"
  pre_event_block_minutes: 30
  post_event_block_minutes: 15
  fomc_pre_block_minutes: 60
  fomc_post_block_minutes: 30

# --- MARKET HOLIDAYS ---
holidays:
  closed_dates_2026:
    - "2026-01-01"
    - "2026-04-03"
    - "2026-12-25"
  early_close_dates_2026:
    - "2026-11-26"
    - "2026-12-24"
    - "2026-12-31"
  early_close_time: "13:00"

# --- TRADE MANAGEMENT ---
trade_management:
  check_interval_seconds: 10
  trailing_activation_rr: 1.5
  max_trade_duration_hours: 24
  move_to_be_at_rr: 1.0
  close_all_before_weekend: true
  friday_close_time: "15:30"
  swap_aware: true
  close_breakeven_before_swap: true
  swap_rollover_time: "17:00"
  max_overnight_holds: 1

# --- DATA VALIDATION ---
data_validation:
  enabled: true
  max_candle_age_minutes: 30      # Reject data if last candle is older than this
  max_price_gap_pct: 5.0          # Alert if consecutive close gap > 5%
  reject_zero_prices: true
  reject_nan_values: true
  reject_high_less_than_low: true

# --- BROKER MONITORING ---
broker_monitoring:
  enabled: true
  # Detect if broker changes symbol specs
  check_symbol_specs_daily: true   # Compare against stored baseline on each daily reset
  alert_on_spec_change: true       # Alert if contract size, margin req, etc. change
  # Detect spread regime changes
  track_spread_average: true
  spread_regime_change_multiplier: 2.0  # Alert if 7-day avg spread > 2x 30-day avg
  # Detect leverage changes
  check_leverage_before_trade: true
  alert_on_leverage_change: true

# --- TELEGRAM ---
telegram:
  enabled: true
  notify_trade_open: true
  notify_trade_close: true
  notify_partial_close: true
  notify_circuit_breaker: true
  notify_errors: true
  notify_daily_report: true
  daily_report_time: "17:30"
  notify_weekly_report: true
  weekly_report_day: "friday"
  enable_commands: true
  max_messages_per_minute: 10

# --- DATABASE ---
database:
  path: "data/goldmind.db"
  wal_mode: true                  # Write-Ahead Logging — crash-safe writes
  backup_enabled: true
  backup_interval_hours: 24
  archive_after_months: 6

# --- LOGGING ---
logging:
  level: "INFO"
  log_file: "logs/goldmind.log"
  max_file_size_mb: 10
  backup_count: 5
  log_all_signals: true
  log_market_data: false

# --- BACKTESTING ---
backtesting:
  default_start_date: "2022-01-01"
  default_end_date: "2025-12-31"
  initial_balance: 500.0
  commission_per_lot: 0.0
  spread_simulation: 25
  slippage_simulation_points: 3   # Random 0-3 points slippage per trade
  walk_forward:
    in_sample_months: 6
    out_of_sample_months: 2
    minimum_trades: 30

# --- HEALTH MONITORING ---
health:
  enabled: true
  min_win_rate_threshold: 35.0
  min_profit_factor_threshold: 1.0
  evaluation_window_trades: 50
  auto_pause_on_degradation: true
  heartbeat_interval_minutes: 30
  mt5_reconnect_attempts: 5
  mt5_reconnect_delay_seconds: 30
  max_memory_mb: 500
  max_cpu_percent: 80
  min_disk_free_gb: 1.0
  # Clock drift monitoring
  max_clock_drift_seconds: 30     # Alert if system clock drifts from MT5 server
  pause_on_clock_drift_seconds: 120  # Pause trading if drift > 2 minutes

# --- WARM-UP ---
warm_up:
  required_bars:
    H4: 250
    H1: 100
    M15: 200
    M5: 100
    D1: 30
```

---

## CORE ENGINE (engine.py) — MAIN ORCHESTRATOR

```
STARTUP SEQUENCE (runs once):
│
├── Load config and credentials
├── Initialize logging
├── Initialize database (SQLite with WAL mode for crash-safe writes)
├── Connect to MT5 (retry logic)
├── Discover gold symbol (try primary → fallbacks → search all symbols)
├── Discover macro symbols (DXY, VIX, US10Y with fallbacks + web API)
├── Verify account info (balance, leverage, currency, margin_level)
├── Detect broker server timezone
├── Store symbol_info() baseline (for daily broker spec change detection)
├── Sync VPS clock — verify system time vs MT5 server time, alert if drift
├── WARM-UP:
│   ├── Fetch required historical bars for ALL timeframes
│   ├── VALIDATE all fetched data (data_validator — reject NaN, zeros, stale)
│   ├── Calculate all indicators (EMAs, ADX, ATR)
│   ├── Establish baseline regime and macro bias
│   ├── Set warm_up_complete = True
│   └── Telegram: "Bot started. Warm-up complete. Ready to trade."
├── Load persisted state from DB (crash recovery)
├── Reconcile internal state with MT5 open positions
├── Register graceful shutdown handler (SIGINT, SIGTERM, SIGBREAK)
├── Start Telegram command listener (separate thread)
└── Enter main loop

MAIN LOOP (60s active sessions, 300s otherwise):
│
├── 1. SYSTEM HEALTH CHECK
│   ├── MT5 connected? → reconnect if not (exponential backoff, max 5 attempts)
│   ├── Balance above minimum? → KILL SWITCH if not
│   ├── Margin level check:
│   │   ├── margin_level < danger (100%) → CLOSE weakest position immediately
│   │   ├── margin_level < warning (150%) → ALERT, block new trades
│   │   └── margin_level < min (200%) → block new trades
│   ├── Clock drift: compare system time vs MT5 server time
│   │   ├── drift > 2 minutes → PAUSE trading, ALERT
│   │   └── drift > 30 seconds → ALERT
│   ├── Weekend or holiday? → sleep
│   ├── System resources (memory/CPU/disk) → alert if thresholds exceeded
│   └── Heartbeat to Telegram every 30 min
│
├── 2. DAILY RESET CHECK (once per trading day at session open)
│   ├── Reset daily P&L, daily trade counter
│   ├── Run broker spec check:
│   │   ├── Fetch fresh symbol_info() for XAUUSD
│   │   ├── Compare against stored baseline (contract size, margin req, lot step, etc.)
│   │   ├── If ANY spec changed → ALERT with details, pause until reviewed
│   │   └── Check current leverage vs expected → ALERT if reduced
│   ├── Check spread regime:
│   │   ├── Calculate 7-day average spread vs 30-day average
│   │   └── If 7-day > 2x 30-day → ALERT "Spread regime may have changed"
│   └── Update stored baseline
│
├── 3. SESSION CHECK (DST-aware timezone conversion)
│   ├── Current session? (Asian/London/NY/Dead Zone/Weekend)
│   ├── Tradeable session? → manage only if not
│   ├── Holiday / early close? → handle accordingly
│   └── Friday close time? → close all, no new trades
│
├── 4. NEWS FILTER → block if high-impact event within window
│
├── 5. CIRCUIT BREAKER CHECK
│   ├── Daily loss limit → block until next day
│   ├── Consecutive losses → block with cooldown
│   ├── Weekly drawdown → reduce sizing by 50%
│   ├── Max total drawdown → KILL SWITCH
│   └── Persist circuit breaker state to DB
│
├── 6. REGIME DETECTION
│   ├── TRENDING → proceed
│   ├── RANGING → block new trades
│   ├── VOLATILE_CRISIS → block, tighten existing stops
│   └── TRANSITIONING → reduce confidence, A+ setups only
│
├── 7. MACRO FILTER
│   ├── DXY direction (symbol discovery + web fallback)
│   ├── 10Y Yield direction
│   ├── VIX level
│   └── Synthesize: BULLISH / BEARISH / NEUTRAL / CONFLICTING
│
├── 8. MANAGE EXISTING TRADES
│   ├── Partial close at 1:1 → move to BE
│   ├── Trailing stop activation
│   ├── Max duration check
│   ├── Swap-aware close (BE trades before rollover)
│   ├── Regime shift → tighten stops if profitable
│   └── Sync with MT5 (detect manual interventions or broker closes)
│
├── 9. SCAN FOR SETUPS (only if ALL filters pass)
│   │
│   ├── VALIDATE CANDLE DATA FIRST:
│   │   ├── Run data_validator on all timeframes
│   │   ├── Check for NaN, zero prices, stale data, impossible candles
│   │   ├── Check for suspicious gaps (>5% between consecutive closes)
│   │   └── If validation fails → SKIP scanning, log error, alert
│   │
│   ├── Use CLOSED candles only (get_closed_candles → [:-1])
│   ├── Mark key levels (PDH/PDL, Asian H/L, zones, psych levels)
│   │
│   ├── Setup A: Sweep & Reversal
│   │   ├── Price swept key level (wick beyond, body inside)
│   │   ├── Market structure shift on M15
│   │   ├── Aligned with H4 bias and macro bias
│   │   └── → SIGNAL (type: SWEEP_REVERSAL)
│   │
│   ├── Setup B: Trend Continuation (EMA Pullback)
│   │   ├── H4 bias confirmed (price vs 200 EMA)
│   │   ├── H1 pullback to 50 EMA with rejection candle
│   │   └── → SIGNAL (type: TREND_CONTINUATION)
│   │
│   ├── Setup C: Flag Breakout
│   │   ├── Impulse > 2x ATR, then consolidation flag
│   │   ├── Breakout in impulse direction confirmed
│   │   └── → SIGNAL (type: FLAG_BREAKOUT)
│   │
│   └── No valid setup → log, continue loop
│
├── 10. VALIDATE SIGNAL
│   ├── H4 bias aligned? Macro aligned?
│   ├── Spread below max? R:R >= min?
│   ├── Max concurrent trades? Duplicate direction?
│   ├── Minimum confidence 0.60?
│   └── If all pass → proceed to sizing
│
├── 11. CALCULATE POSITION SIZE
│   ├── Fetch FRESH symbol_info() (specs can change during volatility)
│   ├── Dynamic point value: (tick_value / tick_size) * point
│   ├── Base lots = (balance × risk_pct) / (SL_distance × point_value_per_lot)
│   ├── Apply adaptive multipliers (consecutive losses, ATR, drawdown, weekly DD)
│   ├── Apply compounding rules (if account has grown past milestone)
│   ├── Take MOST CONSERVATIVE of all calculations
│   ├── Clamp to min_lot / max_lot, round to lot_step
│   │
│   ├── *** INDEPENDENT SANITY CHECK (catches bugs in above calculation) ***
│   │   ├── Is lot > sanity_check.max_lot_hard_ceiling (1.0)? → REFUSE
│   │   ├── Would this lot risk > sanity_check.max_risk_pct_per_position (5%)? → REFUSE
│   │   ├── Is lot × required_margin > free_margin × max_margin_usage_pct? → REFUSE
│   │   └── If ANY sanity check fails → log error, ALERT, do NOT trade
│   │
│   └── *** MARGIN PRE-CHECK ***
│       ├── required_margin = mt5.order_calc_margin(type, symbol, lot, price)
│       ├── If required_margin > free_margin × 0.5 → SKIP (insufficient margin buffer)
│       └── If order_calc_margin returns None/error → SKIP
│
├── 12. EXECUTE TRADE
│   ├── Build order: comment = "{setup}|v{version}|{timestamp}"
│   ├── Send to MT5
│   ├── Handle return codes:
│   │   ├── DONE → verify fill (see partial fill handling below)
│   │   ├── Requote → re-fetch price, retry ONCE
│   │   ├── Reject → log reason, alert, skip
│   │   ├── Invalid stops → recalculate SL/TP, retry
│   │   └── Connection error → reconnect, do NOT retry blind
│   │
│   ├── *** PARTIAL FILL HANDLING ***
│   │   ├── Compare result.volume vs requested lot
│   │   ├── If partial fill: log warning, update internal state with ACTUAL volume
│   │   ├── Recalculate risk metrics for actual position size
│   │   └── If filled volume < min_lot → close immediately (too small to manage)
│   │
│   ├── Log to DB (include strategy_version, actual fill volume, fill price)
│   ├── Notify Telegram
│   └── Update internal state
│
└── 13. ANALYTICS & REPORTING
    ├── After each trade close:
    │   ├── Update running statistics (win rate, PF, expectancy)
    │   ├── Track swap costs separately
    │   ├── Check health monitor thresholds
    │   └── If degradation detected → auto-pause + alert
    ├── Daily report:
    │   ├── P&L, swap costs, margin level, regime, resources
    │   ├── Broker spec change alerts (if any)
    │   ├── Spread regime status
    │   └── Clock drift status
    └── Weekly report:
        ├── Rolling 50-trade stats, version comparison
        ├── Compounding milestone status
        └── Recommendation: continue / review / pause
```

---

## DATA VALIDATOR (data_validator.py)

```
CLASS: DataValidator

Validates ALL candle data BEFORE it reaches the strategy engine.
If data is corrupt, the bot must NOT trade — garbage in = garbage out.

METHODS:

validate_candles(df, timeframe, symbol) → (bool, str)
    """
    Run BEFORE any strategy calculation. Raises DataError or returns (False, reason)
    if data is unusable.
    """
    Checks:
    1. DataFrame is not empty
    2. No NaN or None values in OHLCV columns
    3. No zero or negative prices
    4. High >= Low for every candle (impossible candle detection)
    5. High >= Open and High >= Close for every candle
    6. Low <= Open and Low <= Close for every candle
    7. Last candle age < max_candle_age_minutes (stale data detection)
    8. No consecutive close gaps > max_price_gap_pct (data gap detection)
    9. Volume > 0 for all candles (dead feed detection)
    10. Timestamps are sequential and monotonically increasing

    On failure:
    - Log detailed error with the specific bad data points
    - Return (False, reason) — caller must NOT proceed with signals
    - If failures persist > 3 consecutive cycles → alert via Telegram

validate_tick(tick) → (bool, str)
    """Validate a single tick before using for order placement."""
    Checks:
    1. bid > 0 and ask > 0
    2. ask > bid (spread is positive)
    3. spread is within reasonable range (< 100 points)
    4. tick time is recent (< 60 seconds old)
```

---

## MT5 CONNECTOR (mt5_connector.py)

```
ALL MT5 interaction goes through this class. No other module imports MetaTrader5.

KEY METHODS:

connect() / disconnect() / is_connected() / reconnect()
    - Exponential backoff retry (max 5 attempts)
    - Verify last tick is recent after connect

discover_symbol(preferred, fallbacks) → str | None
    - Try each candidate, verify via symbol_info()
    - Last resort: search all symbols for "XAU" + "USD"
    - mt5.symbol_select() to make visible in Market Watch

get_broker_timezone() → str
    - Detect from server time vs UTC comparison

get_clock_drift() → timedelta
    - Compare system clock against MT5 server time
    - Used by health monitor to detect VPS clock drift

get_account_info() → dict
    - balance, equity, margin, free_margin, profit
    - margin_level, leverage, currency

get_symbol_info(symbol) → dict
    - MUST be called fresh before EVERY position size calc
    - point, digits, tick_value, tick_size
    - volume_min, volume_max, volume_step
    - swap_long, swap_short
    - spread (current)

get_point_value(symbol_info) → float
    - (tick_value / tick_size) * point — NEVER hardcode

check_margin_for_order(order_type, symbol, lot, price) → dict
    - Calls mt5.order_calc_margin()
    - Returns {required_margin, free_margin, margin_usage_pct, sufficient: bool}

get_closed_candles(symbol, timeframe, count) → DataFrame
    - Fetches count+1 candles, drops last (incomplete)
    - ALL signal logic MUST use this, never raw get_rates()

place_order(order_type, lot, sl, tp, comment) → dict
    - Full return code handling
    - PARTIAL FILL detection: compare result.volume vs requested
    - Returns {success, ticket, fill_price, fill_volume, partial_fill: bool}

modify_position() / close_position() / close_all_positions()
get_positions(magic_number) / get_history() / get_economic_calendar()

RULES:
- try/except on ALL methods
- threading.Lock() (MT5 API not thread-safe)
- Rate limit: max 10 req/sec
- Verify connection before every operation
```

---

## STRATEGY ENGINE (strategy.py)

```
PURE LOGIC — data in, signals out. No execution, no risk checks.
ALL data must pass DataValidator BEFORE reaching this module.
ALL candle data must be CLOSED candles (get_closed_candles).

METHODS:
- calculate_key_levels() → PDH/PDL, Asian H/L, supply/demand zones, psych levels
- get_h4_bias() → BULLISH/BEARISH/NEUTRAL (200 EMA, 3-candle confirmation)
- detect_sweep() → liquidity sweep at key level
- detect_structure_shift() → M15 structure change after sweep
- detect_ema_pullback() → H1 pullback to 50 EMA in H4 direction
- detect_flag_pattern() → consolidation flag after impulse, breakout
- scan_for_signals() → runs all detectors, returns scored signals

SIGNAL: {type, direction, entry, sl, tp, rr_ratio, confidence, h4_aligned, macro_aligned, reasoning}

CONFIDENCE: base 0.5 + h4(+0.15) + macro(+0.15) + fresh zone(+0.10) + confluence(+0.10)
            - counter trend(-0.20) - conflicting macro(-0.15). Minimum to trade: 0.60
```

---

## RISK MANAGER (risk_manager.py)

```
STATE (persisted to DB): consecutive_losses, daily/weekly P&L, peak_balance, drawdown, circuit_breakers

can_trade() → (bool, str)
    Checks in order: circuit breakers → balance → margin_level → drawdown →
    daily loss → consecutive losses → concurrent trades
    Returns (True, "OK") only if ALL pass.

calculate_position_size(balance, sl_distance, symbol_info) → float
    1. Fresh symbol_info (tick_value, tick_size, point)
    2. Dynamic point value — NEVER hardcode
    3. Base lots = (balance × risk%) / (SL × point_value_per_lot)
    4. Adaptive multipliers (losses, ATR, drawdown, weekly DD)
    5. Compounding adjustment (if milestone reached)
    6. Most conservative of all
    7. Clamp min/max, round to lot_step

sanity_check_lot(lot, balance, symbol_info) → (float | None, str)
    """
    INDEPENDENT safety layer — runs AFTER calculation.
    Catches bugs in the position sizing code itself.
    """
    - lot > max_lot_hard_ceiling (1.0 for small accounts) → REFUSE
    - lot would risk > 5% of balance → REFUSE
    - lot × margin > free_margin × 50% → REFUSE
    - If any fail: log "SANITY CHECK FAILED", return (None, reason)

check_margin_before_order(order_type, symbol, lot, price) → (bool, str)
    - mt5.order_calc_margin() to get required margin
    - If required > free_margin × max_margin_usage_pct → REFUSE

handle_partial_fill(requested_lot, actual_lot, ticket)
    - If actual < requested: log warning, update internal state
    - If actual < min_lot: close position (too small)
    - Recalculate risk metrics for actual size

validate_signal() → final checks: R:R, SL range, margin, concurrent, duplicate, spread
update_after_trade() → counters, P&L, drawdown, circuit breakers, persist state
check_kill_switch() → balance < min OR drawdown > max → close all, alert, stop
reset_daily() / reset_weekly()
```

---

## REGIME DETECTOR, MACRO FILTER, SESSION MANAGER

```
REGIME DETECTOR:
- Regimes: TRENDING_BULLISH/BEARISH, RANGING, VOLATILE_CRISIS, TRANSITIONING
- ADX + ATR + EMA alignment → classification + recommendation
- should_trade() → True for TRENDING, reduced for TRANSITIONING, False otherwise

MACRO FILTER:
- DXY + Yields + VIX → BULLISH/BEARISH/NEUTRAL/CONFLICTING
- Symbol discovery with fallbacks + yfinance web API fallback (cached 15 min)

SESSION MANAGER:
- ALL times DST-aware via pytz/zoneinfo. NEVER hardcode UTC offsets.
- get_current_session(), is_tradeable(), get_asian_range()
- is_friday_close_time(), is_holiday(), is_early_close_day()
- broker_time_to_utc(), utc_to_session_time()
```

---

## TELEGRAM BOT (telegram_bot.py)

```
NOTIFICATIONS:
- Trade open/close (with strategy version, fill volume, swap costs, margin level)
- Circuit breaker activated
- Kill switch (URGENT)
- Daily/weekly reports (includes system resources, broker status, clock drift)
- Strategy health alert (auto-pause)
- Broker spec change alert
- Spread regime change alert
- Sanity check failure alert
- Partial fill warning
- Margin level warning
- Clock drift warning

COMMANDS:
/status     → Account, positions, regime, macro, margin level
/pause      → Pause trading (manage existing only)
/resume     → Resume
/closeall   → Close all (requires confirmation)
/report     → Performance report
/risk N     → Change risk per trade
/maxlot N   → Change max lot
/kill       → Kill switch (requires confirmation)
/health     → Strategy health + system resources + broker status
/trades     → Last 10 trades
/uptime     → Uptime, last trade, system status
/version    → Strategy version and notes
/margin     → Current margin level and usage

Runs in separate thread. Queued messages. Rate limited 10/min.
Destructive commands (/closeall, /kill) require confirmation reply.
```

---

## ANALYTICS & HEALTH MONITOR

```
PERFORMANCE METRICS (rolling 50-trade window):
- win_rate, profit_factor, expectancy, sharpe_ratio
- avg_rr_achieved, max_drawdown, max_drawdown_duration
- best/worst by session, setup, day of week
- total_swap_costs (tracked separately)
- results_by_strategy_version
- partial_fill_count (track broker execution quality)

STRATEGY HEALTH (after every trade close):
1. Win rate < threshold → ALERT
2. Profit factor < 1.0 → ALERT
3. Consecutive losses > 2x historical avg → ALERT
4. Avg R:R dropping below 1.0 → ALERT
5. Overtrading (frequency spike) → ALERT
6. Drawdown duration > 2x avg → ALERT
→ 2+ checks fail = AUTO-PAUSE + ALERT

SYSTEM HEALTH (every heartbeat):
- Memory, CPU, disk → alert if thresholds exceeded
- Clock drift vs MT5 server → pause if > 2 min

BROKER HEALTH (daily reset):
- Symbol spec comparison vs baseline → alert on change
- Spread regime (7d avg vs 30d avg) → alert if 2x
- Leverage verification → alert if changed
- Execution quality (partial fill frequency) → alert if increasing
```

---

## BACKTESTING

```
- Same strategy logic as live (no separate code)
- Simulates: spread, slippage (random 0-3 points), session timing, swap costs, partials, trailing
- Walk-forward: in-sample → out-of-sample → rolling windows
- Valid if: OOS PF > 1.0 in 60%+ windows, OOS WR > 40% in 60%+, OOS DD < 20% in ALL
```

---

## DATABASE SCHEMA

```sql
-- Use WAL mode for crash-safe writes:
-- PRAGMA journal_mode=WAL;

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER, type TEXT, setup_type TEXT, strategy_version TEXT,
    entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL,
    requested_lot REAL, filled_lot REAL, partial_fill BOOLEAN DEFAULT 0,
    pnl REAL, pnl_pct REAL, swap_cost REAL, commission REAL,
    rr_achieved REAL, duration_minutes INTEGER,
    session TEXT, regime TEXT, macro_bias TEXT, confidence REAL,
    margin_level_at_entry REAL,
    entry_time TIMESTAMP, exit_time TIMESTAMP,
    exit_reason TEXT, is_backtest BOOLEAN DEFAULT 0, notes TEXT
);

CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT, direction TEXT, entry_price REAL, stop_loss REAL, take_profit REAL,
    confidence REAL, was_traded BOOLEAN, skip_reason TEXT,
    timestamp TIMESTAMP, regime TEXT, macro_bias TEXT, session TEXT, strategy_version TEXT
);

CREATE TABLE daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE, balance REAL, equity REAL, margin_level REAL,
    trades_count INTEGER, wins INTEGER, losses INTEGER,
    daily_pnl REAL, daily_pnl_pct REAL, swap_costs_total REAL,
    drawdown_from_peak REAL, regime TEXT, macro_bias TEXT,
    circuit_breakers_triggered TEXT, strategy_version TEXT,
    spread_avg REAL, leverage REAL,
    memory_usage_mb REAL, disk_free_gb REAL, clock_drift_seconds REAL,
    broker_spec_changes TEXT, notes TEXT
);

CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP);

CREATE TABLE circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT, triggered_at TIMESTAMP, reason TEXT,
    balance_at_trigger REAL, margin_level_at_trigger REAL,
    resolved_at TIMESTAMP
);

CREATE TABLE broker_spec_baseline (
    symbol TEXT PRIMARY KEY,
    contract_size REAL, margin_initial REAL, margin_maintenance REAL,
    volume_min REAL, volume_max REAL, volume_step REAL,
    swap_long REAL, swap_short REAL, leverage INTEGER,
    last_updated TIMESTAMP
);

CREATE TABLE withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount REAL, balance_before REAL, balance_after REAL,
    timestamp TIMESTAMP, notes TEXT
);
```

---

## CRASH RECOVERY & RELIABILITY

```
1. MT5 DISCONNECT → reconnect exponential backoff, sync positions
2. MT5 AUTO-UPDATE → disable in settings; detect restart, reconnect, verify positions
3. PYTHON CRASH → load state from DB (WAL mode prevents corruption), reconcile with MT5
4. VPS RESTART → Task Scheduler auto-start + watchdog every 30 min
5. RDP DISCONNECT → disable screen lock (NoLockScreen registry), disable screensaver
6. GRACEFUL SHUTDOWN → save state, DON'T close positions (server-side SL/TP protect them)
7. UNHANDLED EXCEPTIONS → global handler, log, alert, continue; 3x in 10min → pause
8. BROKER ISSUES → handle requotes, market closed, invalid price; never retry blind
9. DATABASE CORRUPTION → WAL mode prevents most; on startup verify DB integrity
10. CLOCK DRIFT → NTP sync in install.bat; monitor drift vs MT5 server every heartbeat
```

---

## VPS SETUP SCRIPTS

```
scripts/install.bat:

@echo off
echo === GoldMind VPS Setup ===
:: Disable screen lock on RDP disconnect
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f
:: Disable auto-restart from Windows Update
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f
:: Disable screensaver
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
:: Force NTP time sync every hour
w32tm /config /manualpeerlist:"time.windows.com" /syncfromflags:manual /reliable:YES /update
net stop w32time & net start w32time & w32tm /resync
:: Auto-start bot on boot
schtasks /create /tn "GoldMind Bot" /tr "cmd /c cd /d C:\goldmind && python main.py" /sc onstart /ru SYSTEM /rl highest /f
:: Watchdog every 30 min
schtasks /create /tn "GoldMind Watchdog" /tr "cmd /c cd /d C:\goldmind && python scripts\watchdog.py" /sc minute /mo 30 /ru SYSTEM /f
:: NTP resync every hour
schtasks /create /tn "GoldMind NTP Sync" /tr "w32tm /resync" /sc hourly /ru SYSTEM /f
echo Setup complete.
pause


scripts/watchdog.py:
- psutil scan for python.exe + goldmind/main.py
- If not running → subprocess.Popen restart with CREATE_NEW_CONSOLE
```

---

## IMPLEMENTATION RULES

```
NEVER:
 1. Hardcode credentials — all in credentials.yaml (gitignored)
 2. Place a trade without verifying spread first
 3. Modify SL to be further from entry (only tighten or move to BE)
 4. Increase lot size during a drawdown
 5. Trade during Asian session or dead zone
 6. Hold trades over the weekend (close Friday afternoon)
 7. Retry a rejected order without re-fetching current price
 8. Trust MT5 connection — verify before every operation
 9. Hardcode pip/point values — calculate from symbol_info()
10. Use naive datetimes — always timezone-aware via pytz/zoneinfo
11. Hardcode UTC offsets — DST changes them twice a year
12. Assume broker symbol name — use discovery with fallbacks
13. Use current incomplete candle for signals — always [:-1]
14. Ignore swap fees in overnight trade decisions
15. Skip the independent lot sanity check — it catches YOUR bugs
16. Ignore partial fills — always verify actual vs requested volume
17. Trade when margin_level < 200% — you're one spike from margin call
18. Trust candle data without validation — garbage in = garbage out

ALWAYS:
19. Set SL and TP server-side (not just in code)
20. Log before AND after every trade operation
21. Send Telegram notification for trade events and errors
22. Use magic number to filter bot's own trades
23. Persist state to database (crash recovery)
24. Validate position size against account balance AND margin
25. Respect circuit breaker hierarchy (daily < weekly < total)
26. Fetch fresh symbol_info() before position sizing
27. Warm up indicators before generating signals on startup
28. Monitor system resources (memory, CPU, disk)
29. Version strategy parameters and track results per version
30. Handle graceful shutdown — save state, don't panic-close
31. Run data validation before signal calculations
32. Check margin with order_calc_margin before placing orders
33. Monitor clock drift and pause if system time is wrong
34. Verify broker specs daily and alert on changes
35. CODE: Type hints, docstrings, clean separation of concerns
36. TEST: Unit tests for risk, sizing, regime, signals, validation, sanity checks
```

---

## DEVELOPMENT ORDER

```
PHASE 1: Foundation
  1. config/ — YAML loading + validation
  2. utils/ — Logger, constants, timezone helpers, pip calc
  3. database/ — Schema (WAL mode), CRUD, state persistence
  4. core/mt5_connector.py — Connection, symbol discovery, data fetching, clock drift
  → TEST: Connect, discover symbol, fetch data, account info, verify clock

PHASE 2: Safety Layer
  5. core/data_validator.py — Candle + tick validation
  6. core/session_manager.py — DST-safe session timing
  → TEST: Validate good/bad data, correct session detection across DST

PHASE 3: Intelligence
  7. core/regime_detector.py — ADX/ATR classification
  8. core/macro_filter.py — DXY/Yields/VIX with discovery + web fallback
  9. core/news_filter.py — Calendar + holidays
  10. core/strategy.py — All 3 setups (validated + closed candles only)
  → TEST: Signals on historical data with proper filtering

PHASE 4: Protection
  11. core/risk_manager.py — Sizing, sanity check, margin check, circuit breakers, kill switch
  12. core/trade_manager.py — Partials, trailing, BE, swap-aware, partial fill handling
  → TEST: Unit tests for ALL risk paths including sanity check edge cases

PHASE 5: Execution
  13. Order execution in mt5_connector.py (partial fill detection)
  14. core/engine.py — Full orchestrator with warm-up, daily reset, broker monitoring
  15. main.py — Entry point with graceful shutdown
  → TEST: Demo account, full lifecycle including partial fills and margin checks

PHASE 6: Communication
  16. notifications/telegram_bot.py — All notifications + 13 commands
  17. notifications/templates.py
  → TEST: All alert types, commands, confirmations

PHASE 7: Analytics
  18. analytics/performance.py — Metrics, swap tracking, version comparison
  19. analytics/health_monitor.py — Strategy + system + broker monitoring
  20. analytics/dashboard.py
  → TEST: Accurate reports from demo data

PHASE 8: Validation
  21. backtesting/backtester.py — With spread + slippage + swap simulation
  22. backtesting/walk_forward.py
  23. backtesting/report_generator.py
  → TEST: Walk-forward on 2+ years

PHASE 9: Production
  24. scripts/install.bat — VPS setup (registry, Task Scheduler, NTP)
  25. scripts/watchdog.py — Auto-restart
  26. 2+ weeks unattended demo
  27. Go live with 0.01 lots
```

---

## FINAL NOTES FOR CLAUDE CODE

```
- Build each module COMPLETELY before moving to the next
- Write unit tests alongside each module
- Use Python type hints throughout
- Every function must have a docstring
- Log extensively — logs are your only window in production
- config.yaml is the SINGLE SOURCE OF TRUTH — no magic numbers in code
- Handle every edge case — this bot runs while the human sleeps
- When in doubt, do NOTHING rather than something risky
- The bot's #1 job is CAPITAL PRESERVATION, #2 is making profit
- The sanity check layer exists to catch YOUR bugs — never skip it
- SQLite WAL mode is mandatory — the bot will crash eventually, DB must survive
- Data validation is not optional — one NaN candle can trigger a false signal

After building each phase, show me:
1. The complete code for that phase
2. How to test it
3. Any configuration needed
4. What to verify before moving to the next phase

Start with Phase 1. Let's build this.
```

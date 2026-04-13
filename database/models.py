"""SQLite schema for GoldMind. Pure DDL; no logic."""
from __future__ import annotations

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER,
        type TEXT,
        setup_type TEXT,
        strategy_version TEXT,
        entry_price REAL,
        exit_price REAL,
        stop_loss REAL,
        take_profit REAL,
        requested_lot REAL,
        filled_lot REAL,
        partial_fill INTEGER DEFAULT 0,
        pnl REAL,
        pnl_pct REAL,
        swap_cost REAL,
        commission REAL,
        rr_achieved REAL,
        duration_minutes INTEGER,
        session TEXT,
        regime TEXT,
        macro_bias TEXT,
        confidence REAL,
        margin_level_at_entry REAL,
        entry_time TIMESTAMP,
        exit_time TIMESTAMP,
        exit_reason TEXT,
        is_backtest INTEGER DEFAULT 0,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        direction TEXT,
        entry_price REAL,
        stop_loss REAL,
        take_profit REAL,
        confidence REAL,
        was_traded INTEGER,
        skip_reason TEXT,
        timestamp TIMESTAMP,
        regime TEXT,
        macro_bias TEXT,
        session TEXT,
        strategy_version TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE UNIQUE,
        balance REAL,
        equity REAL,
        margin_level REAL,
        trades_count INTEGER,
        wins INTEGER,
        losses INTEGER,
        daily_pnl REAL,
        daily_pnl_pct REAL,
        swap_costs_total REAL,
        drawdown_from_peak REAL,
        regime TEXT,
        macro_bias TEXT,
        circuit_breakers_triggered TEXT,
        strategy_version TEXT,
        spread_avg REAL,
        leverage REAL,
        memory_usage_mb REAL,
        disk_free_gb REAL,
        clock_drift_seconds REAL,
        broker_spec_changes TEXT,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS circuit_breaker_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        triggered_at TIMESTAMP,
        reason TEXT,
        balance_at_trigger REAL,
        margin_level_at_trigger REAL,
        resolved_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broker_spec_baseline (
        symbol TEXT PRIMARY KEY,
        contract_size REAL,
        margin_initial REAL,
        margin_maintenance REAL,
        volume_min REAL,
        volume_max REAL,
        volume_step REAL,
        swap_long REAL,
        swap_short REAL,
        leverage INTEGER,
        last_updated TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount REAL,
        balance_before REAL,
        balance_after REAL,
        timestamp TIMESTAMP,
        notes TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy_version ON trades(strategy_version)",
    "CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_cb_triggered_at ON circuit_breaker_events(triggered_at)",
)

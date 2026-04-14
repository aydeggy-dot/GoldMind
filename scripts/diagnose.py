"""One-shot diagnostic: run ONE tick of the engine's scanning pipeline
and print every decision point so we can see where it bails out.

Reuses the real Engine subsystems (connector, strategy, regime, sessions)
so the output reflects production behavior exactly.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_all
from core.data_validator import DataValidator
from core.macro_filter import MacroFilter
from core.mt5_connector import MT5Connector
from core.news_filter import NewsFilter
from core.regime_detector import RegimeDetector
from core.session_manager import SessionManager
from core.strategy import Strategy
from utils.constants import MacroBias, Regime


def _pp(label: str, value) -> None:
    print(f"  {label:<24s} = {value}")


def main() -> int:
    bundle = load_all()
    cfg, creds = bundle["config"], bundle["credentials"]

    c = MT5Connector(
        account=int(creds["mt5"]["account"]),
        password=str(creds["mt5"]["password"]),
        server=str(creds["mt5"]["server"]),
        terminal_path=creds["mt5"].get("terminal_path") or None,
        max_reconnect_attempts=1, reconnect_delay_seconds=1,
    )
    if not c.connect():
        print("ERROR: could not connect to MT5"); return 1

    symbol = c.discover_symbol(cfg["mt5"]["symbol"],
                               cfg["mt5"].get("symbol_fallbacks", []))
    print(f"=== Diagnostic for {symbol} @ {datetime.now(timezone.utc).isoformat()} ===\n")

    drift = c.get_clock_drift()
    _pp("clock_drift_seconds", f"{drift.total_seconds():.1f}" if drift else "n/a")
    _pp("broker_utc_offset",   c._broker_utc_offset())

    # --- session
    sessions = SessionManager(cfg["sessions"], cfg["holidays"])
    sess = sessions.get_current_session()
    _pp("current_session",     sess.value)
    _pp("is_tradeable",        sessions.is_tradeable())
    _pp("trade_sessions_cfg",  cfg["sessions"]["trade_sessions"])

    # --- fetch snapshot
    print("\n--- snapshot fetch ---")
    warm = cfg["warm_up"]["required_bars"]
    snap = {tf: c.get_closed_candles(tf, int(warm[tf])) for tf in ("H4","H1","M15","D1")}
    for tf, df in snap.items():
        last_time = df["time"].iloc[-1] if len(df) else "n/a"
        _pp(f"{tf} bars fetched", f"{len(df)} (last={last_time})")

    # --- validate
    print("\n--- snapshot validation ---")
    v = DataValidator(
        max_candle_age_minutes=cfg["data_validation"]["max_candle_age_minutes"],
        max_price_gap_pct=cfg["data_validation"]["max_price_gap_pct"],
        max_price_gap_pct_by_tf=cfg["data_validation"].get("max_price_gap_pct_by_tf") or {},
    )
    for tf, df in snap.items():
        r = v.validate_candles(df, tf, symbol or "")
        _pp(f"{tf} validation", f"{'PASS' if bool(r) else 'FAIL: ' + r.reason}")

    # --- regime
    print("\n--- regime ---")
    reg = RegimeDetector.from_config(cfg["regime"], cfg["strategy"])
    rr = reg.detect(snap["H1"])
    _pp("regime",             rr.regime.value)
    _pp("adx",                f"{rr.adx:.2f}")
    _pp("atr",                f"{rr.atr:.2f}")
    _pp("atr_ratio",          f"{rr.atr_ratio:.2f}")
    _pp("ema_fast / ema_slow", f"{rr.ema_fast:.2f} / {rr.ema_slow:.2f}")
    _pp("regime_reason",      rr.reason)

    # --- macro
    print("\n--- macro ---")
    try:
        macro = MacroFilter(
            cfg["macro"],
            broker_fetcher=lambda sym, tf, n: c.get_closed_candles(tf, n, symbol=sym),
            symbol_resolver=lambda p, fb: c.discover_symbol(p, fb),
        )
        mr = macro.evaluate()
        _pp("macro_bias", mr.bias.value)
        _pp("macro_reason", mr.reason)
    except Exception as exc:  # noqa: BLE001
        _pp("macro_error", str(exc))
        mr = type("M", (), {"bias": MacroBias.NEUTRAL})()

    # --- news
    print("\n--- news ---")
    news = NewsFilter(cfg["news"], event_fetcher=None)
    nc = news.is_blocked()
    _pp("news_blocked", f"{nc.blocked} — {nc.reason}")

    # --- strategy
    print("\n--- strategy signals ---")
    info = c.get_symbol_info(symbol) or {}
    strategy = Strategy(cfg["strategy"], cfg["risk"])
    asian_range = sessions.get_asian_range(snap["H1"])
    _pp("asian_range", asian_range)
    try:
        signals = strategy.scan_for_signals(
            h4=snap["H4"], h1=snap["H1"], m15=snap["M15"], d1=snap["D1"],
            symbol_info=info,
            regime=rr.regime, macro_bias=mr.bias, asian_range=asian_range,
        )
    except Exception as exc:  # noqa: BLE001
        _pp("strategy_error", str(exc)); signals = []

    _pp("signal_count", len(signals))
    for i, s in enumerate(signals):
        print(f"  signal[{i}]: {s.type.value} {s.direction.value} "
              f"entry={s.entry:.2f} sl={s.sl:.2f} tp={s.tp:.2f} "
              f"rr={s.rr_ratio:.2f} conf={s.confidence:.2f}")
        print(f"            reasoning={s.reasoning}")

    print("\n--- key levels (for sweep setup) ---")
    levels = strategy.calculate_key_levels(snap["D1"], snap["H1"], asian_range,
                                             float(info.get("point", 0.01)))
    for lvl in levels[:10]:
        _pp(f"  {lvl.name}", f"{lvl.price:.2f} fresh={lvl.fresh}")

    c.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())

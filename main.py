"""GoldMind entry point.

Loads config + credentials, wires connector + DB + engine, and starts the
main loop. Registers signal handlers so SIGINT / SIGTERM / SIGBREAK trigger
a graceful shutdown that saves state. Open positions are NOT panic-closed —
they remain protected by their server-side SL/TP.
"""
from __future__ import annotations

import logging
import signal
import sys
from typing import Any

from config import load_all
from core.engine import Engine
from core.mt5_connector import MT5Connector
from database import DBManager
from analytics import HealthMonitor
from notifications import TelegramNotifier
from utils.logger import setup_logger


def _wire_signal_handlers(engine: Engine) -> None:
    def _handler(signum, _frame):  # noqa: ANN001
        logging.getLogger("goldmind").warning("Signal %s received — stopping engine", signum)
        engine.stop()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover
                # SIGBREAK only on Windows; some platforms restrict signal binding.
                pass


def main() -> int:
    bundle: dict[str, Any] = load_all()
    cfg = bundle["config"]
    creds = bundle["credentials"]

    log_cfg = cfg["logging"]
    logger = setup_logger(
        name="goldmind",
        log_file=log_cfg["log_file"],
        level=log_cfg["level"],
        max_file_size_mb=int(log_cfg["max_file_size_mb"]),
        backup_count=int(log_cfg["backup_count"]),
    )
    logger.info("=== GoldMind starting ===")

    db = DBManager(cfg["database"]["path"], wal_mode=bool(cfg["database"]["wal_mode"]))
    if not db.integrity_check():
        logger.critical("DB integrity check failed; aborting")
        return 2

    health = cfg["health"]
    connector = MT5Connector(
        account=int(creds["mt5"]["account"]),
        password=str(creds["mt5"]["password"]),
        server=str(creds["mt5"]["server"]),
        terminal_path=creds["mt5"].get("terminal_path") or None,
        max_reconnect_attempts=int(health["mt5_reconnect_attempts"]),
        reconnect_delay_seconds=int(health["mt5_reconnect_delay_seconds"]),
    )

    if not connector.connect():
        logger.critical("MT5 connection failed; aborting")
        return 3

    sym = connector.discover_symbol(cfg["mt5"]["symbol"], cfg["mt5"].get("symbol_fallbacks", []))
    if not sym:
        logger.critical("Could not discover XAUUSD symbol; aborting")
        connector.disconnect()
        return 4

    notifier: TelegramNotifier | None = None
    if bool(cfg["telegram"].get("enabled", False)):
        notifier = TelegramNotifier(
            bot_token=str(creds["telegram"]["bot_token"]),
            chat_id=str(creds["telegram"]["chat_id"]),
            config=cfg,
            db=db,
            rate_limit_per_minute=int(cfg["telegram"].get("max_messages_per_minute", 10)),
        )

    health_monitor = HealthMonitor(cfg, db, notifier=notifier)
    engine = Engine(config=cfg, connector=connector, db=db, notifier=notifier,
                    health_monitor=health_monitor)
    if notifier is not None:
        notifier.engine = engine
        notifier.start()
    _wire_signal_handlers(engine)

    try:
        engine.start()
    finally:
        if notifier is not None:
            notifier.stop()
        connector.disconnect()
        db.close()
        logger.info("=== GoldMind stopped ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

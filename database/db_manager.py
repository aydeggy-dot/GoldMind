"""SQLite manager with WAL mode (crash-safe writes) and a small CRUD surface.

Single-process model: a single shared connection guarded by an RLock.
WAL allows readers to coexist with writers and survives unclean shutdowns.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import SCHEMA

logger = logging.getLogger("goldmind")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DBManager:
    """Thread-safe SQLite wrapper for GoldMind."""

    def __init__(self, path: str | Path, wal_mode: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,  # autocommit; we manage tx explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if wal_mode:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            for stmt in SCHEMA:
                self._conn.execute(stmt)

    def integrity_check(self) -> bool:
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error as e:
                logger.warning("DB close error: %s", e)

    def __enter__(self) -> "DBManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Generic exec helpers
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------
    # System state KV (used for crash recovery)
    # ------------------------------------------------------------------
    def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO system_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                updated_at=excluded.updated_at
                """,
                (key, payload, _utcnow_iso()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value FROM system_state WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def delete_state(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM system_state WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # Trades / signals / events (minimal Phase 1 surface — extended later)
    # ------------------------------------------------------------------
    def insert_trade(self, trade: dict[str, Any]) -> int:
        cols = ", ".join(trade.keys())
        placeholders = ", ".join(["?"] * len(trade))
        sql = f"INSERT INTO trades ({cols}) VALUES ({placeholders})"
        with self._lock, self._conn:
            cur = self._conn.execute(sql, tuple(trade.values()))
            return int(cur.lastrowid)

    def insert_signal(self, signal: dict[str, Any]) -> int:
        cols = ", ".join(signal.keys())
        placeholders = ", ".join(["?"] * len(signal))
        sql = f"INSERT INTO signals ({cols}) VALUES ({placeholders})"
        with self._lock, self._conn:
            cur = self._conn.execute(sql, tuple(signal.values()))
            return int(cur.lastrowid)

    def insert_circuit_breaker_event(self, event: dict[str, Any]) -> int:
        cols = ", ".join(event.keys())
        placeholders = ", ".join(["?"] * len(event))
        sql = f"INSERT INTO circuit_breaker_events ({cols}) VALUES ({placeholders})"
        with self._lock, self._conn:
            cur = self._conn.execute(sql, tuple(event.values()))
            return int(cur.lastrowid)

    def upsert_broker_baseline(self, symbol: str, specs: dict[str, Any]) -> None:
        row = {"symbol": symbol, **specs, "last_updated": _utcnow_iso()}
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        updates = ", ".join(f"{k}=excluded.{k}" for k in row.keys() if k != "symbol")
        sql = (
            f"INSERT INTO broker_spec_baseline ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {updates}"
        )
        with self._lock, self._conn:
            self._conn.execute(sql, tuple(row.values()))

    def get_broker_baseline(self, symbol: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT * FROM broker_spec_baseline WHERE symbol = ?", (symbol,)
        )
        return dict(row) if row else None

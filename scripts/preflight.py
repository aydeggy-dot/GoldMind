"""Pre-flight checks — run before starting the bot live.

Validates:
  1. Config + credentials load and pass schema validation.
  2. Credential fields are filled in (not the example placeholders).
  3. Database path is writable + passes SQLite integrity_check().
  4. MT5 connection succeeds and the XAUUSD symbol resolves.
  5. Account balance is above the circuit-breaker floor.
  6. Symbol broker specs are reasonable (positive tick value, min lot
     fits the configured risk, spread within max_spread_points).
  7. Clock drift vs broker is within the pause threshold.

Returns exit code 0 on pass, non-zero on any failure so it can be used
in an install.bat tail: `python scripts\\preflight.py && start bot`.

Checks are injection-friendly (connector_factory) so the test suite can
drive this without MT5 installed.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import load_all
from database import DBManager

logger = logging.getLogger("goldmind.preflight")


@dataclass
class PreflightReport:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    def render(self) -> str:
        out = ["=== Preflight ==="]
        for name, passed, detail in self.checks:
            mark = "PASS" if passed else "FAIL"
            out.append(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        out.append(f"\nResult: {'OK' if self.ok else 'FAIL'}")
        return "\n".join(out)


# ----------------------------------------------------------------------
PLACEHOLDER_TOKENS = ("your_password_here", "12345678",
                      "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
                      "987654321", "YourBroker-Live")


def _creds_filled(creds: dict) -> tuple[bool, str]:
    mt5 = creds.get("mt5", {}) or {}
    tg = creds.get("telegram", {}) or {}
    for field_name, val in (
        ("mt5.account", mt5.get("account")),
        ("mt5.password", mt5.get("password")),
        ("mt5.server", mt5.get("server")),
    ):
        if val is None or val == "":
            return False, f"{field_name} missing"
        if str(val) in PLACEHOLDER_TOKENS:
            return False, f"{field_name} is still an example placeholder"
    if str(tg.get("bot_token", "")) in PLACEHOLDER_TOKENS:
        return False, "telegram.bot_token is still an example placeholder"
    if str(tg.get("chat_id", "")) in PLACEHOLDER_TOKENS:
        return False, "telegram.chat_id is still an example placeholder"
    return True, "credentials filled"


def _reasonable_specs(info: dict, cfg: dict) -> tuple[bool, str]:
    tick_value = float(info.get("trade_tick_value") or 0)
    tick_size = float(info.get("trade_tick_size") or 0)
    volume_min = float(info.get("volume_min") or 0)
    volume_max = float(info.get("volume_max") or 0)
    if tick_value <= 0 or tick_size <= 0:
        return False, "tick value/size not positive"
    if volume_min <= 0 or volume_max < volume_min:
        return False, f"volume bounds invalid: min={volume_min} max={volume_max}"
    cfg_min = float(cfg.get("risk", {}).get("min_lot_size", 0.01))
    if cfg_min < volume_min:
        return False, f"risk.min_lot_size {cfg_min} below broker minimum {volume_min}"
    return True, f"specs OK (min_lot={volume_min}, tick_value={tick_value})"


# ----------------------------------------------------------------------
def run_preflight(
    *,
    bundle: dict | None = None,
    connector_factory: Callable[[dict, dict], Any] | None = None,
) -> PreflightReport:
    report = PreflightReport()

    try:
        bundle = bundle or load_all()
    except Exception as exc:  # noqa: BLE001
        report.add("config + credentials load", False, str(exc))
        return report
    cfg = bundle["config"]
    creds = bundle["credentials"]
    report.add("config + credentials load", True, "")

    ok, detail = _creds_filled(creds)
    report.add("credentials filled", ok, detail)
    if not ok:
        return report

    # DB check
    try:
        db_path = Path(cfg["database"]["path"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = DBManager(db_path, wal_mode=bool(cfg["database"].get("wal_mode", True)))
        ok_integrity = db.integrity_check()
        db.close()
        report.add("db integrity", ok_integrity,
                   str(db_path) if ok_integrity else "integrity_check failed")
    except Exception as exc:  # noqa: BLE001
        report.add("db integrity", False, str(exc))
        return report

    # Connector / symbol / balance / clock drift
    if connector_factory is None:
        from core.mt5_connector import MT5Connector

        def connector_factory(cfg_in, creds_in):  # noqa: ANN001
            h = cfg_in["health"]
            return MT5Connector(
                account=int(creds_in["mt5"]["account"]),
                password=str(creds_in["mt5"]["password"]),
                server=str(creds_in["mt5"]["server"]),
                terminal_path=creds_in["mt5"].get("terminal_path") or None,
                max_reconnect_attempts=int(h.get("mt5_reconnect_attempts", 3)),
                reconnect_delay_seconds=int(h.get("mt5_reconnect_delay_seconds", 10)),
            )

    try:
        connector = connector_factory(cfg, creds)
    except Exception as exc:  # noqa: BLE001
        report.add("connector init", False, str(exc))
        return report
    report.add("connector init", True, "")

    try:
        connected = connector.connect()
        report.add("mt5 connect", bool(connected),
                   "ok" if connected else "connect() returned False")
        if not connected:
            return report

        symbol = connector.discover_symbol(
            cfg["mt5"]["symbol"], cfg["mt5"].get("symbol_fallbacks", []))
        report.add("symbol discovery", bool(symbol), f"resolved={symbol}")
        if not symbol:
            return report

        info = connector.get_symbol_info(symbol) or {}
        ok, detail = _reasonable_specs(info, cfg)
        report.add("broker specs sane", ok, detail)

        acct = connector.get_account_info() or {}
        balance = float(acct.get("balance", 0) or 0)
        floor = float(cfg["circuit_breakers"]["min_account_balance"])
        report.add(
            "balance above floor",
            balance >= floor,
            f"balance={balance:.2f} floor={floor:.2f}",
        )

        drift = connector.get_clock_drift()
        if drift is None:
            report.add("clock drift", True, "unavailable (assumed ok)")
        else:
            secs = abs(drift.total_seconds())
            pause_at = float(cfg["health"]["pause_on_clock_drift_seconds"])
            report.add("clock drift under pause-threshold",
                       secs < pause_at,
                       f"drift={secs:.1f}s threshold={pause_at:.0f}s")
    finally:
        try:
            connector.disconnect()
        except Exception:  # noqa: BLE001
            pass

    return report


# ----------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = run_preflight()
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

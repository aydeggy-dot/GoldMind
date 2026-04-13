"""Tests for scripts/watchdog.py and scripts/preflight.py.

Both scripts are importable via `scripts.watchdog` / `scripts.preflight`
because `scripts/` sits under the project root (added to sys.path in
conftest). Behavior is driven via injection so no real psutil, subprocess,
or MT5 is needed.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from config import load_config


# ----------------------------------------------------------------------
def _import(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ROOT = Path(__file__).resolve().parent.parent
watchdog = _import("gm_watchdog", ROOT / "scripts" / "watchdog.py")
preflight = _import("gm_preflight", ROOT / "scripts" / "preflight.py")


# ----------------------------------------------------------------------
# Watchdog
# ----------------------------------------------------------------------
def test_is_bot_running_matches_main_py():
    procs = [
        {"pid": 1, "name": "python.exe",
         "cmdline": ["python", str(watchdog.MAIN_SCRIPT)]},
        {"pid": 2, "name": "notepad.exe", "cmdline": ["notepad"]},
    ]
    assert watchdog.is_bot_running(procs) is True


def test_is_bot_running_ignores_non_python():
    procs = [
        {"pid": 1, "name": "notepad.exe",
         "cmdline": ["notepad", str(watchdog.MAIN_SCRIPT)]},
    ]
    assert watchdog.is_bot_running(procs) is False


def test_is_bot_running_matches_by_filename_only():
    # Windows vs Linux path separators + relative paths — match on basename
    procs = [{"pid": 1, "name": "python.exe", "cmdline": ["py", "main.py"]}]
    assert watchdog.is_bot_running(procs) is True


def test_watchdog_tick_ok_when_running():
    procs = [{"pid": 1, "name": "python.exe",
              "cmdline": [str(watchdog.MAIN_SCRIPT)]}]
    called = []
    verdict = watchdog.watchdog_tick(
        process_lister=lambda: procs,
        launcher=lambda py, script, cwd: called.append((py, script, cwd)),
    )
    assert verdict.startswith("ok")
    assert called == []  # launcher must NOT be called


def test_watchdog_tick_restarts_when_missing():
    called = []
    verdict = watchdog.watchdog_tick(
        process_lister=lambda: [],
        launcher=lambda py, script, cwd: called.append((py, script, cwd)),
        python_exe="python-fake",
    )
    assert verdict == "restarted"
    assert len(called) == 1 and called[0][0] == "python-fake"


def test_watchdog_tick_reports_launch_failure():
    def boom(*_a, **_kw):
        raise RuntimeError("simulated")
    verdict = watchdog.watchdog_tick(
        process_lister=lambda: [],
        launcher=boom,
        python_exe="x",
    )
    assert verdict.startswith("error")


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------
class _FakeConnector:
    def __init__(self, *, balance=1000.0, drift_seconds=0.0, fail_connect=False,
                 symbol="XAUUSD", info=None):
        self.balance = balance
        self.drift_seconds = drift_seconds
        self.fail_connect = fail_connect
        self._symbol = symbol
        self._info = info or {
            "point": 0.01, "trade_tick_value": 1.0, "trade_tick_size": 0.01,
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
            "trade_contract_size": 100.0, "swap_long": -5.0, "swap_short": -3.0,
        }

    def connect(self): return not self.fail_connect
    def disconnect(self): pass
    def discover_symbol(self, primary, fb): return self._symbol
    def get_symbol_info(self, symbol=None): return self._info
    def get_account_info(self):
        return {"balance": self.balance, "equity": self.balance,
                "margin": 0, "free_margin": self.balance}
    def get_clock_drift(self): return timedelta(seconds=self.drift_seconds)


def _bundle_fixture():
    cfg = load_config("config/config.example.yaml")
    creds = {
        "mt5": {"account": 99999999, "password": "real_pw",
                "server": "RealBroker-Live", "terminal_path": None},
        "telegram": {"bot_token": "99:real_token", "chat_id": "11111"},
    }
    return {"config": cfg, "credentials": creds}


def test_preflight_passes_happy_path(tmp_path, monkeypatch):
    bundle = _bundle_fixture()
    bundle["config"]["database"]["path"] = str(tmp_path / "pf.db")
    r = preflight.run_preflight(
        bundle=bundle,
        connector_factory=lambda cfg, creds: _FakeConnector(balance=1000.0),
    )
    assert r.ok, r.render()


def test_preflight_fails_on_placeholder_credentials(tmp_path):
    bundle = _bundle_fixture()
    bundle["config"]["database"]["path"] = str(tmp_path / "pf.db")
    bundle["credentials"]["mt5"]["password"] = "your_password_here"
    r = preflight.run_preflight(bundle=bundle,
                                connector_factory=lambda c, cr: _FakeConnector())
    assert not r.ok
    assert any("placeholder" in detail.lower()
               for _, ok, detail in r.checks if not ok)


def test_preflight_fails_when_balance_below_floor(tmp_path):
    bundle = _bundle_fixture()
    bundle["config"]["database"]["path"] = str(tmp_path / "pf.db")
    r = preflight.run_preflight(
        bundle=bundle,
        connector_factory=lambda c, cr: _FakeConnector(balance=50.0),
    )
    names = {n: ok for n, ok, _ in r.checks}
    assert names["balance above floor"] is False


def test_preflight_fails_on_excessive_clock_drift(tmp_path):
    bundle = _bundle_fixture()
    bundle["config"]["database"]["path"] = str(tmp_path / "pf.db")
    r = preflight.run_preflight(
        bundle=bundle,
        connector_factory=lambda c, cr: _FakeConnector(drift_seconds=9999.0),
    )
    names = {n: ok for n, ok, _ in r.checks}
    assert names["clock drift under pause-threshold"] is False


def test_preflight_fails_on_connect_failure(tmp_path):
    bundle = _bundle_fixture()
    bundle["config"]["database"]["path"] = str(tmp_path / "pf.db")
    r = preflight.run_preflight(
        bundle=bundle,
        connector_factory=lambda c, cr: _FakeConnector(fail_connect=True),
    )
    assert not r.ok
    assert any(n == "mt5 connect" and not ok for n, ok, _ in r.checks)


def test_preflight_reports_have_render_text(tmp_path):
    bundle = _bundle_fixture()
    bundle["config"]["database"]["path"] = str(tmp_path / "pf.db")
    r = preflight.run_preflight(bundle=bundle,
                                connector_factory=lambda c, cr: _FakeConnector())
    text = r.render()
    assert "Preflight" in text and ("PASS" in text or "FAIL" in text)

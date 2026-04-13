"""HealthMonitor — strategy + system + broker health.

Implements the Engine's HealthMonitor Protocol:

    heartbeat(account_info) -> None
    on_trade_closed(trade)  -> {"pause": bool, "alerts": list[str]}

Strategy health: 6 checks (prompt §ANALYTICS). 2+ failing = auto-pause.
System health: memory/CPU/disk via psutil (optional — missing psutil is
handled gracefully so tests stay lightweight).

Kept deliberately free of side effects — the engine decides what to do with
the returned readings (pause flag, alert messages). This makes the monitor
trivial to unit test without mocking an Engine.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from analytics.performance import compute_metrics

logger = logging.getLogger("goldmind")

try:
    import psutil  # type: ignore
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


@dataclass
class HealthReading:
    pause: bool = False
    alerts: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """Strategy/system/broker health. Pure — engine applies the decision."""

    def __init__(
        self,
        config: Mapping[str, Any],
        db: Any,
        notifier: Any | None = None,
    ) -> None:
        self.cfg = config
        self.health_cfg = config.get("health", {})
        self.db = db
        self.notifier = notifier

    # ------------------------------------------------------------------
    # Heartbeat (system health + clock drift owned by engine)
    # ------------------------------------------------------------------
    def heartbeat(self, account_info: Mapping[str, Any]) -> HealthReading:
        reading = HealthReading()
        if not _PSUTIL_AVAILABLE:
            return reading

        mem_limit = float(self.health_cfg.get("max_memory_mb", 500))
        cpu_limit = float(self.health_cfg.get("max_cpu_percent", 80))
        disk_min = float(self.health_cfg.get("min_disk_free_gb", 1.0))

        try:
            proc = psutil.Process()
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            cpu = psutil.cpu_percent(interval=None)
            disk = psutil.disk_usage(".").free / (1024 ** 3)
        except Exception as exc:  # noqa: BLE001
            logger.warning("psutil sampling failed: %s", exc)
            return reading

        reading.details = {"memory_mb": mem_mb, "cpu_pct": cpu, "disk_free_gb": disk}
        if mem_mb > mem_limit:
            reading.alerts.append(f"memory {mem_mb:.0f}MB > {mem_limit:.0f}MB")
        if cpu > cpu_limit:
            reading.alerts.append(f"cpu {cpu:.0f}% > {cpu_limit:.0f}%")
        if disk < disk_min:
            reading.alerts.append(f"disk_free {disk:.2f}GB < {disk_min:.2f}GB")
        self._emit_alerts(reading.alerts, category="system")
        return reading

    # ------------------------------------------------------------------
    # Strategy health (after every trade close)
    # ------------------------------------------------------------------
    def on_trade_closed(self, trade: Mapping[str, Any]) -> HealthReading:
        if not self.health_cfg.get("auto_pause_on_degradation", True):
            return HealthReading()

        window = int(self.health_cfg.get("evaluation_window_trades", 50))
        rows = self.db.fetchall(
            "SELECT * FROM trades WHERE is_backtest=0 AND exit_time IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (window,))
        trades = [dict(r) for r in reversed(rows)]
        if len(trades) < 5:  # not enough data
            return HealthReading(details={"trades": len(trades)})

        m = compute_metrics(trades, window=window)
        min_wr = float(self.health_cfg.get("min_win_rate_threshold", 35.0))
        min_pf = float(self.health_cfg.get("min_profit_factor_threshold", 1.0))

        failed: list[str] = []
        if m.win_rate < min_wr:
            failed.append(f"win_rate {m.win_rate:.1f}% < {min_wr:.1f}%")
        if m.profit_factor < min_pf:
            failed.append(f"profit_factor {m.profit_factor:.2f} < {min_pf:.2f}")
        if m.avg_rr_achieved and m.avg_rr_achieved < 1.0:
            failed.append(f"avg_rr {m.avg_rr_achieved:.2f} < 1.0")

        # Consecutive losses — use the tail of the window
        consec = 0
        for t in reversed(trades):
            if (t.get("pnl") or 0) < 0:
                consec += 1
            else:
                break
        hist_avg_losses = max(1, m.losses / max(1, m.trades // 10))
        if consec > 2 * hist_avg_losses:
            failed.append(f"consecutive_losses {consec} > 2x avg")

        # Drawdown duration (trades-long > 2x avg trades between wins)
        if m.max_drawdown_duration > 2 * max(1, m.trades // max(1, m.wins or 1)):
            failed.append(f"dd_duration {m.max_drawdown_duration} trades excessive")

        # Overtrading: last 24h trade count > historical hourly avg × 24 × 2
        failed.extend(self._overtrading_check(trades))

        reading = HealthReading(
            pause=len(failed) >= 2,
            alerts=failed,
            details={"metrics": m.__dict__},
        )
        self._emit_alerts(failed, category="strategy")
        return reading

    # ------------------------------------------------------------------
    # Broker health (called daily by engine)
    # ------------------------------------------------------------------
    def broker_health_check(
        self,
        *,
        current_spread_pts: float,
        spread_avg_recent_pts: float,
        spread_avg_baseline_pts: float,
    ) -> HealthReading:
        alerts: list[str] = []
        mult = float(self.cfg.get("broker_monitoring", {})
                     .get("spread_regime_change_multiplier", 2.0))
        if spread_avg_baseline_pts > 0 and spread_avg_recent_pts >= spread_avg_baseline_pts * mult:
            alerts.append(
                f"spread regime change: avg_recent={spread_avg_recent_pts:.0f}pts "
                f"vs baseline={spread_avg_baseline_pts:.0f}pts"
            )
        max_spread = float(self.cfg.get("risk", {}).get("max_spread_points", 40))
        if current_spread_pts > max_spread:
            alerts.append(f"current spread {current_spread_pts:.0f}pts > {max_spread:.0f}pts")
        self._emit_alerts(alerts, category="broker")
        return HealthReading(alerts=alerts, details={
            "current": current_spread_pts,
            "recent_avg": spread_avg_recent_pts,
            "baseline_avg": spread_avg_baseline_pts,
        })

    # ------------------------------------------------------------------
    def _overtrading_check(self, trades: list[Mapping[str, Any]]) -> list[str]:
        if len(trades) < 10:
            return []
        now = datetime.now(timezone.utc)
        recent = 0
        for t in trades:
            ts = t.get("entry_time")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts))
            except ValueError:
                continue
            if (now - dt).total_seconds() < 86400:
                recent += 1
        avg_per_day = len(trades) / 30.0  # rough baseline assuming ~30d window
        if avg_per_day > 0 and recent > avg_per_day * 2:
            return [f"overtrading: {recent} trades/24h vs avg {avg_per_day:.1f}"]
        return []

    def _emit_alerts(self, alerts: list[str], *, category: str) -> None:
        if not alerts or self.notifier is None:
            return
        for a in alerts:
            try:
                self.notifier.notify(category, a, urgent=True)
            except Exception:  # noqa: BLE001
                logger.exception("notify failed")

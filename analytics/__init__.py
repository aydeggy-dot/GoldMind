"""Analytics — performance metrics, health monitoring, daily/weekly reports."""
from analytics.performance import PerformanceMetrics, compute_metrics  # noqa: F401
from analytics.health_monitor import HealthMonitor, HealthReading  # noqa: F401
from analytics.dashboard import build_daily_report, build_weekly_report  # noqa: F401

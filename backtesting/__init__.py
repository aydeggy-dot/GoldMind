"""Backtesting — offline simulation reusing the live Strategy + RegimeDetector."""
from backtesting.backtester import Backtester, BacktestConfig, BacktestResult  # noqa: F401
from backtesting.walk_forward import WindowResult, run_walk_forward, validate_windows  # noqa: F401
from backtesting.report_generator import build_backtest_report, build_walk_forward_report  # noqa: F401

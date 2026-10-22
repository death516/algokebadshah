"""Backtesting engine and metrics module (Role B)."""
from backtest.engine import BTConfig, run_backtest
from backtest.metrics import compute_metrics, save_results
from backtest.data import load_candles, load_1m

__all__ = [
    "BTConfig",
    "run_backtest",
    "compute_metrics",
    "save_results",
    "load_candles",
    "load_1m",
]

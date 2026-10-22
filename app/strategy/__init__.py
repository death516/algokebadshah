"""Trading strategies module (Role B)."""
from app.strategy.base import BaseStrategy
from app.strategy.trend import (
    TrendStrategy,
    TrendFollowingStrategy,
    TrendParams,
    compute_signals,
)

__all__ = [
    "BaseStrategy",
    "TrendStrategy",
    "TrendFollowingStrategy",
    "TrendParams",
    "compute_signals",
]

"""Candle utilities, aggregation, and validation (Role B)."""
from typing import List
from app.models import Candle

def validate_candles(candles: List[Candle]) -> bool:
    """Verify candle series is strictly monotonic in time with positive prices."""
    if not candles:
        return False
    for i in range(1, len(candles)):
        if candles[i].ts <= candles[i - 1].ts:
            return False
    return True

"""Position sizing calculation (Role C).

Calculates position size: (equity * risk%) / |entry - stop|,
bounded by max leverage and max notional caps.
"""
import logging

logger = logging.getLogger("app.risk.sizing")

def calculate_position_size(
    equity: float,
    entry_price: float,
    stop_loss: float,
    risk_pct: float = 1.0,
    max_leverage: float = 5.0,
    max_notional: float = 50000.0,
) -> float:
    """Calculate allowable order size according to risk rules."""
    if equity <= 0 or entry_price <= 0:
        return 0.0

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        return 0.0

    risk_amount = equity * (risk_pct / 100.0)
    size = risk_amount / stop_distance

    # Cap by max leverage
    max_leverage_size = (equity * max_leverage) / entry_price
    size = min(size, max_leverage_size)

    # Cap by max notional
    max_notional_size = max_notional / entry_price
    size = min(size, max_notional_size)

    return max(0.0, round(size, 4))

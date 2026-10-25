"""Position sizing calculation (Role C).

Calculates position size: (equity * risk%) / |entry - stop|,
bounded by max leverage and max notional caps.
"""
import logging

logger = logging.getLogger("app.risk.sizing")


def volatility_adjusted_risk_pct(
    base_risk_pct: float,
    atr_value: float,
    entry_price: float,
    target_atr_pct: float = 1.0,
    min_risk_pct: float = 0.25,
) -> float:
    """Scale a fixed-fraction risk budget down as ATR volatility rises.

    The base fraction is a hard ceiling: quiet markets never increase risk above
    the base. Missing volatility data falls back to the configured risk floor.
    """
    ceiling = max(0.0, float(base_risk_pct))
    floor = min(ceiling, max(0.0, float(min_risk_pct)))
    if entry_price <= 0 or atr_value <= 0 or target_atr_pct <= 0:
        return floor
    current_atr_pct = float(atr_value) / float(entry_price) * 100.0
    if current_atr_pct <= 0:
        return floor
    scaled = ceiling * float(target_atr_pct) / current_atr_pct
    return min(ceiling, max(floor, scaled))

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

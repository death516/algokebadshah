import pytest
from app.strategy.trend import TrendFollowingStrategy

def test_trend_strategy_init():
    strategy = TrendFollowingStrategy(symbol="BTCUSD")
    assert strategy.symbol == "BTCUSD"

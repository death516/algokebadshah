"""Strategy base interfaces and abstract definitions (Role B)."""
from abc import ABC, abstractmethod
from typing import Optional, List
from app.models import Candle, Signal

class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""
    @abstractmethod
    def generate_signal(self, candles: List[Candle], htf_candles: List[Candle]) -> Optional[Signal]:
        """Generate a trading signal or return None."""
        pass

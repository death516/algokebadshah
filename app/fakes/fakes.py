import json
import os
import uuid
import time
from typing import Optional, List, Dict, Any
from app.models import Candle, Signal, Position, AccountState, RiskDecision, OrderRequest, OrderResult
from app.interfaces import Executor, MarketData, Store

class FakeExecutor:
    """Fake executor returning fixed AccountState and recording orders."""
    def __init__(self, initial_equity: float = 10000.0, symbol: str = "BTCUSD"):
        self.equity = initial_equity
        self.symbol = symbol
        self.orders: List[OrderRequest] = []
        self.order_results: List[OrderResult] = []
        self.positions: List[Position] = []
        self.daily_pnl: float = 0.0
        self.peak_equity: float = initial_equity
        self.last_data_ts: int = int(time.time())

    def execute(self, order: OrderRequest) -> OrderResult:
        self.orders.append(order)
        order_id = f"fake-{uuid.uuid4().hex[:8]}"
        result = OrderResult(
            ok=True,
            order_id=order_id,
            status="FILLED",
            error=None
        )
        self.order_results.append(result)

        # Update position
        pos = Position(
            symbol=order.symbol,
            side=order.side,
            size=order.size,
            entry_price=order.entry_price,
            unrealized_pnl=0.0
        )
        self.positions.append(pos)
        return result

    def flatten_all(self) -> None:
        self.positions.clear()

    def get_account_state(self) -> AccountState:
        return AccountState(
            equity=self.equity,
            open_positions=list(self.positions),
            daily_pnl=self.daily_pnl,
            peak_equity=self.peak_equity,
            last_data_ts=self.last_data_ts
        )


class FakeMarketData:
    """Market data provider reading candles from database store or fixture files."""
    def __init__(self, fixtures_dir: Optional[str] = None, store: Optional[Store] = None):
        self.store = store
        if fixtures_dir is None:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            fixtures_dir = os.path.join(base_dir, "tests", "fixtures")
        self.fixtures_dir = fixtures_dir
        self._15m_candles: List[Candle] = self._load_candles("candles_15m.json")
        self._1h_candles: List[Candle] = self._load_candles("candles_1h.json")

    def _load_candles(self, filename: str) -> List[Candle]:
        path = os.path.join(self.fixtures_dir, filename)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Candle(**item) for item in data]

    def get_candles(self, symbol: str, resolution: str, limit: int = 100) -> List[Candle]:
        if self.store and hasattr(self.store, "get_candles"):
            stored = self.store.get_candles(symbol, resolution, limit=limit)
            if stored and len(stored) > 0:
                return [Candle(**item) for item in stored]
        candles = self._1h_candles if resolution in ("1h", "60", "60m") else self._15m_candles
        return candles[-limit:] if limit > 0 else candles


class FakeStore:
    """In-memory store for signals, trades, and equity curve."""
    def __init__(self):
        self.signals: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = []

    def save_signal(self, signal: Signal, decision: RiskDecision) -> None:
        self.signals.append({
            "signal": signal.model_dump(),
            "decision": decision.model_dump(),
            "recorded_at": int(time.time())
        })

    def save_trade(self, order: OrderRequest, result: OrderResult, reason: str = "") -> None:
        self.trades.append({
            "order": order.model_dump(),
            "result": result.model_dump(),
            "reason": reason,
            "recorded_at": int(time.time())
        })

    def save_equity(self, ts: int, equity: float) -> None:
        self.equity_curve.append({
            "ts": ts,
            "equity": equity
        })

    def get_trades(self, limit: int = 100) -> List[dict]:
        return self.trades[-limit:]

    def get_equity_curve(self, limit: int = 1000) -> List[dict]:
        return self.equity_curve[-limit:]

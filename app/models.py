from pydantic import BaseModel
from typing import Literal, Optional

Side = Literal["buy", "sell"]

class Candle(BaseModel):
    ts: int            # unix seconds, candle open time
    open: float
    high: float
    low: float
    close: float
    volume: float

class Signal(BaseModel):
    symbol: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float
    reason: str        # human-readable, shown in logs and dashboard
    ts: int
    atr: float = 0.0
    trail_atr_mult: float = 0.0
    use_take_profit: bool = True

class Position(BaseModel):
    symbol: str
    side: Side
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    entry_ts: Optional[int] = None

class AccountState(BaseModel):
    equity: float
    open_positions: list[Position]
    daily_pnl: float
    peak_equity: float
    last_data_ts: int  # ts of latest market data seen (for stale-data guard)

class RiskDecision(BaseModel):
    approved: bool
    size: float = 0.0
    reason: str = ""

class OrderRequest(BaseModel):
    symbol: str
    side: Side
    size: float
    entry_price: float
    stop_loss: float
    take_profit: float
    client_order_id: str   # idempotency key
    signal_ts: Optional[int] = None
    atr: float = 0.0
    trail_atr_mult: float = 0.0
    use_take_profit: bool = True
    reason: str = ""

class OrderResult(BaseModel):
    ok: bool
    order_id: Optional[str] = None
    status: str = ""
    error: Optional[str] = None
    fill_price: Optional[float] = None
    filled_at: Optional[int] = None
    fee: Optional[float] = None

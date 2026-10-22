"""Phase 0 Smoke Test.

Validates that FakeMarketData loads sample candles,
a dummy Signal is generated, RiskManager evaluates it,
and FakeExecutor executes the OrderRequest without error.
"""
import pytest
from app.fakes import FakeMarketData, FakeExecutor, FakeStore
from app.models import Signal, OrderRequest
from app.risk.manager import DeltaRiskManager

def test_phase_0_smoke_pipeline():
    market_data = FakeMarketData()
    executor = FakeExecutor(initial_equity=10000.0)
    risk_manager = DeltaRiskManager()
    store = FakeStore()

    # 1. Fetch candles from fixtures
    candles_15m = market_data.get_candles("BTCUSD", "15m", limit=10)
    candles_1h = market_data.get_candles("BTCUSD", "1h", limit=10)
    assert len(candles_15m) == 10
    assert len(candles_1h) == 10

    # 2. Generate dummy signal
    latest = candles_15m[-1]
    signal = Signal(
        symbol="BTCUSD",
        side="buy",
        entry_price=latest.close,
        stop_loss=latest.close * 0.98,
        take_profit=latest.close * 1.04,
        reason="Phase 0 Smoke Test Signal",
        ts=latest.ts
    )

    # 3. RiskManager evaluate
    account = executor.get_account_state()
    decision = risk_manager.evaluate(signal, account)
    assert decision.approved
    assert decision.size > 0

    # 4. Executor execute
    order = OrderRequest(
        symbol=signal.symbol,
        side=signal.side,
        size=decision.size,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        client_order_id="smoke-test-order-1"
    )
    result = executor.execute(order)
    assert result.ok
    assert result.status == "FILLED"

    # 5. Store record
    store.save_signal(signal, decision)
    store.save_trade(order, result)
    assert len(store.signals) == 1
    assert len(store.trades) == 1

#!/usr/bin/env python3
"""Phase 0 Exit Gate Smoke Test Script.

Runs FakeMarketData -> Dummy Signal -> DeltaRiskManager -> FakeExecutor -> FakeStore.
Exits with 0 on success, 1 on error.
"""
import sys
import os

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.fakes import FakeMarketData, FakeExecutor, FakeStore
from app.models import Signal, OrderRequest
from app.risk.manager import DeltaRiskManager

def run_smoke_test():
    print("=" * 60)
    print("Starting Phase 0 Smoke Test (FakeMarketData -> Risk -> FakeExecutor)...")
    print("=" * 60)

    # Step 1: Initialize services
    market_data = FakeMarketData()
    executor = FakeExecutor(initial_equity=10000.0)
    risk_manager = DeltaRiskManager()
    store = FakeStore()

    # Step 2: Fetch candles
    candles_15m = market_data.get_candles("BTCUSD", "15m", limit=10)
    candles_1h = market_data.get_candles("BTCUSD", "1h", limit=10)
    print(f"[OK] Loaded {len(candles_15m)} 15m candles and {len(candles_1h)} 1h candles.")
    assert len(candles_15m) > 0, "No 15m candles found!"

    # Step 3: Formulate dummy signal
    latest = candles_15m[-1]
    signal = Signal(
        symbol="BTCUSD",
        side="buy",
        entry_price=latest.close,
        stop_loss=round(latest.close * 0.98, 2),
        take_profit=round(latest.close * 1.04, 2),
        reason="EMA cross confirmed by 1h trend",
        ts=latest.ts
    )
    print(f"[OK] Generated signal: {signal.side.upper()} {signal.symbol} at {signal.entry_price}")

    # Step 4: Evaluate risk
    account = executor.get_account_state()
    decision = risk_manager.evaluate(signal, account)
    print(f"[OK] Risk decision: approved={decision.approved}, size={decision.size}, reason='{decision.reason}'")
    assert decision.approved, f"Signal rejected: {decision.reason}"

    # Step 5: Execute order
    order = OrderRequest(
        symbol=signal.symbol,
        side=signal.side,
        size=decision.size,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        client_order_id="smoke-exit-gate-001"
    )
    result = executor.execute(order)
    print(f"[OK] Order executed: status={result.status}, order_id={result.order_id}")
    assert result.ok, f"Order failed: {result.error}"

    # Step 6: Persist in store
    store.save_signal(signal, decision)
    store.save_trade(order, result)
    print(f"[OK] Saved to store: {len(store.signals)} signal(s), {len(store.trades)} trade(s)")

    print("=" * 60)
    print("PHASE 0 SMOKE TEST PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    try:
        run_smoke_test()
    except Exception as e:
        print(f"[ERROR] Smoke test failed: {e}", file=sys.stderr)
        sys.exit(1)

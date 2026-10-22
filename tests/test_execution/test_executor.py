from unittest.mock import MagicMock, patch
import pytest

from app.execution.executor import DeltaExecutor
from app.execution.reconcile import Reconciler
from app.engine.runner import EngineRunner
from app.fakes import FakeMarketData, FakeStore
from app.models import OrderRequest, Position, Signal, RiskDecision
from app.risk.manager import DeltaRiskManager


def test_delta_executor_execute_with_mock_client():
    mock_client = MagicMock()
    mock_client.api_key = "test_key"
    mock_client.api_secret = "test_secret"
    mock_client.get_product_id.return_value = 27
    mock_client.place_order.return_value = {"result": {"id": "delta-ord-100", "state": "FILLED"}}

    executor = DeltaExecutor(client=mock_client)
    order = OrderRequest(
        symbol="BTCUSD",
        side="buy",
        size=0.1,
        entry_price=65000.0,
        stop_loss=64000.0,
        take_profit=67000.0,
        client_order_id="client-test-exec-1",
    )

    result = executor.execute(order)
    assert result.ok is True
    assert result.order_id == "delta-ord-100"
    assert result.status == "FILLED"

    mock_client.place_order.assert_called_once_with(
        product_id=27,
        size=0.1,
        side="buy",
        order_type="limit_order",
        price=65000.0,
        client_order_id="client-test-exec-1",
        stop_loss_price=64000.0,
        take_profit_price=67000.0,
    )


def test_delta_executor_flatten_all():
    mock_client = MagicMock()
    mock_client.api_key = "k"
    mock_client.api_secret = "s"
    mock_client.get_positions.return_value = [
        Position(symbol="BTCUSD", side="buy", size=0.2, entry_price=65000.0, unrealized_pnl=50.0),
        Position(symbol="BTCUSD", side="sell", size=0.1, entry_price=64000.0, unrealized_pnl=-20.0),
    ]
    mock_client.get_product_id.return_value = 27

    executor = DeltaExecutor(client=mock_client)
    executor.flatten_all()

    # Verify cancelled all orders
    mock_client.cancel_all_orders.assert_called_once()

    # Verify placed market orders in opposite directions to flatten
    assert mock_client.place_order.call_count == 2
    calls = mock_client.place_order.call_args_list

    # Position 1 was BUY 0.2 -> must SELL 0.2
    assert calls[0][1]["side"] == "sell"
    assert calls[0][1]["size"] == 0.2
    assert calls[0][1]["order_type"] == "market_order"

    # Position 2 was SELL 0.1 -> must BUY 0.1
    assert calls[1][1]["side"] == "buy"
    assert calls[1][1]["size"] == 0.1


def test_delta_executor_get_account_state():
    mock_client = MagicMock()
    mock_client.api_key = "k"
    mock_client.api_secret = "s"
    mock_client.get_balance.return_value = [{"balance": 15000.0}]
    mock_client.get_positions.return_value = [
        Position(symbol="BTCUSD", side="buy", size=0.05, entry_price=65000.0, unrealized_pnl=10.0),
    ]

    executor = DeltaExecutor(client=mock_client, fallback_equity=10000.0)
    state = executor.get_account_state()

    assert state.equity == 15000.0
    assert state.peak_equity == 15000.0
    assert len(state.open_positions) == 1
    assert state.last_data_ts > 0


def test_reconciler_checks_brackets():
    mock_client = MagicMock()
    mock_client.api_key = "k"
    mock_client.api_secret = "s"
    mock_client.get_positions.return_value = [
        Position(symbol="BTCUSD", side="buy", size=0.1, entry_price=65000.0, unrealized_pnl=0.0),
    ]
    # No stop orders active
    mock_client.get_open_orders.return_value = []
    mock_client.get_product_id.return_value = 27

    reconciler = Reconciler(client=mock_client)
    summary = reconciler.reconcile()

    assert summary["positions_count"] == 1
    assert len(summary["missing_brackets"]) == 1
    assert summary["missing_brackets"][0]["symbol"] == "BTCUSD"


def test_engine_runner_tick_pipeline():
    mock_strategy = MagicMock()
    # Mock strategy emits a signal
    mock_signal = Signal(
        symbol="BTCUSD",
        side="buy",
        entry_price=65000.0,
        stop_loss=64000.0,
        take_profit=67000.0,
        reason="Runner Test Signal",
        ts=1700000000,
    )
    mock_strategy.generate_signal.return_value = mock_signal

    mock_client = MagicMock()
    mock_client.api_key = "k"
    mock_client.api_secret = "s"
    mock_client.get_product_id.return_value = 27
    mock_client.place_order.return_value = {"result": {"id": "delta-run-1", "state": "FILLED"}}

    market_data = FakeMarketData()
    executor = DeltaExecutor(client=mock_client, fallback_equity=10000.0)
    risk_manager = DeltaRiskManager()
    store = FakeStore()

    runner = EngineRunner(
        strategy=mock_strategy,
        risk_manager=risk_manager,
        executor=executor,
        market_data=market_data,
        store=store,
    )

    result = runner.run_tick()

    assert result is not None
    assert result.ok is True
    assert result.order_id == "delta-run-1"
    assert len(store.signals) == 1
    assert len(store.trades) == 1

import pytest
import os
import uuid
import tempfile
from app.models import Signal, RiskDecision, OrderRequest, OrderResult
from app.storage.store import SQLiteStore, PostgresStore, get_default_store

def test_sqlite_store_lifecycle():
    temp_db = os.path.join(tempfile.gettempdir(), f"test_store_{uuid.uuid4().hex[:8]}.db")
    
    try:
        store = SQLiteStore(db_path=temp_db)
        
        sig = Signal(
            symbol="BTCUSD",
            side="buy",
            entry_price=65000.0,
            stop_loss=64000.0,
            take_profit=67000.0,
            reason="Test signal",
            ts=1700000000
        )
        dec = RiskDecision(approved=True, size=0.1, reason="Approved")
        store.save_signal(sig, dec)

        order = OrderRequest(
            symbol="BTCUSD",
            side="buy",
            size=0.1,
            entry_price=65000.0,
            stop_loss=64000.0,
            take_profit=67000.0,
            client_order_id="test-sqlite-1"
        )
        res = OrderResult(ok=True, order_id="delta-sqlite-001", status="FILLED")
        store.save_trade(order, res)
        store.save_equity(1700000000, 10000.0)
        store.save_risk_event("HALT", "Daily loss limit breached")
        store.save_log("INFO", "Bot started successfully", "app.main")

        trades = store.get_trades(limit=10)
        assert len(trades) == 1
        assert trades[0]["symbol"] == "BTCUSD"

        signals = store.get_signals(limit=10)
        assert len(signals) == 1
        assert signals[0]["approved"] == 1

        curve = store.get_equity_curve(limit=10)
        assert len(curve) == 1
        assert curve[0]["equity"] == 10000.0

        latest_eq = store.get_latest_equity()
        assert latest_eq == 10000.0

        events = store.get_risk_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "HALT"

        logs = store.get_logs(limit=10)
        assert len(logs) == 1
        assert logs[0]["level"] == "INFO"
        assert "Bot started" in logs[0]["message"]

        # Test clear_all
        store.clear_all()
        assert len(store.get_trades()) == 0
        assert len(store.get_signals()) == 0
        assert len(store.get_equity_curve()) == 0
        assert len(store.get_risk_events()) == 0
        assert len(store.get_logs()) == 0
    finally:
        try:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        except OSError:
            pass


def test_sqlite_store_logging_handler():
    import logging
    from app.storage.store import StoreLoggingHandler

    temp_db = os.path.join(tempfile.gettempdir(), f"test_log_{uuid.uuid4().hex[:8]}.db")
    try:
        store = SQLiteStore(db_path=temp_db)
        handler = StoreLoggingHandler(store=store, level=logging.INFO)
        test_logger = logging.getLogger("test_logger")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        test_logger.info("Test log message via handler")

        logs = store.get_logs(limit=10)
        assert len(logs) >= 1
        assert "Test log message via handler" in logs[0]["message"]
        test_logger.removeHandler(handler)
    finally:
        try:
            if os.path.exists(temp_db):
                os.remove(temp_db)
        except OSError:
            pass


def test_postgres_store():
    store = get_default_store()
    # Test that store implements protocol and saves/reads properly
    sig = Signal(
        symbol="BTCUSD",
        side="sell",
        entry_price=68000.0,
        stop_loss=69000.0,
        take_profit=66000.0,
        reason="EMA cross down",
        ts=1700000050
    )
    dec = RiskDecision(approved=True, size=0.08, reason="Approved")
    store.save_signal(sig, dec)

    order = OrderRequest(
        symbol="BTCUSD",
        side="sell",
        size=0.08,
        entry_price=68000.0,
        stop_loss=69000.0,
        take_profit=66000.0,
        client_order_id="test-pg-order-99"
    )
    res = OrderResult(ok=True, order_id="delta-pg-999", status="FILLED")
    store.save_trade(order, res)

    trades = store.get_trades(limit=5)
    assert len(trades) >= 1

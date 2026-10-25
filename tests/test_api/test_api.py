import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app, risk_manager, executor, store, market_data
from app.models import Signal, RiskDecision, OrderRequest, OrderResult

@pytest.fixture(autouse=True)
def reset_state():
    # Ensure clean state before each test
    risk_manager.resume()
    executor.flatten_all()
    if hasattr(store, "clear_all"):
        store.clear_all()
    yield
    risk_manager.resume()
    executor.flatten_all()


def test_app_health_and_root():
    client = TestClient(app)

    resp_root = client.get("/")
    assert resp_root.status_code == 200
    assert resp_root.json()["status"] == "running"

    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    assert resp_health.json() == {"status": "ok"}


def test_api_status_and_pnl_dual_prefix():
    client = TestClient(app)

    # Test /api/status and /status both return valid responses
    resp1 = client.get("/api/status")
    resp2 = client.get("/status")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["status"] == "running"
    assert resp2.json()["status"] == "running"
    assert "equity" in resp1.json()
    assert "daily_pnl" in resp1.json()
    assert "is_halted" in resp1.json()
    assert resp1.json()["is_halted"] is False

    # Test /api/pnl and /pnl
    resp_pnl1 = client.get("/api/pnl")
    resp_pnl2 = client.get("/pnl")
    assert resp_pnl1.status_code == 200
    assert resp_pnl2.status_code == 200
    assert "daily_pnl" in resp_pnl1.json()
    assert "unrealized_pnl" in resp_pnl1.json()
    assert "equity" in resp_pnl1.json()


def test_api_trades_signals_equity_persistence():
    client = TestClient(app)

    sig = Signal(
        symbol="BTCUSD",
        side="buy",
        entry_price=64000.0,
        stop_loss=63000.0,
        take_profit=66000.0,
        reason="Test route signal",
        ts=1700000000,
    )
    dec = RiskDecision(approved=True, size=0.05, reason="Approved by risk")
    store.save_signal(sig, dec)

    order = OrderRequest(
        symbol="BTCUSD",
        side="buy",
        size=0.05,
        entry_price=64000.0,
        stop_loss=63000.0,
        take_profit=66000.0,
        client_order_id="client-test-api-001",
    )
    result = OrderResult(ok=True, order_id="delta-order-001", status="FILLED")
    store.save_trade(order, result)
    store.save_equity(1700000000, 10000.0)

    # Test GET /api/trades and /trades
    resp_trades = client.get("/api/trades?limit=10")
    assert resp_trades.status_code == 200
    assert len(resp_trades.json()) == 1
    assert resp_trades.json()[0]["symbol"] == "BTCUSD"

    # Test GET /api/signals and /signals
    resp_signals = client.get("/signals?limit=10")
    assert resp_signals.status_code == 200
    assert len(resp_signals.json()) == 1
    assert resp_signals.json()[0]["reason"] == "Test route signal"

    # Test GET /api/equity and /equity
    resp_equity = client.get("/api/equity?limit=10")
    assert resp_equity.status_code == 200
    assert len(resp_equity.json()) == 1
    assert resp_equity.json()[0]["equity"] == 10000.0


def test_api_kill_switch_and_resume():
    client = TestClient(app)

    # Initial state: running
    assert not risk_manager.is_halted()

    # Trigger emergency kill switch
    resp_kill = client.post("/api/kill", json={"reason": "Emergency hackathon kill test"})
    assert resp_kill.status_code == 200
    data = resp_kill.json()
    assert data["status"] == "killed"
    assert data["reason"] == "Emergency hackathon kill test"
    assert risk_manager.is_halted()

    # GET /api/risk should reflect halted state
    resp_risk = client.get("/api/risk")
    assert resp_risk.status_code == 200
    assert resp_risk.json()["halted"] is True
    assert "kill" in resp_risk.json()["halt_reason"].lower()

    # Resume trading via /resume
    resp_resume = client.post("/resume")
    assert resp_resume.status_code == 200
    assert resp_resume.json()["status"] == "resumed"
    assert not risk_manager.is_halted()


def test_api_pause():
    client = TestClient(app)

    resp_pause = client.post("/pause")
    assert resp_pause.status_code == 200
    assert resp_pause.json()["status"] == "paused"
    assert risk_manager.is_halted()


def test_api_price_and_ticker():
    client = TestClient(app)

    resp = client.get("/api/price")
    assert resp.status_code == 200
    data = resp.json()
    assert "price" in data
    assert "symbol" in data
    assert data["symbol"] == "BTCUSD"
    assert data["price"] > 0


def test_api_orders_and_positions():
    client = TestClient(app)

    resp_orders = client.get("/api/orders")
    assert resp_orders.status_code == 200
    assert isinstance(resp_orders.json(), list)

    resp_pos = client.get("/api/positions")
    assert resp_pos.status_code == 200
    assert isinstance(resp_pos.json(), list)


def test_api_strategy_endpoint():
    client = TestClient(app)

    resp = client.get("/api/strategy")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "BTCUSD"
    assert "timeframe" in data
    assert "status" in data


def test_api_logs_endpoint():
    client = TestClient(app)

    if hasattr(store, "save_log"):
        store.save_log("WARNING", "Test warning message", "test.module")

    resp = client.get("/api/logs?limit=10")
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) >= 1
    assert any("Test warning" in l["message"] for l in logs)


def test_api_demo_trigger():
    client = TestClient(app)
    executor.update_price(64000.0, int(time.time()))

    resp = client.post("/api/demo/trigger", json={"side": "buy"})
    assert resp.status_code == 200
    data = resp.json()
    assert "signal" in data
    assert "decision" in data
    assert data["signal"]["side"] == "buy"
    assert data["decision"]["approved"] is True
    assert data["order"] is not None
    assert data["result"]["ok"] is True
    assert data["paper_only"] is True
    assert data["price_source"] == "live_mark"
    assert data["order"]["size"] * data["signal"]["entry_price"] <= 100.01

    # Trades endpoint should now have this demo trade
    trades_resp = client.get("/api/trades")
    assert trades_resp.status_code == 200
    assert len(trades_resp.json()) >= 1


def test_api_demo_trigger_requires_fresh_paper_price():
    client = TestClient(app)
    executor.orders.clear()
    executor.last_price = 0.0
    executor.last_data_ts = 0

    resp = client.post("/api/demo/trigger", json={"side": "buy"})

    assert resp.status_code == 503
    assert "No fresh market price" in resp.json()["detail"]
    assert executor.orders == []


def test_api_websocket_connection_and_actions():
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        # First message is snapshot
        data = ws.receive_json()
        assert data["type"] == "snapshot"
        assert "equity" in data["data"]
        assert "symbol" in data["data"]

        # Ping-pong
        ws.send_text(json.dumps({"action": "ping"}))
        pong = ws.receive_json()
        assert pong["type"] == "pong"
        assert "ts" in pong

        # Request snapshot on demand
        ws.send_text(json.dumps({"action": "snapshot"}))
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"

        # Trigger kill switch via WebSocket
        ws.send_text(json.dumps({"action": "kill", "reason": "WS Kill Test"}))
        kill_msg = ws.receive_json()
        assert kill_msg["type"] == "kill_switch"
        assert risk_manager.is_halted()

        # Resume via WebSocket
        ws.send_text(json.dumps({"action": "resume"}))
        resume_msg = ws.receive_json()
        assert resume_msg["type"] == "resume"
        assert not risk_manager.is_halted()


def test_end_to_end_pipeline_integration():
    """End-to-end integration test per codex_team_prompt.md Phase C3:
    signal -> risk -> fake executor -> store -> API shows it.
    """
    client = TestClient(app)

    # 1. Generate Signal
    sig = Signal(
        symbol="BTCUSD",
        side="buy",
        entry_price=65000.0,
        stop_loss=64000.0,
        take_profit=67000.0,
        reason="E2E Pipeline Integration Test Signal",
        ts=1700000000,
    )

    # 2. Risk Manager Evaluate
    account = executor.get_account_state()
    decision = risk_manager.evaluate(sig, account)
    assert decision.approved
    assert decision.size > 0

    # 3. Fake Executor Execute
    order = OrderRequest(
        symbol=sig.symbol,
        side=sig.side,
        size=decision.size,
        entry_price=sig.entry_price,
        stop_loss=sig.stop_loss,
        take_profit=sig.take_profit,
        client_order_id="e2e-order-12345",
    )
    result = executor.execute(order)
    assert result.ok
    assert result.status == "FILLED"

    # 4. Store Persistence
    store.save_signal(sig, decision)
    store.save_trade(order, result)
    store.save_equity(1700000000, 10000.0)

    # 5. Verify API shows all saved data
    trades_resp = client.get("/api/trades")
    assert trades_resp.status_code == 200
    assert any(t["client_order_id"] == "e2e-order-12345" for t in trades_resp.json())

    signals_resp = client.get("/api/signals")
    assert signals_resp.status_code == 200
    assert any("E2E Pipeline" in s["reason"] for s in signals_resp.json())

    positions_resp = client.get("/api/positions")
    assert positions_resp.status_code == 200
    assert len(positions_resp.json()) >= 1
    assert positions_resp.json()[0]["symbol"] == "BTCUSD"

    status_resp = client.get("/api/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "running"
    assert status_resp.json()["open_positions_count"] >= 1


def test_dashboard_and_candles_endpoints():
    client = TestClient(app)

    # 1. Test /dashboard
    resp_dash = client.get("/dashboard")
    assert resp_dash.status_code == 200
    assert "Delta Quant Core" in resp_dash.text
    assert "8-Rule Risk Radar" in resp_dash.text

    # 2. Test /ui
    resp_ui = client.get("/ui")
    assert resp_ui.status_code == 200
    assert "Delta Quant Core" in resp_ui.text

    # 3. Test /candles
    resp_candles = client.get("/api/candles?limit=10")
    assert resp_candles.status_code == 200
    assert isinstance(resp_candles.json(), list)


def test_api_v1_endpoints_and_flatten():
    client = TestClient(app)

    # 1. Test /api/v1/status
    res_status = client.get("/api/v1/status")
    assert res_status.status_code == 200
    assert res_status.json()["status"] == "running"

    # 2. Test /api/v1/demo/trigger
    res_demo = client.post("/api/v1/demo/trigger", json={"side": "buy"})
    assert res_demo.status_code == 200
    assert res_demo.json()["result"]["ok"] is True

    # Position should now exist
    pos_res = client.get("/api/v1/positions")
    assert pos_res.status_code == 200
    assert len(pos_res.json()) >= 1

    # 3. Test 1-click /api/v1/positions/flatten
    res_flatten = client.post("/api/v1/positions/flatten")
    assert res_flatten.status_code == 200
    assert res_flatten.json()["status"] == "flattened"

    # Positions should be empty
    pos_after = client.get("/api/v1/positions")
    assert pos_after.status_code == 200
    assert len(pos_after.json()) == 0
    # Engine should not be halted by flatten
    assert not risk_manager.is_halted()

    # 4. Test /api/v1/pause and /api/v1/resume
    res_pause = client.post("/api/v1/pause")
    assert res_pause.status_code == 200
    assert res_pause.json()["status"] == "paused"
    assert risk_manager.is_halted()

    res_resume = client.post("/api/v1/resume")
    assert res_resume.status_code == 200
    assert res_resume.json()["status"] == "resumed"
    assert not risk_manager.is_halted()

    # 5. Test /api/v1/kill
    res_kill = client.post("/api/v1/kill", json={"reason": "v1 kill test"})
    assert res_kill.status_code == 200
    assert res_kill.json()["status"] == "killed"
    assert risk_manager.is_halted()

    # Reset
    risk_manager.resume()


def test_mode_and_history_endpoints():
    client = TestClient(app)

    # 1. Test GET /api/v1/mode
    res_mode = client.get("/api/v1/mode")
    assert res_mode.status_code == 200
    assert "mode" in res_mode.json()

    # 2. Test POST /api/v1/mode
    res_set = client.post("/api/v1/mode", json={"mode": "uploaded"})
    assert res_set.status_code == 200
    assert res_set.json()["mode"] == "uploaded"

    res_back = client.post("/api/v1/mode", json={"mode": "live"})
    assert res_back.status_code == 200
    assert res_back.json()["mode"] == "live"


def test_candles_storage_upload_and_history(tmp_path, monkeypatch):
    from app.api import routes as api_routes
    monkeypatch.setattr(api_routes, "_UPLOAD_DATA_DIR", tmp_path)
    client = TestClient(app)

    # 1. Test GET /api/v1/candles/summary
    res_summary = client.get("/api/v1/candles/summary")
    assert res_summary.status_code == 200
    assert isinstance(res_summary.json(), dict)

    # 2. Test POST /api/v1/candles/upload with CSV data
    csv_content = (
        "ts,open,high,low,close,volume\n"
        "1700000000,82000.0,82500.0,81900.0,82300.0,10.5\n"
        "1700000900,82300.0,82800.0,82200.0,82700.0,15.2\n"
        "1700001800,82700.0,83200.0,82600.0,83000.0,12.8\n"
    )
    files = {"file": ("test_btc_15m.csv", csv_content, "text/csv")}
    res_upload = client.post("/api/v1/candles/upload?timeframe=15m", files=files)
    assert res_upload.status_code == 200
    upload_data = res_upload.json()
    assert upload_data["status"] == "ok"
    assert upload_data["count"] >= 3

    # 3. Test GET /api/v1/candles with resolution
    res_candles = client.get("/api/v1/candles?resolution=15m&limit=10")
    assert res_candles.status_code == 200
    candles = res_candles.json()
    assert len(candles) >= 3

    # Uploaded candle history remains available for chart inspection.
    res_history = client.get("/api/v1/history/candles?resolution=15m&symbol=BTCUSD&limit=10")
    assert res_history.status_code == 200
    assert len(res_history.json()) >= 3



import hashlib
import hmac
import time
from unittest.mock import MagicMock, patch
import pytest

from app.exchange.client import DeltaClient, DeltaMarketData, DeltaAPIError
from app.models import Candle


def test_delta_client_init():
    client = DeltaClient(
        base_url="https://cdn-ind.testnet.deltaex.org",
        api_key="my_key",
        api_secret="my_secret",
    )
    assert client.base_url == "https://cdn-ind.testnet.deltaex.org"
    assert client.api_key == "my_key"
    assert client.api_secret == "my_secret"


def test_signature_generation_matches_hmac_sha256():
    client = DeltaClient(api_key="test_key", api_secret="test_secret")

    method = "GET"
    path = "/v2/wallet/balances"
    query = "?param=1"
    payload = ""
    ts = "1700000000"

    expected_msg = f"{method}{ts}{path}{query}{payload}"
    expected_sig = hmac.new(
        b"test_secret",
        expected_msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    ret_ts, ret_sig = client.generate_signature(
        method=method,
        path=path,
        query_string=query,
        payload=payload,
        timestamp=ts,
    )

    assert ret_ts == ts
    assert ret_sig == expected_sig


def test_get_product_by_symbol_and_caching():
    client = DeltaClient(api_key="k", api_secret="s")
    mock_products = [
        {"id": 27, "symbol": "BTCUSD", "contract_type": "perpetual_futures"},
        {"id": 35, "symbol": "ETHUSD", "contract_type": "perpetual_futures"},
    ]

    with patch.object(client, "_request", return_value={"result": mock_products}) as mock_req:
        p1 = client.get_product_by_symbol("BTCUSD")
        assert p1 is not None
        assert p1["id"] == 27
        assert client.get_product_id("BTCUSD") == 27

        # Second call should use cache, not making another request
        p2 = client.get_product_by_symbol("ETHUSD")
        assert p2["id"] == 35
        assert mock_req.call_count == 1


def test_get_candles_parses_into_models():
    client = DeltaClient(api_key="k", api_secret="s")
    mock_candles = [
        {"time": 1700000900, "open": 60100, "high": 60200, "low": 60050, "close": 60150, "volume": 12.5},
        {"time": 1700000000, "open": 60000, "high": 60100, "low": 59900, "close": 60050, "volume": 10.0},
    ]

    with patch.object(client, "_request", return_value={"result": mock_candles}):
        candles = client.get_candles(symbol="BTCUSD", resolution="15m", limit=10)
        assert len(candles) == 2
        # Verify sorted chronologically (oldest first)
        assert candles[0].ts == 1700000000
        assert candles[1].ts == 1700000900
        assert isinstance(candles[0], Candle)
        assert candles[0].close == 60050.0


def test_place_bracket_order_payload():
    client = DeltaClient(api_key="k", api_secret="s")

    with patch.object(client, "_request", return_value={"result": {"id": "delta-99", "state": "OPEN"}}) as mock_req:
        client.place_order(
            product_id=27,
            size=0.1,
            side="buy",
            order_type="limit_order",
            price=60000.0,
            client_order_id="client-test-1",
            stop_loss_price=59000.0,
            take_profit_price=62000.0,
        )

        assert mock_req.call_count == 1
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/v2/orders"
        data = call_args[1]["data"]
        assert data["product_id"] == 27
        assert data["side"] == "buy"
        assert data["limit_price"] == "60000.0"
        assert data["bracket_stop_loss_price"] == "59000.0"
        assert data["bracket_take_profit_price"] == "62000.0"
        assert data["client_order_id"] == "client-test-1"


def test_delta_market_data_adapter():
    client = DeltaClient()
    mock_candles = [
        Candle(ts=1700000000, open=60000, high=60100, low=59900, close=60050, volume=10.0),
    ]

    with patch.object(client, "get_candles", return_value=mock_candles):
        market_data = DeltaMarketData(client=client)
        candles = market_data.get_candles(symbol="BTCUSD", resolution="15m", limit=1)
        assert len(candles) == 1
        assert candles[0].close == 60050.0

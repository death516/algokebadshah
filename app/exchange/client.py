"""Delta Exchange REST API client and MarketData adapter (Role A).

Handles HMAC-SHA256 request authentication, products resolution,
historical candles, wallet balance, active positions, order placement
(including bracket SL/TP orders), and cancellations with retry logic.
"""
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Optional, List, Dict, Any

import httpx

from app.config import settings
from app.interfaces import MarketData
from app.models import Candle, Position

logger = logging.getLogger("app.exchange.client")


class DeltaAPIError(Exception):
    """Custom exception raised for Delta Exchange API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[Any] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class DeltaClient:
    """Production-grade REST client for Delta Exchange Testnet with HMAC-SHA256 authentication."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.base_url = (base_url or settings.DELTA_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.DELTA_API_KEY
        self.api_secret = api_secret if api_secret is not None else settings.DELTA_API_SECRET
        self.timeout = timeout
        self._products_cache: Dict[str, Dict[str, Any]] = {}
        self._products_cached_at: float = 0.0
        self._client: Optional[httpx.Client] = None
        logger.info("[exchange] Initialized DeltaClient targeting %s", self.base_url)

    def _get_http_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()

    def generate_signature(
        self,
        method: str,
        path: str,
        query_string: str = "",
        payload: str = "",
        timestamp: Optional[str] = None,
    ) -> tuple[str, str]:
        """Generate Delta Exchange HMAC-SHA256 signature and timestamp header.

        Signature formula:
            message = method.upper() + timestamp + path + query_string + payload
            signature = hmac_sha256(api_secret, message)
        """
        ts = timestamp if timestamp is not None else str(int(time.time()))
        message = method.upper() + ts + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return ts, signature

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Execute HTTP request with authentication, query formatting, and exponential backoff retries."""
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        query_string = ""
        if params:
            # Filter out None values and encode
            filtered_params = {k: v for k, v in params.items() if v is not None}
            if filtered_params:
                query_string = "?" + urllib.parse.urlencode(filtered_params)

        payload_str = ""
        if data is not None:
            # Compact JSON without whitespaces per Delta API specs
            payload_str = json.dumps(data, separators=(",", ":"))

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "delta-algo-trader/1.0",
        }

        if auth and self.api_key and self.api_secret:
            ts, sig = self.generate_signature(
                method=method,
                path=path,
                query_string=query_string,
                payload=payload_str,
            )
            headers["api-key"] = self.api_key
            headers["timestamp"] = ts
            headers["signature"] = sig

        url = f"{self.base_url}{path}{query_string}"
        client = self._get_http_client()

        last_error = None
        for attempt in range(max_retries):
            try:
                response = client.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    content=payload_str.encode("utf-8") if payload_str else None,
                )

                if response.status_code == 429:
                    wait_sec = 1.5 * (2 ** attempt)
                    logger.warning("[exchange] Rate limit (429) hit. Retrying in %.2fs...", wait_sec)
                    time.sleep(wait_sec)
                    continue

                if response.status_code >= 400:
                    try:
                        err_json = response.json()
                    except Exception:
                        err_json = {"error": response.text}
                    raise DeltaAPIError(
                        f"Delta API error {response.status_code}: {err_json}",
                        status_code=response.status_code,
                        response_data=err_json,
                    )

                return response.json()

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                last_error = exc
                wait_sec = 1.0 * (2 ** attempt)
                logger.warning("[exchange] Network error (%s). Retry %d/%d in %.2fs", exc, attempt + 1, max_retries, wait_sec)
                time.sleep(wait_sec)

        raise DeltaAPIError(f"Request failed after {max_retries} retries: {last_error}")

    # -------------------------------------------------------------------------
    # Products & Metadata
    # -------------------------------------------------------------------------

    def get_products(self) -> List[Dict[str, Any]]:
        """Fetch active products to resolve symbol -> product_id."""
        now = time.time()
        if self._products_cache and (now - self._products_cached_at < 300):
            return list(self._products_cache.values())

        data = self._request("GET", "/v2/products", auth=False)
        result = data.get("result", [])
        if isinstance(result, list):
            self._products_cache = {p.get("symbol", ""): p for p in result if "symbol" in p}
            self._products_cached_at = now
            return result
        return []

    def get_product_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Resolve a symbol (e.g. 'BTCUSD') to product metadata including product_id."""
        if not self._products_cache:
            self.get_products()
        if symbol in self._products_cache:
            return self._products_cache[symbol]

        # Refresh cache if not found
        self.get_products()
        return self._products_cache.get(symbol)

    def get_product_id(self, symbol: str) -> int:
        """Resolve symbol to product_id integer."""
        prod = self.get_product_by_symbol(symbol)
        if prod and "id" in prod:
            return int(prod["id"])
        raise DeltaAPIError(f"Product ID not found for symbol {symbol}")

    # -------------------------------------------------------------------------
    # Market Data
    # -------------------------------------------------------------------------

    def get_candles(self, symbol: str, resolution: str = "15m", limit: int = 100) -> List[Candle]:
        """Fetch historical OHLCV candles from /v2/history/candles.

        Returns candles in ascending chronological order (oldest -> newest).
        """
        # Map resolution to approximate seconds per bar
        res_seconds_map = {
            "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400,
        }
        bar_sec = res_seconds_map.get(resolution, 900)
        end_time = int(time.time())
        # Fetch slightly extra to ensure enough candles
        start_time = end_time - (bar_sec * (limit + 5))

        params = {
            "symbol": symbol,
            "resolution": resolution,
            "start": start_time,
            "end": end_time,
        }

        try:
            data = self._request("GET", "/v2/history/candles", params=params, auth=False)
            raw_candles = data.get("result", [])
        except Exception as e:
            logger.warning("[exchange] Could not fetch candles from API (%s).", e)
            return []

        candles: List[Candle] = []
        for item in raw_candles:
            try:
                # Delta candles item format: {'time': ts, 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}
                ts = int(item.get("time", item.get("ts", 0)))
                # If timestamp is in microseconds or milliseconds, convert to seconds
                if ts > 1e11:
                    ts = int(ts / 1e6 if ts > 1e14 else ts / 1e3)

                candles.append(Candle(
                    ts=ts,
                    open=float(item.get("open", 0.0)),
                    high=float(item.get("high", 0.0)),
                    low=float(item.get("low", 0.0)),
                    close=float(item.get("close", 0.0)),
                    volume=float(item.get("volume", 0.0)),
                ))
            except Exception as parse_err:
                logger.debug("[exchange] Skipped malformed candle: %s (%s)", item, parse_err)

        # Delta returns newest first; reverse so oldest is first
        candles.sort(key=lambda c: c.ts)
        return candles[-limit:] if limit > 0 else candles

    # -------------------------------------------------------------------------
    # Wallet & Account
    # -------------------------------------------------------------------------

    def get_balance(self) -> Dict[str, Any]:
        """Fetch account wallet balances."""
        data = self._request("GET", "/v2/wallet/balances", auth=True)
        return data.get("result", data)

    def get_positions(self, product_id: Optional[int] = None) -> List[Position]:
        """Fetch open derivative positions."""
        if product_id is not None:
            data = self._request("GET", "/v2/positions", params={"product_id": product_id}, auth=True)
            res = data.get("result", {})
            if isinstance(res, dict):
                size = float(res.get("size", 0.0) or 0.0)
                if abs(size) > 0:
                    symbol = res.get("product_symbol", res.get("symbol", settings.SYMBOL))
                    return [Position(
                        symbol=symbol,
                        side="buy" if size > 0 else "sell",
                        size=abs(size),
                        entry_price=float(res.get("entry_price", 0.0) or 0.0),
                        unrealized_pnl=float(res.get("unrealized_pnl", 0.0) or 0.0),
                    )]
                return []

        try:
            data = self._request("GET", "/v2/positions/margined", auth=True)
            result = data.get("result", [])
        except Exception:
            try:
                pid = self.get_product_id(settings.SYMBOL)
                return self.get_positions(product_id=pid)
            except Exception:
                return []

        positions: List[Position] = []
        if isinstance(result, list):
            for item in result:
                size = float(item.get("size", 0.0) or 0.0)
                if abs(size) <= 0:
                    continue
                side_str = "buy" if size > 0 else "sell"
                symbol = item.get("product_symbol", item.get("symbol", settings.SYMBOL))
                positions.append(Position(
                    symbol=symbol,
                    side=side_str,
                    size=abs(size),
                    entry_price=float(item.get("entry_price", 0.0) or 0.0),
                    unrealized_pnl=float(item.get("unrealized_pnl", 0.0) or 0.0),
                ))
        return positions

    # -------------------------------------------------------------------------
    # Orders
    # -------------------------------------------------------------------------

    def get_open_orders(self, product_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch open or pending orders."""
        params = {"states": "open,pending"}
        if product_id is not None:
            params["product_id"] = product_id
        data = self._request("GET", "/v2/orders", params=params, auth=True)
        return data.get("result", [])

    def place_order(
        self,
        product_id: int,
        size: float,
        side: str,
        order_type: str = "limit_order",
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place order with optional bracket Stop Loss and Take Profit."""
        payload: Dict[str, Any] = {
            "product_id": product_id,
            "size": int(round(size)) if size >= 1 else round(size, 4),
            "side": side.lower(),
            "order_type": order_type,
        }
        if price is not None:
            payload["limit_price"] = str(round(price, 2))
        if client_order_id:
            payload["client_order_id"] = client_order_id

        # Attach bracket parameters if specified
        if stop_loss_price is not None:
            payload["bracket_stop_loss_price"] = str(round(stop_loss_price, 2))
            payload["bracket_stop_trigger_method"] = "mark_price"
        if take_profit_price is not None:
            payload["bracket_take_profit_price"] = str(round(take_profit_price, 2))

        return self._request("POST", "/v2/orders", data=payload, auth=True)

    def cancel_order(self, product_id: int, order_id: str) -> Dict[str, Any]:
        """Cancel a single order by order_id."""
        data = {"product_id": product_id, "id": order_id}
        return self._request("DELETE", "/v2/orders", data=data, auth=True)

    def cancel_all_orders(self, product_id: Optional[int] = None) -> Dict[str, Any]:
        """Cancel all open orders, optionally scoped to product_id."""
        payload = {}
        if product_id is not None:
            payload["product_id"] = product_id
        return self._request("DELETE", "/v2/orders/all", data=payload, auth=True)


class DeltaMarketData(MarketData):
    """Production MarketData adapter that fetches live candles from Delta Exchange
    and automatically persists/caches them into PostgreSQL / SQLite storage.
    """

    def __init__(self, client: Optional[DeltaClient] = None, store: Optional[Any] = None):
        self.client = client or DeltaClient()
        self.store = store
        self.last_fetch_live = False
        self._live_resolutions: dict[str, bool] = {}

    def _normalize_symbol(self, symbol: str) -> str:
        s = (symbol or settings.SYMBOL).upper().replace("/", "").replace("-", "")
        if s.endswith("PERP"):
            s = s[:-4]
        return s

    def get_candles(self, symbol: str, resolution: str, limit: int = 100) -> List[Candle]:
        norm_sym = self._normalize_symbol(symbol)
        candles: List[Candle] = []
        self.last_fetch_live = False
        live_response = False

        # 1. Fetch live candles from Delta Exchange Testnet REST API
        try:
            candles = self.client.get_candles(symbol=norm_sym, resolution=resolution, limit=limit)
            live_response = bool(candles)
            self.last_fetch_live = live_response
            if candles and self.store and hasattr(self.store, "save_candles"):
                try:
                    self.store.save_candles(norm_sym, resolution, candles)
                    logger.debug("[exchange] Successfully persisted %d live Delta candles for %s (%s)", len(candles), norm_sym, resolution)
                except Exception as save_err:
                    logger.debug("[exchange] Could not persist live candles to store: %s", save_err)
        except Exception as exc:
            logger.warning("[exchange] Could not fetch live candles from Delta Exchange: %s", exc)

        self._live_resolutions[resolution] = live_response

        # 2. If Delta Exchange is temporarily unreachable, fall back to database store
        if not candles and self.store and hasattr(self.store, "get_candles"):
            stored = self.store.get_candles(norm_sym, resolution, limit=limit)
            if stored and len(stored) > 0:
                candles = [Candle(**item) for item in stored]
                logger.info("[exchange] Using %d cached candles from store for %s (%s)", len(candles), norm_sym, resolution)

        return candles

    def has_fresh_candles(self, resolution: str) -> bool:
        """True only if the requested resolution came from this live API fetch, not cache."""
        return self._live_resolutions.get(resolution, False)

"""FastAPI API routes for live trading dashboard and controls (Role C).

Endpoints (available under both /api/* and /*):
- GET  /status       - System health, account summary, and live status
- GET  /positions    - Open derivatives positions
- GET  /orders       - Active and recorded orders
- GET  /trades       - Executed trades log from SQLite/PostgreSQL
- GET  /signals      - Strategy signals & risk decisions log
- GET  /pnl          - Daily & cumulative PnL summary
- GET  /equity       - Historical equity curve snapshots
- GET  /risk         - Real-time risk metrics (drawdown, daily loss, halt state)
- GET  /price        - Real-time mark/ticker price
- GET  /strategy     - Strategy state and configuration
- GET  /logs         - Application/bot logs
- POST /kill         - Emergency kill switch: flattens positions and halts trading
- POST /pause        - Pauses new trade generation
- POST /resume       - Resumes trading operations
- POST /demo/trigger - Trigger a capped, paper-only order pipeline check
- WS   /ws           - Real-time bidirectional WebSocket stream for dashboard
- GET  /stream       - Server-Sent Events (SSE) live updates stream
"""
import os
import io
import asyncio
import json
import logging
import time
import uuid
import urllib.request
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd
from fastapi import APIRouter, Query, HTTPException, WebSocket, WebSocketDisconnect, File, UploadFile, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.models import Signal, RiskDecision, OrderRequest, OrderResult, Candle
from app.config import settings

logger = logging.getLogger("app.api.routes")
_UPLOAD_DATA_DIR = Path(__file__).resolve().parents[2] / "backtest" / "data" / "uploads"

router = APIRouter(tags=["trading"])

# Global operating mode: 'live' or 'uploaded'
_current_mode: str = "paper"
_BACKTEST_PAUSE_REASON = "Dashboard switched to Backtest Analysis"


class ModeRequest(BaseModel):
    mode: str


class BacktestRequest(BaseModel):
    execution_timeframe: str = "15m"
    trend_timeframe: str = "1h"
    symbol: Optional[str] = None

# Dependency references set by app.main at startup or via set_services
_services: Dict[str, Any] = {}
_live_market_price: Dict[str, Any] = {"feed_connected": False}


def set_live_market_price(data: Dict[str, Any]) -> None:
    """Store the latest public exchange ticker values for API and dashboard use."""
    _live_market_price.update(data)


def set_live_feed_connected(connected: bool) -> None:
    _live_market_price["feed_connected"] = bool(connected)


def _live_price_fields() -> Dict[str, Any]:
    received = float(_live_market_price.get("received_timestamp") or 0)
    age = max(0, round(time.time() - received)) if received else None
    return {
        "mark_price": _live_market_price.get("mark_price"),
        "spot_price": _live_market_price.get("spot_price"),
        "last_trade_price": _live_market_price.get("last_price"),
        "price_source": _live_market_price.get("source", "unavailable"),
        "price_type": ("MARK" if _live_market_price.get("mark_price") else
                       "LAST_TRADE" if _live_market_price.get("last_price") else "CANDLE_CLOSE"),
        "price_timestamp": _live_market_price.get("source_timestamp"),
        "price_received_timestamp": received or None,
        "price_age_seconds": age,
        "price_is_live": bool(received and age is not None and age <= 10),
        "price_feed_connected": bool(_live_market_price.get("feed_connected")),
    }


def _preferred_live_price() -> Optional[float]:
    price = _live_market_price.get("mark_price") or _live_market_price.get("last_price")
    received = float(_live_market_price.get("received_timestamp") or 0)
    if price and received and time.time() - received <= 30:
        return float(price)
    return None


def set_services(executor=None, risk_manager=None, store=None, market_data=None, strategy=None):
    """Register runtime services with the API router for dependency injection."""
    if executor is not None:
        _services["executor"] = executor
    if risk_manager is not None:
        _services["risk_manager"] = risk_manager
    if store is not None:
        _services["store"] = store
    if market_data is not None:
        _services["market_data"] = market_data
    if strategy is not None:
        _services["strategy"] = strategy


def get_service(name: str) -> Any:
    return _services.get(name)


class KillSwitchRequest(BaseModel):
    reason: Optional[str] = "Manual emergency kill switch triggered via API"


class DemoTriggerRequest(BaseModel):
    side: Optional[str] = "buy"
    price_offset_pct: Optional[float] = 0.0


class PriceOverrideRequest(BaseModel):
    price: float
    timeframe: Optional[str] = "1m"


# -----------------------------------------------------------------------------
# WebSocket Connection Manager
# -----------------------------------------------------------------------------

class ConnectionManager:
    """Manages active WebSocket connections for live dashboard streaming."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("[api.ws] Client connected (%d total)", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info("[api.ws] Client disconnected (%d remaining)", len(self.active_connections))

    async def broadcast(self, message: Dict[str, Any]):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


ws_manager = ConnectionManager()


def get_system_snapshot() -> Dict[str, Any]:
    """Helper compiling full system status snapshot for WebSocket/SSE."""
    executor = get_service("executor")
    risk_manager = get_service("risk_manager")
    store = get_service("store")
    market_data = get_service("market_data")

    account = executor.get_account_state() if executor else None
    is_halted = risk_manager.is_halted() if risk_manager else False
    halt_reason = (
        getattr(risk_manager, "halt_reason", "")
        or getattr(risk_manager, "_halt_reason", "")
        if risk_manager
        else ""
    )

    price = _preferred_live_price() or 0.0
    if store and hasattr(store, "get_latest_price"):
        db_p = store.get_latest_price(settings.SYMBOL)
        if db_p and db_p > 0:
            price = float(db_p)
    if price <= 0.0 and store and hasattr(store, "get_candles"):
        stored_c = store.get_candles(settings.SYMBOL, settings.TIMEFRAME, limit=1)
        if stored_c:
            price = float(stored_c[-1]["close"])
    if price <= 0.0 and market_data:
        candles = market_data.get_candles(settings.SYMBOL, settings.TIMEFRAME, limit=1)
        if candles:
            price = float(candles[-1].close)
    if price <= 0.0:
        price = 0.0

    trades = store.get_trades(limit=10) if store else []
    signals = store.get_signals(limit=10) if (store and hasattr(store, "get_signals")) else []
    curve = store.get_equity_curve(limit=20) if store else []

    peak = account.peak_equity if (account and account.peak_equity > 0) else 10000.0
    current = account.equity if account else 10000.0
    drawdown = max(0.0, round(((peak - current) / peak) * 100.0, 2))

    positions = account.open_positions if account else []
    notional = sum(p.size * price for p in positions)
    margin_used = notional / float(settings.MAX_LEVERAGE)
    cash = max(0.0, current - margin_used)
    available_margin = max(0.0, current - margin_used)

    orders = []
    if executor:
        if hasattr(executor, "get_open_orders"):
            orders = executor.get_open_orders()
        elif hasattr(executor, "orders"):
            raw_orders = getattr(executor, "orders", [])
            orders = [o.model_dump() if hasattr(o, "model_dump") else dict(o) for o in raw_orders]

    return {
        "status": "halted" if is_halted else "running",
        "equity": round(current, 2),
        "cash": round(cash, 2),
        "margin_used": round(margin_used, 2),
        "available_margin": round(available_margin, 2),
        "daily_pnl": round(account.daily_pnl, 2) if account else 0.0,
        "peak_equity": round(peak, 2),
        "is_halted": is_halted,
        "halt_reason": halt_reason,
        "drawdown_pct": drawdown,
        "price": round(price, 2),
        **_live_price_fields(),
        "symbol": settings.SYMBOL,
        "mode": _current_mode,
        "execution_mode": "paper",
        "open_positions": [p.model_dump() for p in positions],
        "open_orders": orders,
        "trades": trades,
        "signals": signals,
        "equity_curve": curve,
        "server_time": int(time.time()),
    }


# -----------------------------------------------------------------------------
# REST Endpoints
# -----------------------------------------------------------------------------

@router.get("/status")
def get_status() -> Dict[str, Any]:
    executor = get_service("executor")
    risk_manager = get_service("risk_manager")
    market_data = get_service("market_data")
    store = get_service("store")

    account = executor.get_account_state() if executor else None
    is_halted = risk_manager.is_halted() if risk_manager else False
    halt_reason = (
        getattr(risk_manager, "halt_reason", "")
        or getattr(risk_manager, "_halt_reason", "")
        if risk_manager
        else ""
    )

    current_price = _preferred_live_price() or 0.0
    if store and hasattr(store, "get_latest_price"):
        db_p = store.get_latest_price(settings.SYMBOL)
        if db_p and db_p > 0:
            current_price = float(db_p)
    if current_price <= 0.0 and store and hasattr(store, "get_candles"):
        stored_c = store.get_candles(settings.SYMBOL, settings.TIMEFRAME, limit=1)
        if stored_c:
            current_price = float(stored_c[-1]["close"])
    if current_price <= 0.0 and market_data:
        candles = market_data.get_candles(settings.SYMBOL, settings.TIMEFRAME, limit=1)
        if candles:
            current_price = float(candles[-1].close)
    if current_price <= 0.0:
        current_price = 0.0

    current = account.equity if account else 10000.0
    positions = account.open_positions if account else []
    notional = sum(p.size * current_price for p in positions)
    margin_used = notional / float(settings.MAX_LEVERAGE)
    cash = max(0.0, current - margin_used)

    return {
        "status": "halted" if is_halted else "running",
        "equity": round(current, 2),
        "cash": round(cash, 2),
        "margin_used": round(margin_used, 2),
        "daily_pnl": round(account.daily_pnl, 2) if account else 0.0,
        "peak_equity": round(account.peak_equity, 2) if account else 10000.0,
        "open_positions_count": len(positions),
        "is_halted": is_halted,
        "halt_reason": halt_reason,
        "symbol": settings.SYMBOL,
        "current_price": round(current_price, 2),
        **_live_price_fields(),
        "mode": _current_mode,
        "execution_mode": "paper",
        "demo_mode": settings.DEMO_MODE,
        "server_time": int(time.time()),
    }


@router.get("/positions")
def get_positions() -> List[Dict[str, Any]]:
    executor = get_service("executor")
    if not executor:
        return []
    account = executor.get_account_state()
    return [pos.model_dump() for pos in account.open_positions]


@router.get("/orders")
def get_orders() -> List[Dict[str, Any]]:
    executor = get_service("executor")
    if not executor:
        return []
    if hasattr(executor, "get_open_orders"):
        return executor.get_open_orders()
    if hasattr(executor, "orders"):
        raw_orders = getattr(executor, "orders", [])
        return [
            o.model_dump() if hasattr(o, "model_dump") else dict(o)
            for o in raw_orders
        ]
    return []


@router.get("/trades")
def get_trades(limit: int = Query(default=100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    store = get_service("store")
    if not store:
        return []
    return store.get_trades(limit=limit)


@router.get("/signals")
def get_signals(limit: int = Query(default=100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    store = get_service("store")
    if not store:
        return []
    if hasattr(store, "get_signals"):
        return store.get_signals(limit=limit)
    return getattr(store, "signals", [])[-limit:]


@router.get("/pnl")
def get_pnl() -> Dict[str, Any]:
    executor = get_service("executor")
    if not executor:
        return {"daily_pnl": 0.0, "total_pnl": 0.0, "unrealized_pnl": 0.0, "equity": 0.0, "peak_equity": 0.0}

    account = executor.get_account_state()
    unrealized = sum(p.unrealized_pnl for p in account.open_positions)
    return {
        "daily_pnl": round(account.daily_pnl, 2),
        "unrealized_pnl": round(unrealized, 2),
        "equity": round(account.equity, 2),
        "peak_equity": round(account.peak_equity, 2),
    }


@router.get("/equity")
def get_equity(limit: int = Query(default=1000, ge=1, le=5000)) -> List[Dict[str, Any]]:
    store = get_service("store")
    if not store:
        return []
    return store.get_equity_curve(limit=limit)


@router.get("/risk")
def get_risk_status() -> Dict[str, Any]:
    risk_manager = get_service("risk_manager")
    executor = get_service("executor")
    store = get_service("store")

    account = executor.get_account_state() if executor else None
    is_halted = risk_manager.is_halted() if risk_manager else False
    halt_reason = (
        getattr(risk_manager, "halt_reason", "")
        or getattr(risk_manager, "_halt_reason", "")
        if risk_manager
        else ""
    )

    peak = account.peak_equity if (account and account.peak_equity > 0) else 1.0
    current = account.equity if account else 1.0
    current_drawdown_pct = max(0.0, round(((peak - current) / peak) * 100.0, 2))
    daily_loss_used_pct = (
        round(abs(min(0.0, account.daily_pnl)) / max(1.0, current) * 100.0, 2)
        if account
        else 0.0
    )

    events = store.get_risk_events(limit=10) if (store and hasattr(store, "get_risk_events")) else []

    return {
        "halted": is_halted,
        "halt_reason": halt_reason,
        "daily_loss_used_pct": daily_loss_used_pct,
        "daily_loss_limit_pct": settings.DAILY_LOSS_LIMIT_PCT,
        "current_drawdown_pct": current_drawdown_pct,
        "max_drawdown_pct": settings.MAX_DRAWDOWN_PCT,
        "max_leverage": settings.MAX_LEVERAGE,
        "max_open_positions": settings.MAX_OPEN_POSITIONS,
        "max_notional_usd": settings.MAX_NOTIONAL_USD,
        "stale_data_seconds": settings.STALE_DATA_SECONDS,
        "recent_risk_events": events,
    }


@router.get("/price")
def get_price(symbol: Optional[str] = None) -> Dict[str, Any]:
    sym = symbol or settings.SYMBOL
    live = _preferred_live_price() if sym.upper() == settings.SYMBOL.upper() else None
    if live:
        return {"symbol": sym, "price": round(live, 2), "mark_price": _live_market_price.get("mark_price"),
                "spot_price": _live_market_price.get("spot_price"), "last_trade_price": _live_market_price.get("last_price"),
                "price_type": "MARK" if _live_market_price.get("mark_price") else "LAST_TRADE",
                **_live_price_fields()}
    store = get_service("store")
    market_data = get_service("market_data")
    latest = None
    if store and hasattr(store, "get_candles"):
        stored_c = store.get_candles(sym, settings.TIMEFRAME, limit=1)
        if stored_c:
            latest = stored_c[-1]
    if latest is None and market_data:
        candles = market_data.get_candles(sym, settings.TIMEFRAME, limit=1)
        if candles:
            latest = candles[-1].model_dump()
    if latest is None:
        return {"symbol": sym, "price": None, "timestamp": None, "price_source": "unavailable",
                "price_type": "unavailable", **_live_price_fields()}
    return {
        "symbol": sym,
        "price": latest.get("close", latest.get("price")),
        "open": latest.get("open"),
        "high": latest.get("high"),
        "low": latest.get("low"),
        "volume": latest.get("volume", 0.0),
        "timestamp": latest.get("ts", int(time.time())),
        "price_source": "stored_candles",
        "price_type": "CANDLE_CLOSE",
        **_live_price_fields(),
    }


def _normalize_symbol(sym: Optional[str]) -> str:
    s = (sym or settings.SYMBOL).upper().replace("/", "").replace("-", "").strip()
    if s.endswith("PERP"):
        s = s[:-4]
    return s or settings.SYMBOL


def _uploaded_candles_path(symbol: str, timeframe: str) -> Path:
    from app.strategy.trend import timeframe_delta
    timeframe_delta(timeframe)
    safe_symbol = re.sub(r"[^A-Z0-9]", "", _normalize_symbol(symbol))
    safe_tf = re.sub(r"[^a-z0-9]", "", timeframe.lower())
    if not safe_symbol or not safe_tf:
        raise ValueError("Invalid symbol or timeframe")
    return _UPLOAD_DATA_DIR / f"{safe_symbol}_{safe_tf}.json"


def _save_uploaded_candles(symbol: str, timeframe: str, candles: List[Candle]) -> None:
    path = _uploaded_candles_path(symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([c.model_dump() for c in candles]), encoding="utf-8")


def _read_uploaded_candles(symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    path = _uploaded_candles_path(symbol, timeframe)
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Uploaded dataset {path.name} is not a candle list")
    return raw


@router.get("/candles")
def get_candles(
    symbol: Optional[str] = None,
    resolution: Optional[str] = None,
    limit: int = Query(default=48, ge=1, le=2000),
    before_ts: Optional[int] = None,
    after_ts: Optional[int] = None,
    force_live: bool = Query(default=False),
) -> List[Dict[str, Any]]:
    """Fetch recent or historical OHLCV candles for charting and analysis.
    Supports force_live to actively pull fresh candles from Delta Exchange Testnet.
    """
    sym = _normalize_symbol(symbol)
    res = resolution or settings.TIMEFRAME
    store = get_service("store")
    market_data = get_service("market_data")

    # If force_live requested and market_data available, fetch from exchange directly
    if force_live and market_data:
        try:
            live = market_data.get_candles(sym, res, limit=limit)
            if live and (not hasattr(market_data, "last_fetch_live") or market_data.last_fetch_live):
                return [c.model_dump() for c in live]
            raise HTTPException(status_code=502, detail="Live candle feed is unavailable; cached candles were not returned as live data.")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("[api] Failed to get live candles: %s", e)
            raise HTTPException(status_code=502, detail="Live candle feed is unavailable.") from e

    if store and hasattr(store, "get_candles"):
        stored = store.get_candles(sym, res, limit=limit, before_ts=before_ts, after_ts=after_ts)
        if stored and len(stored) > 0:
            return stored

    if market_data:
        try:
            candles = market_data.get_candles(sym, res, limit=limit)
            return [c.model_dump() for c in candles]
        except Exception as e:
            logger.warning("[api] Failed to get candles: %s", e)
    return []


@router.post("/exchange/sync-candles")
async def sync_exchange_candles(
    symbol: str = Query(default=settings.SYMBOL),
    resolution: str = Query(default="15m"),
    limit: int = Query(default=50, ge=5, le=500),
) -> Dict[str, Any]:
    """Explicitly fetch current candles using the configured public Delta feed,
    persist them to algo_bot2 database, and calibrate the system mark price.
    """
    sym = _normalize_symbol(symbol)
    market_data = get_service("market_data")
    candles = market_data.get_candles(sym, resolution, limit=limit) if market_data else []

    if not candles:
        raise HTTPException(
            status_code=502,
            detail=f"Delta Exchange returned 0 candles for {sym} ({resolution}). Check the configured public market-data feed."
        )

    store = get_service("store")
    saved_count = 0
    if store and hasattr(store, "save_candles"):
        saved_count = store.save_candles(sym, resolution, candles)

    latest_c = candles[-1]
    await ws_manager.broadcast({
        "type": "candles_updated",
        "symbol": sym,
        "timeframe": resolution,
        "count": len(candles),
        "latest_price": latest_c.close,
        "latest_ts": latest_c.ts,
        "source": "Delta Exchange public market data"
    })

    return {
        "status": "ok",
        "source": "Delta Exchange public market data",
        "symbol": sym,
        "resolution": resolution,
        "count": len(candles),
        "saved_to_db": saved_count,
        "latest_price": latest_c.close,
        "latest_ts": latest_c.ts,
        "candles": [c.model_dump() for c in candles]
    }


@router.get("/candles/summary")
def get_candles_summary(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Get stored timeframes and count summary from algo_bot2 database."""
    sym = _normalize_symbol(symbol)
    store = get_service("store")
    if store and hasattr(store, "get_stored_timeframes"):
        return {"symbol": sym, "timeframes": store.get_stored_timeframes(sym)}
    return {"symbol": sym, "timeframes": []}


def _normalize_timestamp(val: Any) -> int:
    """Normalize various timestamp formats into integer epoch seconds."""
    if val is None:
        return int(time.time())
    if isinstance(val, (int, float)):
        num = int(val)
        if num > 1e17:
            return int(num // 10**9)
        if num > 1e14:
            return int(num // 10**6)
        if num > 1e11:
            return int(num // 10**3)
        return num
    if isinstance(val, str):
        try:
            num = int(float(val))
            if num > 1e17:
                return int(num // 10**9)
            if num > 1e14:
                return int(num // 10**6)
            if num > 1e11:
                return int(num // 10**3)
            return num
        except Exception:
            pass
        try:
            dt = pd.to_datetime(val)
            return int(dt.timestamp())
        except Exception:
            pass
    return int(time.time())


def _parse_json_candles(raw_data: Any, timeframe: str = "15m") -> List[Candle]:
    """Universal parser for Bitcoin JSON datasets supporting array of dicts, Delta/exchange wrappers, arrays-of-arrays, and single price objects."""
    if isinstance(raw_data, dict):
        for candidate in ["candles", "result", "data", "bars", "history", "items"]:
            if candidate in raw_data and isinstance(raw_data[candidate], list):
                raw_data = raw_data[candidate]
                break
        else:
            p = raw_data.get("price") or raw_data.get("close") or raw_data.get("c") or raw_data.get("mark_price")
            if p is not None:
                o = float(raw_data.get("open", raw_data.get("o", p)))
                h = float(raw_data.get("high", raw_data.get("h", max(o, float(p)))))
                l = float(raw_data.get("low", raw_data.get("l", min(o, float(p)))))
                c = float(p)
                v = float(raw_data.get("volume", raw_data.get("v", 1.0)))
                raw_ts = raw_data.get("ts", raw_data.get("time", raw_data.get("t", int(time.time()))))
                norm_ts = _normalize_timestamp(raw_ts)
                return [Candle(ts=norm_ts, open=o, high=h, low=l, close=c, volume=v)]
            raise ValueError("Expected JSON array of candles or dictionary containing candle list")

    if not isinstance(raw_data, list):
        raise ValueError("Expected JSON list of candle records or wrapper object")

    if len(raw_data) == 0:
        return []

    candles: List[Candle] = []
    first = raw_data[0]

    # Case A: Array of Arrays [[ts, open, high, low, close, volume], ...]
    if isinstance(first, (list, tuple)):
        for item in raw_data:
            if len(item) >= 5:
                norm_ts = _normalize_timestamp(item[0])
                o = float(item[1])
                h = float(item[2])
                l = float(item[3])
                c = float(item[4])
                v = float(item[5]) if len(item) > 5 else 1.0
                candles.append(Candle(ts=norm_ts, open=o, high=h, low=l, close=c, volume=v))
    else:
        # Case B: Array of Dicts
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            c_map = {str(k).lower().strip(): v for k, v in item.items()}

            o = c_map.get("open", c_map.get("o", c_map.get("open_price", None)))
            h = c_map.get("high", c_map.get("h", c_map.get("high_price", None)))
            l = c_map.get("low", c_map.get("l", c_map.get("low_price", None)))
            c = c_map.get("close", c_map.get("c", c_map.get("close_price", c_map.get("price", None))))
            v = c_map.get("volume", c_map.get("v", c_map.get("vol", c_map.get("size", 1.0))))
            t = c_map.get("ts", c_map.get("time", c_map.get("timestamp", c_map.get("datetime", c_map.get("t", c_map.get("date", None))))))

            if c is None and o is not None:
                c = o
            if o is None and c is not None:
                o = c
            if o is None or c is None:
                continue

            o_val = float(o)
            c_val = float(c)
            h_val = float(h) if h is not None else max(o_val, c_val)
            l_val = float(l) if l is not None else min(o_val, c_val)
            v_val = float(v) if v is not None else 1.0
            norm_ts = _normalize_timestamp(t)

            candles.append(Candle(ts=norm_ts, open=o_val, high=h_val, low=l_val, close=c_val, volume=v_val))

    if len(candles) > 1 and len(set(c.ts for c in candles)) == 1:
        step = 60 if timeframe == "1m" else 300 if timeframe == "5m" else 3600 if timeframe == "1h" else 900
        now = int(time.time())
        for i, c in enumerate(candles):
            c.ts = now - (len(candles) - 1 - i) * step

    return candles


@router.post("/candles/upload")
async def upload_candles(
    file: UploadFile = File(...),
    timeframe: str = Query(default="15m"),
    symbol: str = Query(default=settings.SYMBOL),
) -> Dict[str, Any]:
    """Upload Bitcoin candle dataset (.csv, .parquet, .json) into PostgreSQL algo_bot2."""
    store = get_service("store")
    if not store or not hasattr(store, "save_candles"):
        raise HTTPException(status_code=500, detail="Database store unavailable")

    contents = await file.read()
    filename = (file.filename or "").lower()

    candles: List[Candle] = []
    if filename.endswith(".json") or file.content_type == "application/json":
        try:
            raw_json = json.loads(contents.decode("utf-8"))
            candles = _parse_json_candles(raw_json, timeframe)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse JSON file: {e}")
    else:
        try:
            if filename.endswith(".parquet"):
                df = pd.read_parquet(io.BytesIO(contents))
            else:
                df = pd.read_csv(io.BytesIO(contents))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse uploaded file: {e}")

        col_map = {str(c).lower().strip(): c for c in df.columns}
        ts_col = None
        for candidate in ["ts", "time", "timestamp", "datetime", "date", "t"]:
            if candidate in col_map:
                ts_col = col_map[candidate]
                break

        o_col = col_map.get("open", col_map.get("o", col_map.get("open_price", None)))
        h_col = col_map.get("high", col_map.get("h", col_map.get("high_price", None)))
        l_col = col_map.get("low", col_map.get("l", col_map.get("low_price", None)))
        c_col = col_map.get("close", col_map.get("c", col_map.get("close_price", col_map.get("price", None))))
        v_col = col_map.get("volume", col_map.get("v", col_map.get("vol", col_map.get("size", None))))

        if not o_col and not c_col:
            raise HTTPException(status_code=400, detail="Uploaded file missing required price columns (open/close or o/c)")

        if not o_col: o_col = c_col
        if not c_col: c_col = o_col
        if not h_col: h_col = o_col
        if not l_col: l_col = c_col

        ts_vals = _extract_epoch_seconds_from_df(df, ts_col, timeframe)
        o_vals = df[o_col].astype(float).values
        h_vals = df[h_col].astype(float).values
        l_vals = df[l_col].astype(float).values
        c_vals = df[c_col].astype(float).values
        v_vals = df[v_col].astype(float).values if v_col else [1.0] * len(df)
        candles = [
            Candle(
                ts=int(ts_vals[i]),
                open=float(o_vals[i]),
                high=float(h_vals[i]),
                low=float(l_vals[i]),
                close=float(c_vals[i]),
                volume=float(v_vals[i]),
            )
            for i in range(len(df))
        ]

    if not candles:
        raise HTTPException(status_code=400, detail="No valid candles found in uploaded file")

    norm_sym = _normalize_symbol(symbol)
    try:
        _save_uploaded_candles(norm_sym, timeframe, candles)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved_count = store.save_candles(norm_sym, timeframe, candles)
    latest_price = candles[-1].close if candles else None
    latest_ts = candles[-1].ts if candles else None

    await ws_manager.broadcast({
        "type": "candles_updated",
        "symbol": norm_sym,
        "timeframe": timeframe,
        "count": saved_count,
        "latest_price": latest_price,
        "latest_ts": latest_ts,
    })

    return {
        "status": "ok",
        "message": f"Successfully stored {saved_count} {timeframe} candles for {norm_sym} in PostgreSQL algo_bot2",
        "count": saved_count,
        "symbol": norm_sym,
        "timeframe": timeframe,
        "latest_price": round(latest_price, 2) if latest_price else None,
        "latest_ts": latest_ts,
    }


@router.post("/candles/upload-json")
async def upload_candles_json(
    payload: Any = Body(...),
    timeframe: str = Query(default="15m"),
    symbol: str = Query(default=settings.SYMBOL),
) -> Dict[str, Any]:
    """Upload or paste raw Bitcoin candle JSON data directly into PostgreSQL algo_bot2."""
    store = get_service("store")
    if not store or not hasattr(store, "save_candles"):
        raise HTTPException(status_code=500, detail="Database store unavailable")

    tf = timeframe
    sym = _normalize_symbol(symbol)
    data_to_parse = payload
    if isinstance(payload, dict):
        if "timeframe" in payload and isinstance(payload["timeframe"], str):
            tf = payload["timeframe"]
        if "symbol" in payload and isinstance(payload["symbol"], str):
            sym = _normalize_symbol(payload["symbol"])
        if "candles" in payload:
            data_to_parse = payload["candles"]
        elif "data" in payload:
            data_to_parse = payload["data"]

    try:
        candles = _parse_json_candles(data_to_parse, tf)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse JSON candle data: {e}")

    if not candles:
        raise HTTPException(status_code=400, detail="No valid candles found in JSON payload")

    try:
        _save_uploaded_candles(sym, tf, candles)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved_count = store.save_candles(sym, tf, candles)
    latest_price = candles[-1].close if candles else None
    latest_ts = candles[-1].ts if candles else None

    await ws_manager.broadcast({
        "type": "candles_updated",
        "symbol": sym,
        "timeframe": tf,
        "count": saved_count,
        "latest_price": latest_price,
        "latest_ts": latest_ts,
    })

    return {
        "status": "ok",
        "message": f"Successfully ingested {saved_count} {tf} candles for {sym} into algo_bot2",
        "count": saved_count,
        "symbol": sym,
        "timeframe": tf,
        "latest_price": round(latest_price, 2) if latest_price else None,
        "latest_ts": latest_ts,
    }


@router.post("/price/sync")
async def sync_live_market_price(symbol: str = Query(default=settings.SYMBOL)) -> Dict[str, Any]:
    """Fetch live real-world Bitcoin mark price from public exchange tickers (Coinbase / Binance)
    and synchronize system price and store a fresh 1m bar in algo_bot2.
    """
    store = get_service("store")
    live_price = None
    source = None

    try:
        req = urllib.request.Request(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=4) as res:
            data = json.loads(res.read().decode("utf-8"))
            if "data" in data and "amount" in data["data"]:
                live_price = float(data["data"]["amount"])
                source = "Coinbase Spot"
    except Exception:
        pass

    if not live_price:
        try:
            req = urllib.request.Request(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=4) as res:
                data = json.loads(res.read().decode("utf-8"))
                if "price" in data:
                    live_price = float(data["price"])
                    source = "Binance Ticker"
        except Exception:
            pass

    if not live_price:
        raise HTTPException(status_code=503, detail="Unable to fetch live ticker from public market exchanges")

    now_ts = int(time.time())
    new_candle = Candle(
        ts=now_ts,
        open=live_price,
        high=live_price,
        low=live_price,
        close=live_price,
        volume=1.0,
    )

    if store and hasattr(store, "save_candles"):
        store.save_candles(symbol, "1m", [new_candle])

    await ws_manager.broadcast({
        "type": "price_update",
        "symbol": symbol,
        "price": live_price,
        "source": source,
        "ts": now_ts,
    })

    return {
        "status": "ok",
        "symbol": symbol,
        "price": round(live_price, 2),
        "source": source,
        "ts": now_ts,
    }


@router.post("/price/override")
async def override_market_price(payload: PriceOverrideRequest) -> Dict[str, Any]:
    """Manually set exact Bitcoin price into algo_bot2 database."""
    store = get_service("store")
    if payload.price <= 0:
        raise HTTPException(status_code=400, detail="Price must be positive")

    now_ts = int(time.time())
    candle = Candle(
        ts=now_ts,
        open=payload.price,
        high=payload.price,
        low=payload.price,
        close=payload.price,
        volume=1.0,
    )
    if store and hasattr(store, "save_candles"):
        store.save_candles(settings.SYMBOL, payload.timeframe or "1m", [candle])

    await ws_manager.broadcast({
        "type": "price_update",
        "symbol": settings.SYMBOL,
        "price": payload.price,
        "source": "manual_override",
        "ts": now_ts,
    })
    return {
        "status": "ok",
        "price": payload.price,
        "symbol": settings.SYMBOL,
        "ts": now_ts,
    }


def _extract_epoch_seconds_from_df(df: pd.DataFrame, ts_col: Optional[str] = None, timeframe: str = "15m") -> List[int]:
    """Helper to reliably extract integer epoch seconds regardless of pandas datetime resolution/timezone."""
    if ts_col is not None and ts_col in df.columns:
        s = df[ts_col]
        if pd.api.types.is_datetime64_any_dtype(s):
            return [int(t.timestamp()) for t in s]
        if pd.api.types.is_numeric_dtype(s):
            res = []
            for v in s.values:
                try:
                    num = int(v)
                    if num > 1e17:
                        num = int(num // 10**9)
                    elif num > 1e14:
                        num = int(num // 10**6)
                    elif num > 1e11:
                        num = int(num // 10**3)
                    res.append(num)
                except Exception:
                    res.append(int(time.time()))
            return res
        try:
            numeric_s = pd.to_numeric(s, errors="coerce")
            if not numeric_s.isna().all():
                res = []
                for v in numeric_s.values:
                    num = int(v)
                    if num > 1e17:
                        num = int(num // 10**9)
                    elif num > 1e14:
                        num = int(num // 10**6)
                    elif num > 1e11:
                        num = int(num // 10**3)
                    res.append(num)
                return res
        except Exception:
            pass
        try:
            converted = pd.to_datetime(s, errors="coerce")
            if not converted.isna().all():
                return [int(t.timestamp()) if pd.notna(t) else 0 for t in converted]
        except Exception:
            pass
    elif isinstance(df.index, pd.DatetimeIndex):
        return [int(t.timestamp()) for t in df.index]
    elif "ts" in df.columns:
        return _extract_epoch_seconds_from_df(df, "ts", timeframe)
    else:
        now = int(time.time())
        step = 60 if timeframe == "1m" else 300 if timeframe == "5m" else 3600 if timeframe == "1h" else 900
        return [now - (len(df) - 1 - i) * step for i in range(len(df))]


@router.post("/candles/load-local")
async def load_local_cache_dataset(
    timeframe: str = Query(default="15m"),
    limit: int = Query(default=1000)
) -> Dict[str, Any]:
    """1-Click load real Bitcoin candles from local parquet cache files into algo_bot2."""
    store = get_service("store")
    if not store or not hasattr(store, "save_candles"):
        raise HTTPException(status_code=500, detail="Database store unavailable")

    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    if timeframe == "1m":
        file_path = os.path.join(base_dir, "crypto_BTC_USD_1m.parquet")
    elif timeframe == "1h":
        file_path = os.path.join(base_dir, "backtest", "cache", "1h.parquet")
    else:
        file_path = os.path.join(base_dir, "backtest", "cache", "15min.parquet")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Local cache file not found: {file_path}")

    df = pd.read_parquet(file_path).tail(limit)
    ts_vals = _extract_epoch_seconds_from_df(df, timeframe=timeframe)
    candles = [
        Candle(
            ts=int(ts_vals[i]),
            open=float(df["open"].iloc[i]),
            high=float(df["high"].iloc[i]),
            low=float(df["low"].iloc[i]),
            close=float(df["close"].iloc[i]),
            volume=float(df["volume"].iloc[i]),
        )
        for i in range(len(df))
    ]

    saved_count = store.save_candles(settings.SYMBOL, timeframe, candles)
    await ws_manager.broadcast({
        "type": "candles_updated",
        "symbol": settings.SYMBOL,
        "timeframe": timeframe,
        "count": saved_count
    })
    return {
        "status": "ok",
        "message": f"Loaded {saved_count} candles into algo_bot2 for {timeframe}",
        "count": saved_count,
        "timeframe": timeframe
    }


@router.get("/mode")
def get_mode() -> Dict[str, Any]:
    return {"mode": _current_mode, "execution_mode": "paper", "exchange_orders_enabled": False,
            "symbol": settings.SYMBOL, "timeframe": settings.TIMEFRAME}


@router.post("/mode")
async def set_mode(payload: ModeRequest) -> Dict[str, Any]:
    global _current_mode
    if payload.mode not in ("paper", "uploaded", "backtest"):
        raise HTTPException(status_code=400, detail="Mode must be 'paper' or 'backtest'")
    target_mode = "uploaded" if payload.mode in ("uploaded", "backtest") else "paper"
    risk_manager = get_service("risk_manager")
    if target_mode != _current_mode and risk_manager:
        if target_mode == "uploaded" and not risk_manager.is_halted():
            risk_manager.halt(_BACKTEST_PAUSE_REASON)
        elif target_mode == "paper" and getattr(risk_manager, "halt_reason", "") == _BACKTEST_PAUSE_REASON:
            risk_manager.resume()
    _current_mode = target_mode
    await ws_manager.broadcast({"type": "mode_changed", "mode": _current_mode})
    return {"status": "ok", "mode": _current_mode, "execution_mode": "paper",
            "exchange_orders_enabled": False}


@router.get("/paper/summary")
def get_paper_summary() -> Dict[str, Any]:
    executor = get_service("executor")
    if not executor or not hasattr(executor, "summary"):
        raise HTTPException(status_code=503, detail="Paper executor is unavailable")
    return executor.summary()


def _candle_rows_to_frame(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    frame = pd.DataFrame(rows)
    if "ts" not in frame or not {"open", "high", "low", "close", "volume"}.issubset(frame.columns):
        raise ValueError("Uploaded candles must have ts, open, high, low, close, and volume fields")
    ts = pd.to_numeric(frame.pop("ts"), errors="raise")
    if ts.abs().median() > 1e11:
        ts = ts / 1000
    frame.index = pd.to_datetime(ts, unit="s", utc=True)
    frame.index.name = "ts"
    frame = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="raise")
    return frame[~frame.index.duplicated(keep="last")].sort_index()


@router.post("/backtest/run")
def run_backtest_endpoint(payload: BacktestRequest = Body(default=BacktestRequest())) -> Dict[str, Any]:
    """Run against uploaded execution/trend candles, or bundled BTC data by default."""
    from app.strategy.trend import compute_signals, PRODUCTION_PARAMS
    from backtest.data import load_backtest_data
    from backtest.engine import run_backtest
    from backtest.run import RISK_CFG
    from backtest.metrics import compute_metrics

    execution_tf = payload.execution_timeframe.strip().lower()
    trend_tf = payload.trend_timeframe.strip().lower()
    symbol = _normalize_symbol(payload.symbol)
    uploaded_exec, uploaded_trend = [], []
    try:
        uploaded_exec = _read_uploaded_candles(symbol, execution_tf)
        uploaded_trend = _read_uploaded_candles(symbol, trend_tf)
        if uploaded_exec or uploaded_trend:
            if not uploaded_exec or not uploaded_trend:
                missing = trend_tf if not uploaded_trend else execution_tf
                raise HTTPException(status_code=400, detail=f"Upload {missing} candles for {symbol} before running this timeframe pair.")
            df_exec = _candle_rows_to_frame(uploaded_exec)
            df_trend = _candle_rows_to_frame(uploaded_trend)
            start = max(df_exec.index.min(), df_trend.index.min())
            end = min(df_exec.index.max(), df_trend.index.max())
            df_exec = df_exec.loc[(df_exec.index >= start) & (df_exec.index <= end)]
            df_trend = df_trend.loc[(df_trend.index >= start) & (df_trend.index <= end)]
            if len(df_exec) < 100 or len(df_trend) < PRODUCTION_PARAMS.htf_ema + 1:
                raise HTTPException(status_code=400, detail=f"The shared date range needs at least 100 execution candles and {PRODUCTION_PARAMS.htf_ema + 1} trend candles for indicator warmup.")
            funding = pd.DataFrame({"rate": []}, index=pd.DatetimeIndex([], tz="UTC"))
            mark = df_exec[["open", "high", "low", "close", "volume"]]
            data_source = "uploaded"
        elif execution_tf == "15m" and trend_tf == "1h":
            df_exec, df_trend, funding, mark = load_backtest_data()
            data_source = "bundled"
        else:
            raise HTTPException(status_code=400, detail=f"No uploaded {execution_tf} and {trend_tf} candles found for {symbol}. Upload both timeframes first.")
        from app.strategy.trend import timeframe_delta
        timeframe_delta(execution_tf)
        timeframe_delta(trend_tf)
    except HTTPException:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Backtest dataset could not be loaded: {exc}")

    sig_df = compute_signals(df_exec, df_trend, PRODUCTION_PARAMS, htf_timeframe=trend_tf)
    # Match `python -m backtest.run` risk_managed configuration exactly.
    cfg = RISK_CFG
    trades_df, equity_df, state = run_backtest(
        sig_df, cfg, mark_prices=mark, funding=funding["rate"]
    )
    metrics = compute_metrics(trades_df, equity_df, state, benchmark=sig_df["close"])
    metrics["total_trades"] = int(len(trades_df))
    metrics["net_profit"] = round(float(state["final_equity"] - state["start_equity"]), 2)
    metrics["total_funding_pnl"] = round(float(trades_df.get("funding_pnl", pd.Series(dtype=float)).sum()), 2)

    trades_list = []
    if not trades_df.empty:
        for _, t in trades_df.iterrows():
            entry_p = float(t["entry"]) if "entry" in t else 0.0
            exit_p = float(t["exit"]) if "exit" in t else 0.0
            pnl_val = float(t["pnl"]) if "pnl" in t else 0.0
            is_short = str(t.get("side", "")).upper() in ("SELL", "SHORT")
            raw_pct = ((exit_p - entry_p) / entry_p * 100) if entry_p > 0 else 0.0
            pnl_pct = round(-raw_pct if is_short else raw_pct, 2)

            trades_list.append({
                "entry_ts": int(t["entry_ts"].timestamp()) if hasattr(t["entry_ts"], "timestamp") else int(t["entry_ts"]),
                "exit_ts": int(t["exit_ts"].timestamp()) if hasattr(t["exit_ts"], "timestamp") else int(t["exit_ts"]),
                "side": str(t.get("side", "BUY")).upper(),
                "entry": round(entry_p, 2),
                "exit": round(exit_p, 2),
                "entry_price": round(entry_p, 2),
                "exit_price": round(exit_p, 2),
                "size": round(float(t.get("size", 0.1)), 4),
                "pnl": round(pnl_val, 2),
                "funding_pnl": round(float(t.get("funding_pnl", 0.0)), 2),
                "pnl_pct": pnl_pct,
                "reason": str(t.get("reason", "Trend following strategy signal")),
                "exit_reason": str(t.get("exit_reason", "Target reached")),
            })

    eq_list = []
    if not equity_df.empty:
        for idx, row in equity_df.tail(100).iterrows():
            ts_val = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
            eq_list.append({"ts": ts_val, "equity": round(float(row["equity"]), 2)})

    res = {
        "status": "ok",
        "metrics": metrics,
        "trades": trades_list,
        "equity_curve": eq_list,
        "bars_analyzed": len(sig_df),
        "data_source": data_source,
        "execution_timeframe": execution_tf,
        "trend_timeframe": trend_tf,
        "symbol": symbol,
    }
    return res


@router.get("/backtest/candles")
def get_backtest_candles(
    resolution: str = Query(default="15m"),
    symbol: Optional[str] = None,
    source: Optional[str] = None,
    # A full year of 1m BTC candles is about 526k rows; keep the endpoint able
    # to return the same uploaded range that the backtest itself analyzes.
    limit: int = Query(default=72, ge=1, le=1000000),
    before_ts: Optional[int] = None,
    after_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return stored uploaded candles (or bundled defaults) for charting backtests."""
    from backtest.data import load_backtest_data

    sym = _normalize_symbol(symbol)
    if source != "bundled":
        try:
            uploaded = _read_uploaded_candles(sym, resolution.lower())
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Uploaded candles could not be loaded: {exc}") from exc
        if uploaded:
            if before_ts is not None:
                uploaded = [c for c in uploaded if int(c["ts"]) < before_ts]
            if after_ts is not None:
                uploaded = [c for c in uploaded if int(c["ts"]) > after_ts]
            return uploaded[-limit:]
        if source == "uploaded":
            return []

    try:
        df15, df1h, _, _ = load_backtest_data()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Backtest dataset could not be loaded: {exc}")

    normalized = resolution.lower()
    frame = df1h if normalized in ("1h", "60", "60m") else df15
    if before_ts is not None:
        frame = frame[frame.index < pd.to_datetime(before_ts, unit="s", utc=True)]
    if after_ts is not None:
        frame = frame[frame.index > pd.to_datetime(after_ts, unit="s", utc=True)]
    frame = frame.tail(limit)
    rows = frame.reset_index().to_dict(orient="records")
    return [{"ts": int(row["ts"].timestamp()), **{k: float(v) for k, v in row.items() if k != "ts"}}
            for row in rows]


@router.get("/strategy")
def get_strategy_state() -> Dict[str, Any]:
    strategy = get_service("strategy")
    risk_manager = get_service("risk_manager")
    is_halted = risk_manager.is_halted() if risk_manager else False

    name = strategy.__class__.__name__ if strategy else "TrendFollowingStrategy"
    params = getattr(strategy, "p", None)
    return {
        "name": name,
        "symbol": settings.SYMBOL,
        "timeframe": settings.TIMEFRAME,
        "htf_timeframe": settings.HTF_TIMEFRAME,
        "status": "halted" if is_halted else "active",
        "description": (
            "Shared regime strategy: ADX trend-following or RSI/Bollinger mean reversion "
            f"on BTCUSD {settings.TIMEFRAME}, with {settings.HTF_TIMEFRAME} data alignment"
        ),
        "parameters": params.__dict__ if params else {},
    }


@router.get("/logs")
def get_logs(limit: int = Query(default=100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    store = get_service("store")
    if not store:
        return []
    if hasattr(store, "get_logs"):
        return store.get_logs(limit=limit)
    return []


@router.post("/kill")
async def trigger_kill_switch(payload: Optional[KillSwitchRequest] = None) -> Dict[str, Any]:
    reason = payload.reason if payload and payload.reason else "Manual emergency kill switch triggered via API"
    logger.warning("[api] EMERGENCY KILL SWITCH TRIGGERED: %s", reason)

    risk_manager = get_service("risk_manager")
    executor = get_service("executor")
    store = get_service("store")

    if risk_manager:
        risk_manager.halt(reason)

    if executor:
        executor.flatten_all()

    if store and hasattr(store, "save_risk_event"):
        store.save_risk_event("KILL_SWITCH", reason)

    response = {
        "status": "killed",
        "message": "Positions flattened and trading halted",
        "reason": reason,
        "timestamp": int(time.time()),
    }
    await ws_manager.broadcast({"type": "kill_switch", "data": response})
    return response


@router.post("/positions/flatten")
async def flatten_positions() -> Dict[str, Any]:
    """1-Click Flatten: close open positions at market without halting the engine."""
    logger.info("[api] 1-Click Flatten requested via API")
    executor = get_service("executor")
    store = get_service("store")

    if executor:
        executor.flatten_all()

    if store and hasattr(store, "save_risk_event"):
        store.save_risk_event("FLATTEN", "1-Click market flatten triggered via API")

    response = {
        "status": "flattened",
        "message": "All open positions flattened at market price",
        "timestamp": int(time.time()),
    }
    await ws_manager.broadcast({"type": "flatten", "data": response})
    return response


@router.post("/pause")
async def pause_trading() -> Dict[str, Any]:
    reason = "Trading paused via API"
    logger.info("[api] %s", reason)

    risk_manager = get_service("risk_manager")
    store = get_service("store")

    if risk_manager:
        risk_manager.halt(reason)

    if store and hasattr(store, "save_risk_event"):
        store.save_risk_event("PAUSE", reason)

    response = {
        "status": "paused",
        "timestamp": int(time.time()),
    }
    await ws_manager.broadcast({"type": "pause", "data": response})
    return response


@router.post("/resume")
async def resume_trading() -> Dict[str, Any]:
    logger.info("[api] Resuming trading operations via API")

    risk_manager = get_service("risk_manager")
    store = get_service("store")

    if risk_manager:
        risk_manager.resume()

    if store and hasattr(store, "save_risk_event"):
        store.save_risk_event("RESUME", "Trading resumed via API")

    response = {
        "status": "resumed",
        "timestamp": int(time.time()),
    }
    await ws_manager.broadcast({"type": "resume", "data": response})
    return response


@router.post("/demo/trigger")
async def trigger_demo_signal(payload: Optional[DemoTriggerRequest] = None) -> Dict[str, Any]:
    """Manually check the paper order pipeline with a fresh market price.

    This endpoint is deliberately restricted to PaperExecutor and caps the
    test order's notional value. It never submits an order to Delta Exchange.
    """
    risk_manager = get_service("risk_manager")
    executor = get_service("executor")
    store = get_service("store")

    from app.execution.paper import PaperExecutor
    if not isinstance(executor, PaperExecutor):
        raise HTTPException(status_code=409, detail="Paper test orders are disabled for non-paper executors")

    ref_price = float(getattr(executor, "last_price", 0.0) or 0.0)
    price_ts = int(getattr(executor, "last_data_ts", 0) or 0)
    price_age = max(0, int(time.time()) - price_ts) if price_ts else None
    if ref_price <= 0 or price_age is None or price_age > settings.STALE_DATA_SECONDS:
        raise HTTPException(status_code=503, detail="No fresh market price yet; wait for the live feed or a newly closed candle")
    if risk_manager and risk_manager.is_halted():
        raise HTTPException(status_code=409, detail="Paper trading is halted; resume it before sending a test order")

    side = (payload.side if payload and payload.side else "buy").lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")

    if payload and payload.price_offset_pct:
        ref_price *= (1.0 + payload.price_offset_pct / 100.0)

    now = int(time.time())
    if side == "buy":
        entry = round(ref_price, 2)
        sl = round(entry * 0.985, 2)
        tp = round(entry * 1.03, 2)
    else:
        entry = round(ref_price, 2)
        sl = round(entry * 1.015, 2)
        tp = round(entry * 0.97, 2)

    signal = Signal(
        symbol=settings.SYMBOL,
        side=side,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        reason=f"Demo triggered {side.upper()} test signal @ ${entry:,.2f}",
        ts=now,
    )

    account = executor.get_account_state() if executor else None
    if not account:
        raise HTTPException(status_code=500, detail="Executor account state unavailable")

    if risk_manager and hasattr(risk_manager, "record_data_received"):
        risk_manager.record_data_received()

    decision = (
        risk_manager.evaluate(signal, account)
        if risk_manager
        else RiskDecision(approved=False, reason="No risk manager")
    )

    # Keep the diagnostic harmless while still exercising risk sizing and the
    # actual PaperExecutor path.
    test_size = min(decision.size, settings.PAPER_TEST_MAX_NOTIONAL_USD / entry) if decision.approved else 0.0
    if decision.approved and test_size <= 0:
        decision = RiskDecision(approved=False, reason="Paper test order size is below the minimum")
    elif decision.approved and test_size < decision.size:
        decision = decision.model_copy(update={
            "size": test_size,
            "reason": f"{decision.reason}; capped at ${settings.PAPER_TEST_MAX_NOTIONAL_USD:.2f} test notional",
        })

    if store:
        store.save_signal(signal, decision)

    order_result = OrderResult(ok=False, status="NOT_EXECUTED", error=decision.reason)
    order_req = None
    if decision.approved:
        order_req = OrderRequest(
            symbol=signal.symbol,
            side=signal.side,
            size=decision.size,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            client_order_id=f"demo-{uuid.uuid4().hex[:8]}",
        )
        order_result = executor.execute(order_req)
        if store:
            store.save_trade(order_req, order_result, reason=signal.reason)
            if hasattr(store, "save_equity"):
                store.save_equity(now, executor.get_account_state().equity)

    result_data = {
        "signal": signal.model_dump(),
        "decision": decision.model_dump(),
        "order": order_req.model_dump() if order_req else None,
        "result": order_result.model_dump() if order_result else None,
        "price_source": getattr(executor, "last_price_source", "unknown"),
        "price_age_seconds": price_age,
        "test_notional_cap_usd": settings.PAPER_TEST_MAX_NOTIONAL_USD,
        "paper_only": True,
    }
    await ws_manager.broadcast({"type": "demo_trade", "data": result_data})
    return result_data


# -----------------------------------------------------------------------------
# WebSocket & SSE Streaming
# -----------------------------------------------------------------------------

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Bidirectional WebSocket for live dashboard data and control signals."""
    await ws_manager.connect(websocket)
    try:
        # Send initial snapshot
        snapshot = get_system_snapshot()
        await websocket.send_json({"type": "snapshot", "data": snapshot})

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")
                if action == "ping":
                    await websocket.send_json({"type": "pong", "ts": int(time.time())})
                elif action == "snapshot":
                    snapshot = get_system_snapshot()
                    await websocket.send_json({"type": "snapshot", "data": snapshot})
                elif action == "kill":
                    reason = msg.get("reason", "Kill switch via WebSocket")
                    rm = get_service("risk_manager")
                    ex = get_service("executor")
                    st = get_service("store")
                    if rm:
                        rm.halt(reason)
                    if ex:
                        ex.flatten_all()
                    if st and hasattr(st, "save_risk_event"):
                        st.save_risk_event("KILL_SWITCH", reason)
                    await ws_manager.broadcast({
                        "type": "kill_switch",
                        "data": {"status": "killed", "reason": reason, "timestamp": int(time.time())},
                    })
                elif action == "flatten":
                    ex = get_service("executor")
                    st = get_service("store")
                    if ex:
                        ex.flatten_all()
                    if st and hasattr(st, "save_risk_event"):
                        st.save_risk_event("FLATTEN", "WebSocket 1-Click Flatten")
                    await ws_manager.broadcast({
                        "type": "flatten",
                        "data": {"status": "flattened", "timestamp": int(time.time())},
                    })
                elif action == "pause":
                    rm = get_service("risk_manager")
                    if rm:
                        rm.halt("Paused via WebSocket")
                    await ws_manager.broadcast({"type": "pause", "data": {"status": "paused"}})
                elif action == "resume":
                    rm = get_service("risk_manager")
                    if rm:
                        rm.resume()
                    await ws_manager.broadcast({"type": "resume", "data": {"status": "resumed"}})
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error("[api.ws] WebSocket error: %s", e)
        ws_manager.disconnect(websocket)


@router.get("/stream")
async def sse_stream():
    """Server-Sent Events (SSE) live updates stream."""
    async def event_generator():
        while True:
            snapshot = get_system_snapshot()
            yield f"data: {json.dumps(snapshot)}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

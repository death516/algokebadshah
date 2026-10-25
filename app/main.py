import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.fakes import FakeMarketData
from app.execution.paper import PaperExecutor
from app.storage.store import get_default_store, StoreLoggingHandler
from app.risk.manager import DeltaRiskManager
from app.api.routes import (router as api_router, set_services, set_execution_mode_controller,
                            set_asset_switch_controller, set_active_symbol, set_execution_mode, ws_manager,
                            set_live_market_price, set_live_feed_connected)

# Setup logging per working rules: [module] message
logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] %(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("app.main")

from app.strategy.trend import TrendStrategy
from app.engine.runner import EngineRunner
from app.exchange.client import DeltaClient, DeltaMarketData
from app.exchange.ws import DeltaWebSocket

# Global services: public Delta market data with local paper fills by default.
store = get_default_store()
delta_client = DeltaClient()
market_data_client = DeltaClient(base_url=settings.DELTA_MARKET_DATA_BASE_URL)


def _persist_paper_exit(event):
    store.save_trade_event(event)


executor = PaperExecutor(
    initial_equity=settings.PAPER_START_EQUITY,
    fee_rate=settings.PAPER_FEE_RATE,
    slippage_bps=settings.PAPER_SLIPPAGE_BPS,
    on_trade_event=_persist_paper_exit,
)
paper_executor = executor
active_executor = executor
risk_manager = DeltaRiskManager(flatten_callback=executor.flatten_all,
                                sizing_mode="kelly")
paper_risk_manager = risk_manager
active_risk_manager = risk_manager
executor.on_trade_closed = risk_manager.record_trade_result
market_data = DeltaMarketData(client=market_data_client, store=store)
strategy = TrendStrategy(symbol=settings.SYMBOL, htf_timeframe=settings.HTF_TIMEFRAME)
active_symbol = settings.SYMBOL

def seed_initial_candles(target_store):
    """Seed current public Bitcoin data or local cache into the database."""
    try:
        # First, seed candles from the configured public market-data environment.
        try:
            live_candles = market_data_client.get_candles(settings.SYMBOL, "5m", limit=100)
            if live_candles:
                target_store.save_candles(settings.SYMBOL, "5m", live_candles)
                logger.info("[storage] Seeded %d live 5m candles from Delta Exchange.", len(live_candles))
                for resolution, count in (("15m", 100), ("1h", 100)):
                    history = market_data_client.get_candles(settings.SYMBOL, resolution, limit=count)
                    if history:
                        target_store.save_candles(settings.SYMBOL, resolution, history)
                return
        except Exception as live_err:
            logger.warning("[storage] Could not pull initial live candles from Delta Exchange: %s", live_err)

        if not hasattr(target_store, "get_candle_count"):
            return
        cnt = target_store.get_candle_count(settings.SYMBOL, "15m")
        if cnt.get("count", 0) > 0:
            return

        base_dir = os.path.dirname(os.path.dirname(__file__))
        path_15m = os.path.join(base_dir, "backtest", "cache", "15min.parquet")
        path_1h = os.path.join(base_dir, "backtest", "cache", "1h.parquet")

        import pandas as pd
        from app.models import Candle

        if os.path.exists(path_15m):
            df15 = pd.read_parquet(path_15m).tail(300)
            candles15 = [
                Candle(
                    ts=int(row.Index.timestamp()) if hasattr(row.Index, "timestamp") else int(row.Index),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                )
                for row in df15.itertuples()
            ]
            target_store.save_candles(settings.SYMBOL, "15m", candles15)
            logger.info("[storage] Seeded %d 15m Bitcoin candles into database.", len(candles15))

        if os.path.exists(path_1h):
            df1h = pd.read_parquet(path_1h).tail(300)
            candles1h = [
                Candle(
                    ts=int(row.Index.timestamp()) if hasattr(row.Index, "timestamp") else int(row.Index),
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                )
                for row in df1h.itertuples()
            ]
            target_store.save_candles(settings.SYMBOL, "1h", candles1h)
            logger.info("[storage] Seeded %d 1h Bitcoin candles into database.", len(candles1h))
    except Exception as e:
        logger.warning("[storage] Could not seed initial candles: %s", e)

# Seed database with initial Bitcoin candle dataset if empty
seed_initial_candles(store)

# Unified engine runner
runner = EngineRunner(
    strategy=strategy,
    risk_manager=risk_manager,
    executor=executor,
    market_data=market_data,
    store=store,
)
runner.execution_enabled = True


def _on_public_ticker(data):
    set_live_market_price(data)
    mark = data.get("mark_price") or data.get("last_price")
    if mark:
        if active_executor is paper_executor and str(data.get("symbol", "")).upper() == runner.symbol.upper():
            active_executor.update_price(float(mark), int(data.get("received_timestamp", time.time())))
            active_risk_manager.enforce_account_limits(active_executor.get_account_state())
    try:
        asyncio.get_running_loop().create_task(ws_manager.broadcast({"type": "market_price", "data": data}))
    except RuntimeError:
        pass


def _on_public_feed_connection(connected):
    set_live_feed_connected(connected)


price_feed = DeltaWebSocket(symbol=settings.SYMBOL, symbols=["BTCUSD", "ETHUSD", "SOLUSD"],
                            on_ticker_callback=_on_public_ticker,
                            on_connection_callback=_on_public_feed_connection)

def get_live_runner() -> EngineRunner:
    """Create a live production trading engine configured with DeltaClient."""
    from app.exchange.client import DeltaClient, DeltaMarketData
    from app.execution.executor import DeltaExecutor

    client = DeltaClient()
    live_market_data = DeltaMarketData(client=client)
    live_executor = DeltaExecutor(client=client, symbol=active_symbol)
    live_risk = DeltaRiskManager(flatten_callback=live_executor.flatten_all, sizing_mode="kelly")
    return EngineRunner(
        strategy=strategy,
        risk_manager=live_risk,
        executor=live_executor,
        market_data=live_market_data,
        store=store,
    )


_demo_client = None
_demo_executor = None
_demo_risk_manager = None


def _switch_active_asset(symbol: str):
    """Switch the engine and dashboard market only when flat and order-free."""
    global active_symbol
    symbol = symbol.upper()
    if symbol not in {"BTCUSD", "ETHUSD", "SOLUSD"}:
        raise ValueError("Supported markets are BTCUSD, ETHUSD, and SOLUSD")

    if active_executor is paper_executor:
        if paper_executor.positions or paper_executor.get_open_orders():
            raise RuntimeError("Close the paper position and its orders before switching markets")
    elif _demo_client is not None:
        try:
            positions = _demo_client.get_positions()
            orders = _demo_client.get_open_orders()
        except Exception as exc:
            raise RuntimeError("Could not verify Delta demo exposure; remain on the current market") from exc
        if positions or orders:
            raise RuntimeError("Close all Delta demo positions and cancel open orders before switching markets")
        product = _demo_client.get_product_by_symbol(symbol)
        if not product or not product.get("id") or float(product.get("contract_value") or 0) <= 0:
            raise ValueError(f"Delta demo metadata is unavailable for {symbol}")
        _demo_executor.symbol = symbol

    active_symbol = symbol
    runner.symbol = symbol
    strategy.set_symbol(symbol)
    set_active_symbol(symbol)
    logger.info("[market] Switched active strategy and dashboard market to %s", symbol)
    return {"symbol": symbol}


def _switch_execution_mode(mode: str):
    """Activate the explicitly configured Delta demo account, never production."""
    global active_executor, active_risk_manager, _demo_client, _demo_executor, _demo_risk_manager
    global executor, risk_manager

    if mode in {"paper", "disabled"}:
        if _demo_executor is not None:
            try:
                positions = _demo_client.get_positions()
                orders = _demo_client.get_open_orders()
            except Exception as exc:
                raise RuntimeError("Could not verify Delta demo exposure; remain in demo mode until the account can be checked") from exc
            if positions or orders:
                raise RuntimeError("Close Delta demo positions and cancel its open orders before disabling execution")
        active_executor = paper_executor
        active_risk_manager = paper_risk_manager
        executor = paper_executor
        risk_manager = paper_risk_manager
        runner.executor = paper_executor
        runner.risk_manager = paper_risk_manager
        runner.execution_enabled = True
        set_services(executor=paper_executor, risk_manager=paper_risk_manager)
        logger.info("[execution] Paper mode active. Approved strategy signals will create simulated fills.")
        return {"exchange": "paper simulation", "symbol": active_symbol,
                "orders_enabled": False, "paper_trading_enabled": True}

    if mode != "delta_demo":
        raise ValueError("Unsupported execution mode")
    if not settings.DEMO_MODE:
        raise ValueError("Demo trading is disabled. Set DEMO_MODE=true in .env and restart the server.")
    if "testnet" not in settings.DELTA_BASE_URL.lower():
        raise ValueError("Refusing demo orders because DELTA_BASE_URL is not a testnet URL")
    if not settings.DELTA_API_KEY or not settings.DELTA_API_SECRET:
        raise ValueError("Delta demo API key and secret are required in .env")

    from app.execution.executor import DeltaExecutor
    if _demo_executor is None:
        _demo_client = DeltaClient()
        _demo_executor = DeltaExecutor(
            client=_demo_client,
            symbol=active_symbol,
            fallback_equity=settings.PAPER_START_EQUITY,
            max_notional_usd=settings.DEMO_MAX_NOTIONAL_USD,
        )
        _demo_risk_manager = DeltaRiskManager(flatten_callback=_demo_executor.flatten_all,
                                              sizing_mode="kelly")

    # Verify credentials, product metadata, and demo account connectivity before
    # routing the autonomous strategy into exchange orders.
    try:
        product = _demo_client.get_product_by_symbol(active_symbol)
        if not product or not product.get("id") or float(product.get("contract_value") or 0) <= 0:
            raise ValueError(f"Delta testnet product metadata is invalid for {active_symbol}")
        balance = _demo_client.get_balance()
        if not balance:
            raise ValueError("Delta demo account balance could not be verified")
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"Could not verify the Delta demo account: {exc}") from exc

    active_executor = _demo_executor
    active_risk_manager = _demo_risk_manager
    executor = active_executor
    risk_manager = active_risk_manager
    runner.executor = active_executor
    runner.risk_manager = active_risk_manager
    runner.execution_enabled = True
    set_services(executor=active_executor, risk_manager=active_risk_manager)
    logger.warning(
        "[execution] Delta DEMO TESTNET enabled for %s (per-order notional cap: $%.2f).",
        active_symbol, settings.DEMO_MAX_NOTIONAL_USD,
    )
    return {"exchange": "Delta demo testnet", "symbol": active_symbol,
            "max_order_notional_usd": settings.DEMO_MAX_NOTIONAL_USD}

# Attach SQLite/Postgres Store logging handler
store_logging_handler = StoreLoggingHandler(store=store, level=logging.INFO)
logging.getLogger().addHandler(store_logging_handler)

# Connect runtime services to API router
set_services(
    executor=executor,
    risk_manager=risk_manager,
    store=store,
    market_data=market_data,
    strategy=strategy,
)
set_execution_mode_controller(_switch_execution_mode)
set_asset_switch_controller(_switch_active_asset)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up Algo Trading Bot...")
    logger.info("Target Symbol: %s, Timeframe: %s", active_symbol, settings.TIMEFRAME)
    logger.info("Execution API URL: %s", settings.DELTA_BASE_URL)
    logger.info("Public market-data URL: %s", settings.DELTA_MARKET_DATA_BASE_URL)
    logger.info("Active Storage: %s", store.__class__.__name__)
    runner_task = None
    price_feed_task = None
    if not os.getenv("PYTEST_CURRENT_TEST"):
        logger.info("[main] Launching autonomous trading engine background loop...")
        runner_task = asyncio.create_task(runner.run_loop(poll_interval_seconds=15.0))
        logger.info("[main] Launching Delta public live price feed...")
        price_feed_task = asyncio.create_task(price_feed.connect_and_listen())
    yield
    if price_feed_task:
        price_feed.stop()
        price_feed_task.cancel()
    if runner_task:
        runner.stop()
        runner_task.cancel()
    logger.info("Shutting down Algo Trading Bot...")

app = FastAPI(
    title="Delta Exchange Algo Trading Bot",
    description="Delta demo trading dashboard with live market data and historical analysis",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for dashboard UI (React / Next.js / Tailwind on any local port)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root(request: Request):
    accept = request.headers.get("accept", "")
    user_agent = request.headers.get("user-agent", "").lower()
    format_param = request.query_params.get("format", "").lower()

    # If browser is navigating to the root URL, serve the full dashboard terminal
    is_browser = "text/html" in accept or "mozilla" in user_agent
    if is_browser and "application/json" not in accept and format_param != "json":
        return get_dashboard()

    return {
        "name": "Delta Exchange Algo Trader",
        "status": "halted" if risk_manager.is_halted() else "running",
        "symbol": active_symbol,
        "demo_mode": settings.DEMO_MODE,
        "database": getattr(store, "__class__", type(store)).__name__
    }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def get_dashboard():
    """Serve the Delta Quant Core web terminal dashboard."""
    dashboard_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "index.html")
    if os.path.exists(dashboard_path):
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard file not found</h1>", status_code=404)

# Mount API router under /api/v1, /api, and root prefixes for seamless dashboard compatibility
app.include_router(api_router, prefix="/api/v1")
app.include_router(api_router, prefix="/api")
app.include_router(api_router)

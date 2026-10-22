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
from app.api.routes import (router as api_router, set_services, ws_manager,
                            set_live_market_price, set_live_feed_connected)

# Setup logging per working rules: [module] message
logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] %(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("app.main")

from app.strategy.trend import TrendStrategy, PRODUCTION_PARAMS
from app.engine.runner import EngineRunner
from app.exchange.client import DeltaClient, DeltaMarketData
from app.exchange.ws import DeltaWebSocket

# Global services: public Delta candle feed with simulated paper execution.
store = get_default_store()
delta_client = DeltaClient()
executor = PaperExecutor(
    initial_equity=settings.PAPER_START_EQUITY,
    fee_rate=settings.PAPER_FEE_RATE,
    slippage_bps=settings.PAPER_SLIPPAGE_BPS,
)
risk_manager = DeltaRiskManager(flatten_callback=executor.flatten_all,
                                sizing_mode="anti_martingale")
executor.on_trade_closed = risk_manager.record_trade_result
market_data = DeltaMarketData(client=delta_client, store=store)
strategy = TrendStrategy(symbol=settings.SYMBOL, params=PRODUCTION_PARAMS,
                         htf_timeframe=settings.HTF_TIMEFRAME)

def seed_initial_candles(target_store):
    """Seed real Bitcoin live data from Delta Exchange Testnet or local cache into database."""
    try:
        # First, try to fetch current live candles from Delta Exchange Testnet
        try:
            live_candles = delta_client.get_candles(settings.SYMBOL, "15m", limit=100)
            if live_candles:
                target_store.save_candles(settings.SYMBOL, "15m", live_candles)
                logger.info("[storage] Seeded %d live 15m candles directly from Delta Exchange into database.", len(live_candles))
                live_1h = delta_client.get_candles(settings.SYMBOL, "1h", limit=50)
                if live_1h:
                    target_store.save_candles(settings.SYMBOL, "1h", live_1h)
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


def _on_public_ticker(data):
    set_live_market_price(data)
    mark = data.get("mark_price") or data.get("last_price")
    if mark:
        executor.update_price(float(mark), int(data.get("received_timestamp", time.time())))
        risk_manager.enforce_account_limits(executor.get_account_state())
    try:
        asyncio.get_running_loop().create_task(ws_manager.broadcast({"type": "market_price", "data": data}))
    except RuntimeError:
        pass


def _on_public_feed_connection(connected):
    set_live_feed_connected(connected)


price_feed = DeltaWebSocket(on_ticker_callback=_on_public_ticker,
                            on_connection_callback=_on_public_feed_connection)

def get_live_runner() -> EngineRunner:
    """Create a live production trading engine configured with DeltaClient."""
    from app.exchange.client import DeltaClient, DeltaMarketData
    from app.execution.executor import DeltaExecutor

    client = DeltaClient()
    live_market_data = DeltaMarketData(client=client)
    live_executor = DeltaExecutor(client=client, symbol=settings.SYMBOL)
    live_risk = DeltaRiskManager(flatten_callback=live_executor.flatten_all)
    return EngineRunner(
        strategy=strategy,
        risk_manager=live_risk,
        executor=live_executor,
        market_data=live_market_data,
        store=store,
    )

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up Algo Trading Bot...")
    logger.info("Target Symbol: %s, Timeframe: %s", settings.SYMBOL, settings.TIMEFRAME)
    logger.info("Testnet URL: %s", settings.DELTA_BASE_URL)
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
    description="Algorithmic paper trading dashboard and historical strategy analysis",
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
        "symbol": settings.SYMBOL,
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

"""Main trading engine runner loop (Role A).

Coordinates the end-to-end trading pipeline on closed candles:
MarketData -> Strategy (Signal) -> Risk Manager (evaluate) -> Executor (bracket order) -> Store (persist).
"""
import asyncio
import logging
import time
import uuid
from typing import Optional

from app.config import settings
from app.interfaces import Strategy, RiskManager, Executor, MarketData, Store
from app.models import OrderRequest, OrderResult, Candle
from app.data.indicators import compute_atr
from app.strategy.trend import timeframe_delta

logger = logging.getLogger("app.engine.runner")


class EngineRunner:
    """Orchestrates the end-to-end autonomous trading loop."""

    def __init__(
        self,
        strategy: Strategy,
        risk_manager: RiskManager,
        executor: Executor,
        market_data: MarketData,
        store: Store,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        htf_timeframe: Optional[str] = None,
    ):
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.executor = executor
        self.market_data = market_data
        self.store = store
        self.symbol = symbol or settings.SYMBOL
        self.timeframe = timeframe or settings.TIMEFRAME
        self.htf_timeframe = htf_timeframe or settings.HTF_TIMEFRAME

        self._running: bool = False
        self._last_processed_candle_ts: int = 0
        self._loop_task: Optional[asyncio.Task] = None
        logger.info(
            "[engine] Initialized EngineRunner for %s (TF: %s, HTF: %s)",
            self.symbol,
            self.timeframe,
            self.htf_timeframe,
        )

    def run_tick(self) -> Optional[OrderResult]:
        """Execute one complete cycle of the trading pipeline:
        1. Guard against halted state.
        2. Fetch market candles (15m & 1h).
        3. Evaluate strategy signal.
        4. Pass signal through the 8-rule risk manager.
        5. If approved, construct OrderRequest and execute on exchange.
        6. Persist signal, decision, trade, and equity to storage.
        """
        logger.info("[engine] Fetching candles for %s...", self.symbol)
        try:
            candles = self.market_data.get_candles(self.symbol, self.timeframe, limit=100)
            htf_candles = self.market_data.get_candles(self.symbol, self.htf_timeframe, limit=100)
        except Exception as e:
            logger.error("[engine] Failed to fetch market data: %s", e)
            return None

        if not candles:
            logger.warning("[engine] No candle data received. Skipping tick.")
            return None

        if hasattr(self.market_data, "has_fresh_candles") and not all(
            self.market_data.has_fresh_candles(tf) for tf in (self.timeframe, self.htf_timeframe)
        ):
            logger.warning("[engine] Live candle fetch failed for a required timeframe; paper orders are disabled while using cached data.")
            return None
        if hasattr(self.market_data, "last_fetch_live") and not hasattr(self.market_data, "has_fresh_candles") and not self.market_data.last_fetch_live:
            logger.warning("[engine] Live candle fetch failed; paper orders are disabled while using cached data.")
            return None

        now = int(time.time())
        tf_seconds = int(timeframe_delta(self.timeframe).total_seconds())
        htf_seconds = int(timeframe_delta(self.htf_timeframe).total_seconds())
        candles = [c for c in candles if c.ts + tf_seconds <= now]
        htf_candles = [c for c in htf_candles if c.ts + htf_seconds <= now]
        if not candles or not htf_candles:
            logger.info("[engine] Waiting for closed strategy and higher-timeframe candles.")
            return None
        # A 15m strategy necessarily acts from a just-closed candle, so allow
        # one candle duration plus the configured data grace period. Older API
        # responses must never be relabelled as fresh by the paper executor.
        execution_age = now - (candles[-1].ts + tf_seconds)
        trend_age = now - (htf_candles[-1].ts + htf_seconds)
        if execution_age > tf_seconds + settings.STALE_DATA_SECONDS or trend_age > htf_seconds + settings.STALE_DATA_SECONDS:
            logger.warning("[engine] Closed candle data is stale (execution=%ss, trend=%ss); paper entries are disabled.",
                           execution_age, trend_age)
            return None

        latest_candle = candles[-1]
        new_closed = ([c for c in candles if c.ts > self._last_processed_candle_ts]
                      if self._last_processed_candle_ts else [latest_candle])
        # The loop polls faster than the strategy timeframe. Manage every newly
        # closed bar once so a brief outage cannot skip an attached paper bracket.
        if not new_closed:
            return None
        atr_values = compute_atr(candles, period=14)
        candle_indexes = {c.ts: i for i, c in enumerate(candles)}
        for candle in new_closed:
            if hasattr(self.executor, "update_market"):
                try:
                    atr_value = float(atr_values[candle_indexes[candle.ts]]) if atr_values else 0.0
                    self.executor.update_market(candle, atr_value=atr_value)
                except Exception as e:
                    logger.exception("[engine] Paper mark-to-market update failed: %s", e)
            if hasattr(self.risk_manager, "enforce_account_limits"):
                self.risk_manager.enforce_account_limits(self.executor.get_account_state())
            if self.store:
                try:
                    tf_seconds = int(timeframe_delta(self.timeframe).total_seconds())
                    account_now = self.executor.get_account_state()
                    self.store.save_equity(candle.ts + tf_seconds, account_now.equity)
                except Exception:
                    logger.exception("[engine] Could not save paper equity snapshot")
        self._last_processed_candle_ts = latest_candle.ts

        # Even while paused/halted, continue managing open positions and stops.
        if self.risk_manager.is_halted():
            logger.warning("[engine] Risk manager is HALTED. Existing paper positions remain managed; no new entries.")
            return None
        logger.info(
            "[engine] Latest bar close: %.2f at %d",
            latest_candle.close,
            latest_candle.ts,
        )

        # 2. Strategy Signal Generation
        try:
            signal = self.strategy.generate_signal(candles, htf_candles)
        except NotImplementedError:
            logger.debug("[engine] Strategy generate_signal is not yet implemented (Role B).")
            return None
        except Exception as e:
            logger.error("[engine] Error generating strategy signal: %s", e)
            return None

        if signal is None:
            logger.info("[engine] No signal generated for current bar.")
            return None

        # Size paper orders from the freshest available mark, preserving the
        # strategy's ATR stop/target distances from the closed signal candle.
        paper_price = float(getattr(self.executor, "last_price", 0.0) or 0.0)
        if paper_price > 0 and hasattr(self.executor, "update_price"):
            direction = 1.0 if signal.side == "buy" else -1.0
            stop_distance = abs(signal.entry_price - signal.stop_loss)
            target_distance = abs(signal.take_profit - signal.entry_price)
            updates = {
                "entry_price": paper_price,
                "stop_loss": paper_price - direction * stop_distance,
                "take_profit": paper_price + direction * target_distance,
            }
            signal = signal.model_copy(update=updates) if hasattr(signal, "model_copy") else signal.copy(update=updates)

        logger.info(
            "[engine] Signal received: %s %s @ %.2f (SL: %.2f, TP: %.2f, Reason: %s)",
            signal.side.upper(),
            signal.symbol,
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit,
            signal.reason,
        )

        # 3. Risk Engine Evaluation
        account = self.executor.get_account_state()
        decision = self.risk_manager.evaluate(signal, account)

        # Persist signal and risk decision
        if self.store:
            self.store.save_signal(signal, decision)

        if not decision.approved:
            logger.warning("[engine] Trade REJECTED by Risk Manager: %s", decision.reason)
            return None

        # 4. Order Construction and Execution
        client_order_id = f"ord-{self.symbol.lower()}-{signal.side}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        order_request = OrderRequest(
            symbol=signal.symbol,
            side=signal.side,
            size=decision.size,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            client_order_id=client_order_id,
            signal_ts=signal.ts,
            atr=signal.atr,
            trail_atr_mult=signal.trail_atr_mult,
            use_take_profit=signal.use_take_profit,
            reason=signal.reason,
        )

        logger.info("[engine] Risk APPROVED. Sending OrderRequest: %s", order_request.client_order_id)
        result = self.executor.execute(order_request)

        # 5. Persistence
        if self.store:
            self.store.save_trade(order_request, result)
            if result.ok:
                self.store.save_equity(int(time.time()), self.executor.get_account_state().equity)

        logger.info(
            "[engine] Order result: ok=%s status=%s id=%s error=%s",
            result.ok,
            result.status,
            result.order_id,
            result.error,
        )
        return result

    async def run_loop(self, poll_interval_seconds: float = 30.0) -> None:
        """Asynchronous execution loop polling candles and running ticks."""
        self._running = True
        logger.info("[engine] Autonomous trading loop started (interval: %.1fs)", poll_interval_seconds)

        while self._running:
            try:
                self.run_tick()
            except Exception as e:
                logger.error("[engine] Error in trading loop tick: %s", e)

            await asyncio.sleep(poll_interval_seconds)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None, interval: float = 30.0) -> asyncio.Task:
        """Start runner loop in asyncio background task."""
        active_loop = loop or asyncio.get_event_loop()
        self._loop_task = active_loop.create_task(self.run_loop(poll_interval_seconds=interval))
        return self._loop_task

    def stop(self) -> None:
        """Gracefully stop engine loop."""
        logger.info("[engine] Stopping trading engine runner...")
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()

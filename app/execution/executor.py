"""Order execution engine for Delta Exchange testnet (Role A).

Implements the Executor protocol with bracket orders (attached SL/TP),
idempotency protection via client_order_id, status tracking, emergency flattening,
and account state polling.
"""
import logging
import time
import uuid
from typing import Optional, List, Dict, Any

from app.config import settings
from app.interfaces import Executor
from app.models import OrderRequest, OrderResult, AccountState, Position
from app.exchange.client import DeltaClient, DeltaAPIError

logger = logging.getLogger("app.execution.executor")


class DeltaExecutor(Executor):
    """Production executor implementing Executor protocol for Delta Exchange."""

    def __init__(
        self,
        client: Optional[DeltaClient] = None,
        symbol: Optional[str] = None,
        fallback_equity: float = 10000.0,
    ):
        self.client = client or DeltaClient()
        self.symbol = symbol or settings.SYMBOL
        self.fallback_equity = fallback_equity
        self._peak_equity: Optional[float] = None
        self._daily_baseline_equity: Optional[float] = None
        self._orders_history: List[OrderRequest] = []
        self._order_results: List[OrderResult] = []
        self._last_tick_ts: int = int(time.time())
        logger.info("[execution] Initialized DeltaExecutor for %s", self.symbol)

    def execute(self, order: OrderRequest) -> OrderResult:
        """Place entry order with attached bracket Stop Loss and Take Profit."""
        logger.info(
            "[execution] Executing %s %s size=%.4f entry=%.2f sl=%.2f tp=%.2f (client_id=%s)",
            order.side.upper(),
            order.symbol,
            order.size,
            order.entry_price,
            order.stop_loss,
            order.take_profit,
            order.client_order_id,
        )

        self._orders_history.append(order)

        # Check if API keys are configured; if not, return clear error
        if not self.client.api_key or not self.client.api_secret:
            msg = "Delta API keys not configured in .env. Order execution rejected."
            logger.warning("[execution] %s", msg)
            res = OrderResult(ok=False, error=msg, status="REJECTED")
            self._order_results.append(res)
            return res

        try:
            product_id = self.client.get_product_id(order.symbol)

            # Place order on Delta with bracket SL/TP
            response = self.client.place_order(
                product_id=product_id,
                size=order.size,
                side=order.side,
                order_type="limit_order",
                price=order.entry_price,
                client_order_id=order.client_order_id,
                stop_loss_price=order.stop_loss,
                take_profit_price=order.take_profit,
            )

            result_data = response.get("result", response)
            order_id = str(result_data.get("id") or result_data.get("order_id") or uuid.uuid4().hex[:8])
            state = str(result_data.get("state") or result_data.get("status") or "OPEN").upper()

            res = OrderResult(
                ok=True,
                order_id=order_id,
                status=state,
                error=None,
            )
            self._order_results.append(res)
            logger.info("[execution] Order successfully placed: ID=%s status=%s", order_id, state)
            return res

        except Exception as exc:
            error_msg = f"Order execution failed on Delta: {exc}"
            logger.error("[execution] %s", error_msg)
            res = OrderResult(ok=False, error=str(exc), status="REJECTED")
            self._order_results.append(res)
            return res

    def flatten_all(self) -> None:
        """Emergency Kill Switch: cancel all pending orders and close all open positions at market."""
        logger.warning("[execution] EMERGENCY FLATTEN ALL triggered!")

        try:
            # 1. Cancel all open orders
            logger.info("[execution] Cancelling all open orders...")
            self.client.cancel_all_orders()
        except Exception as e:
            logger.error("[execution] Failed to cancel open orders during flatten: %s", e)

        try:
            # 2. Query open positions and send market orders to close
            positions = self.client.get_positions()
            for pos in positions:
                if abs(pos.size) <= 0:
                    continue
                product_id = self.client.get_product_id(pos.symbol)
                close_side = "sell" if pos.side == "buy" else "buy"

                logger.warning(
                    "[execution] Market closing position %s %s size=%.4f",
                    pos.symbol,
                    pos.side,
                    pos.size,
                )
                self.client.place_order(
                    product_id=product_id,
                    size=pos.size,
                    side=close_side,
                    order_type="market_order",
                )
        except Exception as e:
            logger.error("[execution] Failed to close positions during flatten: %s", e)

    def get_account_state(self) -> AccountState:
        """Fetch real-time account equity, positions, daily PnL, and peak equity."""
        open_positions: List[Position] = []
        equity = self.fallback_equity

        if self.client.api_key and self.client.api_secret:
            try:
                # 1. Fetch positions
                open_positions = self.client.get_positions()
            except Exception as e:
                logger.debug("[execution] Could not fetch positions from exchange (%s)", e)

            try:
                # 2. Fetch wallet balance
                bal_data = self.client.get_balance()
                if isinstance(bal_data, list) and bal_data:
                    # Delta balance array item format
                    first_bal = bal_data[0]
                    equity = float(first_bal.get("balance") or first_bal.get("equity") or self.fallback_equity)
                elif isinstance(bal_data, dict):
                    equity = float(bal_data.get("equity") or bal_data.get("balance") or self.fallback_equity)
            except Exception as e:
                logger.debug("[execution] Could not fetch balance from exchange (%s)", e)

        # Initialize baseline & update peak equity
        if self._daily_baseline_equity is None:
            self._daily_baseline_equity = equity
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity

        # Calculate daily PnL
        daily_pnl = equity - self._daily_baseline_equity
        now = int(time.time())

        return AccountState(
            equity=equity,
            open_positions=open_positions,
            daily_pnl=daily_pnl,
            peak_equity=self._peak_equity,
            last_data_ts=now,
        )

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Fetch open orders from exchange or local history."""
        try:
            if self.client.api_key and self.client.api_secret:
                return self.client.get_open_orders()
        except Exception:
            pass
        return [o.model_dump() for o in self._orders_history]

"""Order execution engine for Delta Exchange testnet (Role A).

Implements the Executor protocol with bracket orders (attached SL/TP),
idempotency protection via client_order_id, status tracking, emergency flattening,
and account state polling.
"""
import logging
import math
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
        max_notional_usd: Optional[float] = None,
    ):
        self.client = client or DeltaClient()
        self.symbol = symbol or settings.SYMBOL
        self.fallback_equity = fallback_equity
        self.max_notional_usd = float(max_notional_usd if max_notional_usd is not None else settings.DEMO_MAX_NOTIONAL_USD)
        self._peak_equity: Optional[float] = None
        self._daily_baseline_equity: Optional[float] = None
        self._orders_history: List[OrderRequest] = []
        self._order_results: List[OrderResult] = []
        self._last_tick_ts: int = int(time.time())
        self.account_connected = False
        self.last_account_error: Optional[str] = "Account has not been verified against Delta demo yet."
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
            product = self.client.get_product_by_symbol(order.symbol)
            if not product:
                raise ValueError(f"Delta product metadata unavailable for {order.symbol}")
            product_id = int(product["id"])
            contract_value = float(product.get("contract_value") or 0)
            if contract_value <= 0:
                raise ValueError(f"Delta returned invalid contract value for {order.symbol}")

            # Engine sizes are BTC units. Delta order sizes are integer contracts.
            requested_contracts = math.floor((float(order.size) + 1e-12) / contract_value)
            max_contracts = math.floor(
                self.max_notional_usd / max(float(order.entry_price), 1e-12) / contract_value
            )
            contracts = min(requested_contracts, max_contracts)
            if contracts < 1:
                raise ValueError(
                    f"Risk-sized order is below one Delta contract or exceeds the ${self.max_notional_usd:.0f} demo order cap"
                )

            # Refuse to trade if the signed demo account is unreachable. Do not
            # allow the fallback equity used for display to authorize an order.
            balance = self.client.get_balance()
            if not balance:
                raise ValueError("Delta demo account returned no wallet balance; refusing to submit order")
            existing_positions = self.client.get_positions(product_id=product_id)
            if existing_positions:
                raise ValueError(f"An exchange position already exists for {order.symbol}; new entry rejected")
            existing_orders = self.client.get_open_orders(product_id=product_id)
            if existing_orders:
                raise ValueError(f"An exchange order is already open for {order.symbol}; duplicate entry rejected")

            # Market orders enter immediately; bracket exits are attached by Delta.
            response = self.client.place_order(
                product_id=product_id,
                size=contracts,
                side=order.side,
                order_type="market_order",
                client_order_id=order.client_order_id,
                stop_loss_price=order.stop_loss,
                take_profit_price=order.take_profit,
            )

            result_data = response.get("result", response)
            order_id = str(result_data.get("id") or result_data.get("order_id") or uuid.uuid4().hex[:8])
            state = str(result_data.get("state") or result_data.get("status") or "UNKNOWN").upper()

            res = OrderResult(
                ok=True,
                order_id=order_id,
                status=state,
                error=None,
            )
            self._order_results.append(res)
            logger.info("[execution] Delta demo order submitted: ID=%s status=%s contracts=%s", order_id, state, contracts)
            return res

        except Exception as exc:
            error_msg = f"Order execution failed on Delta: {exc}"
            logger.error("[execution] %s", error_msg)
            res = OrderResult(ok=False, error=str(exc), status="REJECTED")
            self._order_results.append(res)
            return res

    def flatten_all(self) -> None:
        """Cancel orders and close positions only for this executor's configured symbol."""
        logger.warning("[execution] EMERGENCY FLATTEN ALL triggered!")

        product_id = self.client.get_product_id(self.symbol)
        try:
            # 1. Cancel all open orders
            logger.info("[execution] Cancelling all open orders...")
            self.client.cancel_all_orders(product_id=product_id)
        except Exception as e:
            logger.error("[execution] Failed to cancel open orders during flatten: %s", e)

        try:
            # 2. Query open positions and send market orders to close
            positions = self.client.get_positions(product_id=product_id)
            for pos in positions:
                if abs(pos.size) <= 0:
                    continue
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
        account_error = None

        if self.client.api_key and self.client.api_secret:
            try:
                product = self.client.get_product_by_symbol(self.symbol)
                if not product:
                    raise ValueError(f"Delta product metadata unavailable for {self.symbol}")
                contract_value = float(product.get("contract_value") or 0)
                if contract_value <= 0:
                    raise ValueError(f"Delta returned invalid contract value for {self.symbol}")
                # Delta reports integer contracts; the application risk engine
                # and dashboard express position size in underlying BTC units.
                exchange_positions = self.client.get_positions(product_id=int(product["id"]))
                open_positions = [
                    pos.model_copy(update={"size": abs(pos.size) * contract_value})
                    if hasattr(pos, "model_copy") else
                    pos.copy(update={"size": abs(pos.size) * contract_value})
                    for pos in exchange_positions
                ]
            except Exception as e:
                account_error = str(e)
                logger.warning("[execution] Could not fetch demo positions from Delta: %s", e)

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
                account_error = str(e)
                logger.warning("[execution] Could not fetch demo balance from Delta: %s", e)
        else:
            account_error = "Delta API credentials are not configured."

        self.account_connected = bool(self.client.api_key and self.client.api_secret and not account_error)
        self.last_account_error = account_error

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
        """Fetch symbol-scoped orders; never treat an API failure as no exposure."""
        if not self.client.api_key or not self.client.api_secret:
            raise RuntimeError("Delta demo credentials are unavailable; open orders cannot be verified")
        product = self.client.get_product_by_symbol(self.symbol)
        if not product or not product.get("id"):
            raise RuntimeError(f"Delta product metadata unavailable for {self.symbol}; open orders cannot be verified")
        contract_value = float(product.get("contract_value") or 0)
        if contract_value <= 0:
            raise RuntimeError(f"Delta contract value unavailable for {self.symbol}; open orders cannot be verified")
        orders = self.client.get_open_orders(product_id=int(product["id"]))
        converted = []
        for order in orders:
            row = dict(order)
            for field in ("size", "unfilled_size"):
                if row.get(field) is not None:
                    row[f"{field}_btc"] = float(row[field]) * contract_value
            converted.append(row)
        return converted

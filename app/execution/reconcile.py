"""Position and order reconciliation loop (Role A).

Syncs local state with exchange positions and verifies SL/TP bracket orders are active.
"""
import logging
import time
from typing import Optional, Dict, Any, List

from app.config import settings
from app.exchange.client import DeltaClient
from app.execution.executor import DeltaExecutor

logger = logging.getLogger("app.execution.reconcile")


class Reconciler:
    """Periodically verifies and synchronizes exchange state."""

    def __init__(self, executor: Optional[DeltaExecutor] = None, client: Optional[DeltaClient] = None):
        self.executor = executor or DeltaExecutor(client=client)
        self.client = client or getattr(self.executor, "client", DeltaClient())
        self.last_reconciled_at: float = 0.0

    def reconcile(self) -> Dict[str, Any]:
        """Run reconciliation check against exchange:
        1. Fetch open positions from exchange.
        2. Fetch active orders to verify attached SL/TP brackets.
        3. If any position lacks an active SL or TP, alert or place protective bracket.
        """
        logger.info("[reconcile] Running position & order reconciliation...")
        now = time.time()
        self.last_reconciled_at = now

        summary = {
            "timestamp": int(now),
            "positions_count": 0,
            "orders_count": 0,
            "missing_brackets": [],
            "status": "ok",
        }

        if not self.client.api_key or not self.client.api_secret:
            logger.info("[reconcile] API keys not set. Skipping live exchange reconciliation.")
            return summary

        try:
            positions = self.client.get_positions()
            summary["positions_count"] = len(positions)

            open_orders = self.client.get_open_orders()
            summary["orders_count"] = len(open_orders)

            for pos in positions:
                if abs(pos.size) <= 0:
                    continue

                # Verify whether there are orders protecting this position
                has_protection = any(
                    str(o.get("product_id")) == str(self.client.get_product_id(pos.symbol))
                    and ("stop" in str(o.get("order_type", "")).lower() or "bracket" in str(o).lower())
                    for o in open_orders
                )

                if not has_protection:
                    warning_msg = f"Position {pos.symbol} {pos.side} {pos.size} BTC has no active SL/TP bracket!"
                    logger.warning("[reconcile] %s", warning_msg)
                    summary["missing_brackets"].append({
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "size": pos.size,
                    })

            logger.info(
                "[reconcile] Reconciliation complete. Positions: %d, Open Orders: %d, Missing Brackets: %d",
                summary["positions_count"],
                summary["orders_count"],
                len(summary["missing_brackets"]),
            )

        except Exception as exc:
            logger.error("[reconcile] Error during reconciliation: %s", exc)
            summary["status"] = f"error: {exc}"

        return summary

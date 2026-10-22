"""Simulated execution against the live candle feed; this class never calls an exchange."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.models import AccountState, OrderRequest, OrderResult, Position


class PaperExecutor:
    """Paper account with conservative candle-based stop/target fills and trading fees."""

    def __init__(self, initial_equity: float = 10_000.0, fee_rate: float = 0.0003,
                 slippage_bps: float = 2.0, on_trade_closed=None):
        self.initial_equity = float(initial_equity)
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_bps) / 10_000.0
        self.on_trade_closed = on_trade_closed
        self.realized_pnl = 0.0
        self.peak_equity = self.initial_equity
        self.daily_pnl = 0.0
        self._day = datetime.now(timezone.utc).date()
        self._daily_start_equity = self.initial_equity
        self.last_data_ts = int(time.time())
        self.last_price = 0.0
        self.last_price_source = "unavailable"
        self._last_live_price_ts = 0
        self.positions: list[Position] = []
        self._position: dict[str, Any] | None = None
        self._last_candle_ts = 0
        self.closed_trades: list[dict[str, Any]] = []
        self.orders: list[OrderRequest] = []

    def update_market(self, candle, atr_value: float = 0.0) -> None:
        """Mark the account, evaluate brackets, and advance ATR trailing stops."""
        now = int(time.time())
        self._last_candle_ts = int(candle.ts)
        if now - self._last_live_price_ts > 30:
            self.last_data_ts = now
            self.last_price = float(candle.close)
            self.last_price_source = "closed_candle"
        current_day = datetime.now(timezone.utc).date()
        if current_day != self._day:
            self._daily_start_equity = self._equity()
            self._day = current_day
            self.daily_pnl = 0.0

        pos = self._position
        if pos and int(candle.ts) > pos["signal_ts"]:
            side = pos["side"]
            is_long = side == "buy"
            stop_hit = candle.low <= pos["stop_loss"] if is_long else candle.high >= pos["stop_loss"]
            target_hit = pos["use_take_profit"] and (
                candle.high >= pos["take_profit"] if is_long else candle.low <= pos["take_profit"]
            )
            if stop_hit or target_hit:
                # If both brackets are inside one bar, assume the stop filled first.
                reason = "stop_loss" if stop_hit else "take_profit"
                level = pos["stop_loss"] if stop_hit else pos["take_profit"]
                if stop_hit and ((is_long and candle.open < level) or (not is_long and candle.open > level)):
                    exit_price = float(candle.open)
                else:
                    exit_price = float(level)
                self._close_position(exit_price, int(candle.ts), reason)

        # Backtest updates its trailing stop from each closed bar and applies it
        # to the next bar. Mirror that ordering in paper execution.
        pos = self._position
        if pos and pos["trail_atr_mult"] > 0 and atr_value > 0:
            distance = pos["trail_atr_mult"] * float(atr_value)
            if pos["side"] == "buy":
                pos["stop_loss"] = max(pos["stop_loss"], float(candle.close) - distance)
            else:
                pos["stop_loss"] = min(pos["stop_loss"], float(candle.close) + distance)

        self._refresh_position()
        equity = self._equity()
        self.peak_equity = max(self.peak_equity, equity)
        self.daily_pnl = equity - self._daily_start_equity

    def update_price(self, price: float, received_ts: int | None = None) -> None:
        """Refresh paper mark-to-market values from a live mark price.

        Bracket fills remain candle-based in update_market; a ticker update only
        refreshes valuation and does not simulate an intrabar fill.
        """
        if price <= 0:
            return
        self.last_data_ts = int(received_ts or time.time())
        self.last_price = float(price)
        self.last_price_source = "live_mark"
        self._last_live_price_ts = self.last_data_ts
        pos = self._position
        if pos:
            is_long = pos["side"] == "buy"
            stop_hit = price <= pos["stop_loss"] if is_long else price >= pos["stop_loss"]
            target_hit = pos["use_take_profit"] and (
                price >= pos["take_profit"] if is_long else price <= pos["take_profit"]
            )
            if stop_hit or target_hit:
                self._close_position(float(price), int(received_ts or time.time()),
                                     "stop_loss" if stop_hit else "take_profit")
        current_day = datetime.now(timezone.utc).date()
        if current_day != self._day:
            self._daily_start_equity = self._equity()
            self._day = current_day
            self.daily_pnl = 0.0
        self._refresh_position()
        equity = self._equity()
        self.peak_equity = max(self.peak_equity, equity)
        self.daily_pnl = equity - self._daily_start_equity

    def execute(self, order: OrderRequest) -> OrderResult:
        if self._position is not None:
            return OrderResult(ok=False, status="REJECTED", error="Paper account already has an open position")
        if self.last_price <= 0:
            self.last_price = float(order.entry_price)
        direction = 1.0 if order.side == "buy" else -1.0
        entry = float(self.last_price) * (1.0 + direction * self.slippage_rate)
        stop_distance = abs(float(order.entry_price) - float(order.stop_loss))
        target_distance = abs(float(order.take_profit) - float(order.entry_price))
        stop_loss = entry - direction * stop_distance
        take_profit = entry + direction * target_distance
        fee = abs(entry * float(order.size)) * self.fee_rate
        self.realized_pnl -= fee
        self._position = {
            "symbol": order.symbol,
            "side": order.side,
            "size": float(order.size),
            "entry_price": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "use_take_profit": bool(order.use_take_profit),
            "trail_atr_mult": max(0.0, float(order.trail_atr_mult)),
            "signal_ts": int(order.signal_ts if order.signal_ts is not None else self._last_candle_ts),
            "atr": max(0.0, float(order.atr)),
            "entry_ts": self.last_data_ts,
            "entry_fee": fee,
            "client_order_id": order.client_order_id,
        }
        self.orders.append(order)
        self._refresh_position()
        order_id = f"paper-{uuid.uuid4().hex[:10]}"
        return OrderResult(ok=True, order_id=order_id, status="PAPER_FILLED")

    def _close_position(self, exit_price: float, exit_ts: int, reason: str) -> None:
        pos = self._position
        if not pos:
            return
        direction = 1.0 if pos["side"] == "buy" else -1.0
        # A close is the opposite side of the entry, so simulated slippage is adverse.
        exit_price = float(exit_price) * (1.0 - direction * self.slippage_rate)
        gross = direction * (exit_price - pos["entry_price"]) * pos["size"]
        exit_fee = abs(exit_price * pos["size"]) * self.fee_rate
        net = gross - pos["entry_fee"] - exit_fee
        self.realized_pnl += gross - exit_fee
        self.closed_trades.append({
            "id": pos["client_order_id"], "symbol": pos["symbol"], "side": pos["side"].upper(),
            "size": pos["size"], "entry_ts": pos["entry_ts"], "exit_ts": exit_ts,
            "entry_price": pos["entry_price"], "exit_price": exit_price,
            "pnl": net, "fees": pos["entry_fee"] + exit_fee, "exit_reason": reason,
            "route": "PAPER",
        })
        self._position = None
        if self.on_trade_closed:
            try:
                self.on_trade_closed(net)
            except Exception:
                pass

    def _unrealized(self) -> float:
        if not self._position or self.last_price <= 0:
            return 0.0
        p = self._position
        direction = 1.0 if p["side"] == "buy" else -1.0
        return direction * (self.last_price - p["entry_price"]) * p["size"]

    def _equity(self) -> float:
        return self.initial_equity + self.realized_pnl + self._unrealized()

    def _refresh_position(self) -> None:
        if not self._position:
            self.positions = []
            return
        p = self._position
        self.positions = [Position(
            symbol=p["symbol"], side=p["side"], size=p["size"], entry_price=p["entry_price"],
            unrealized_pnl=self._unrealized(), stop_loss=p["stop_loss"],
            take_profit=p["take_profit"] if p["use_take_profit"] else None,
            entry_ts=p["entry_ts"],
        )]

    def get_account_state(self) -> AccountState:
        self._refresh_position()
        return AccountState(
            equity=self._equity(), open_positions=list(self.positions), daily_pnl=self.daily_pnl,
            peak_equity=self.peak_equity, last_data_ts=self.last_data_ts,
        )

    def get_open_orders(self) -> list[dict[str, Any]]:
        return []

    def flatten_all(self) -> None:
        if self._position:
            self._close_position(self.last_price or self._position["entry_price"], self.last_data_ts, "manual_flatten")
        self._refresh_position()

    def summary(self) -> dict[str, Any]:
        account = self.get_account_state()
        return {
            "mode": "paper", "exchange_orders_enabled": False,
            "initial_equity": self.initial_equity, "equity": round(account.equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self._unrealized(), 2),
            "daily_pnl": round(self.daily_pnl, 2), "peak_equity": round(self.peak_equity, 2),
            "last_price": self.last_price, "last_price_source": self.last_price_source,
            "open_positions": [p.model_dump() for p in self.positions],
            "closed_trades": list(reversed(self.closed_trades[-250:])),
            "closed_trade_count": len(self.closed_trades),
        }

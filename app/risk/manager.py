"""Full Risk Manager implementation satisfying RiskManager protocol (Role C).

Implements all 8 safety checks in order:
  1. System Halt Guard        - blocks if kill switch is active
  2. Stale Data Guard         - blocks if market data is older than STALE_DATA_SECONDS
  3. Max Open Positions       - blocks if already at the position limit
  4. Daily Loss Limit         - halts for the day if losses exceed DAILY_LOSS_LIMIT_PCT
  5. Drawdown Kill Switch     - flattens and halts if drawdown exceeds MAX_DRAWDOWN_PCT
  6. Order Sanity Check       - validates SL/TP are on the correct sides of entry
  7. Position Sizing          - scales fixed-fraction risk down as ATR volatility rises
  8. Leverage & Notional Caps - clamps size within MAX_LEVERAGE and MAX_NOTIONAL_USD
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Callable

from app.models import Signal, AccountState, RiskDecision
from app.interfaces import RiskManager
from app.config import settings
from app.risk.sizing import calculate_position_size, volatility_adjusted_risk_pct

logger = logging.getLogger("app.risk.manager")


class DeltaRiskManager(RiskManager):
    """Production-grade risk manager with 8 safety checks and a manual kill switch.

    Args:
        flatten_callback: Optional callable the manager invokes when a drawdown
                          kill switch fires.  The engine/executor should wire its
                          ``flatten_all()`` method here so positions are closed
                          automatically.
        clock:            Optional callable returning current unix timestamp (seconds).
                          Defaults to ``time.time``.  Inject a fake clock in tests.
    """

    def __init__(
        self,
        flatten_callback: Optional[Callable[[], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        sizing_mode: str = "fixed",
        anti_mult: float = 1.5,
        anti_max_steps: int = 3,
    ) -> None:
        self._halted: bool = False
        self._halt_reason: str = ""
        self._flatten_callback = flatten_callback
        self._clock = clock or time.time
        self.sizing_mode = sizing_mode
        self.anti_mult = anti_mult
        self.anti_max_steps = anti_max_steps
        self._win_streak = 0
        self._kelly_r_multiples: list[float] = []
        self._daily_halt_day = None

    def record_trade_result(self, pnl: float, initial_risk: Optional[float] = None) -> None:
        """Update rolling position-sizing statistics after a paper position closes."""
        if initial_risk is not None and initial_risk > 0:
            self._kelly_r_multiples.append(float(pnl) / float(initial_risk))
            del self._kelly_r_multiples[:-50]
        if pnl > 0:
            self._win_streak += 1
        else:
            self._win_streak = 0

    def enforce_account_limits(self, account: AccountState) -> None:
        """Enforce account-wide loss limits continuously, including between signals."""
        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        if self._daily_halt_day is not None and today > self._daily_halt_day:
            logger.info("[risk] Daily loss pause expired at UTC day boundary.")
            self._halted = False
            self._halt_reason = ""
            self._daily_halt_day = None
        elif self._halted and self._daily_halt_day is None:
            return

        # Drawdown is a hard kill switch and must be checked even if a daily
        # loss pause is already preventing new entries.
        if account.peak_equity > 0:
            drawdown_pct = (account.peak_equity - account.equity) / account.peak_equity * 100.0
            if drawdown_pct >= settings.MAX_DRAWDOWN_PCT:
                self.halt(f"Max drawdown kill switch triggered: {drawdown_pct:.2f}% >= {settings.MAX_DRAWDOWN_PCT}%")
                self._daily_halt_day = None
                if self._flatten_callback is not None:
                    self._flatten_callback()
                return

        # AccountState.daily_pnl is measured from the executor's day-start
        # equity. Recover that baseline so the loss threshold does not shrink
        # or expand with intraday mark-to-market equity changes.
        day_start_equity = max(0.0, account.equity - account.daily_pnl)
        daily_limit = -(settings.DAILY_LOSS_LIMIT_PCT / 100.0) * day_start_equity
        if account.daily_pnl < daily_limit:
            self._daily_halt_day = today
            self.halt(f"Daily loss limit breached: {account.daily_pnl:.2f} < {daily_limit:.2f}")

    @property
    def halt_reason(self) -> str:
        """Return the reason why the system is halted, or empty string if not halted."""
        return self._halt_reason

    # ------------------------------------------------------------------
    # RiskManager Protocol implementation
    # ------------------------------------------------------------------

    def evaluate(self, signal: Signal, account: AccountState) -> RiskDecision:
        """Run all 8 checks in sequence.  Returns immediately on first failure."""

        # -----------------------------------------------------------
        # Check 1: System Halt Guard
        # -----------------------------------------------------------
        if self._halted:
            logger.warning("[risk] Rejected — system is HALTED: %s", self._halt_reason)
            return RiskDecision(
                approved=False,
                size=0.0,
                reason=f"System halted: {self._halt_reason}",
            )

        # -----------------------------------------------------------
        # Check 2: Stale Data Guard
        # -----------------------------------------------------------
        now = self._clock()
        data_age = now - account.last_data_ts
        if account.last_data_ts <= 0 or data_age > settings.STALE_DATA_SECONDS:
            reason = (
                f"Stale market data: last tick was {data_age:.0f}s ago "
                f"(limit {settings.STALE_DATA_SECONDS}s)"
            )
            logger.warning("[risk] Rejected — %s", reason)
            return RiskDecision(approved=False, size=0.0, reason=reason)

        # -----------------------------------------------------------
        # Check 3: Max Open Positions
        # -----------------------------------------------------------
        if len(account.open_positions) >= settings.MAX_OPEN_POSITIONS:
            reason = (
                f"Max open positions reached "
                f"({len(account.open_positions)}/{settings.MAX_OPEN_POSITIONS})"
            )
            logger.info("[risk] Rejected — %s", reason)
            return RiskDecision(approved=False, size=0.0, reason=reason)

        # -----------------------------------------------------------
        # Check 4: Daily Loss Limit
        # -----------------------------------------------------------
        self.enforce_account_limits(account)
        if self._halted:
            return RiskDecision(approved=False, size=0.0, reason=self._halt_reason)

        # -----------------------------------------------------------
        # Check 6: Order Sanity Check
        # -----------------------------------------------------------
        sanity_error = self._sanity_check(signal)
        if sanity_error:
            logger.warning("[risk] Rejected — order sanity: %s", sanity_error)
            return RiskDecision(approved=False, size=0.0, reason=f"Order sanity: {sanity_error}")

        # -----------------------------------------------------------
        # Check 7 & 8: Position Sizing + Leverage/Notional Caps
        #   (calculate_position_size already applies both caps)
        # -----------------------------------------------------------
        risk_pct = settings.RISK_PER_TRADE_PCT
        if self.sizing_mode == "volatility_fixed":
            risk_pct = volatility_adjusted_risk_pct(
                base_risk_pct=settings.RISK_PER_TRADE_PCT,
                atr_value=signal.atr,
                entry_price=signal.entry_price,
                target_atr_pct=settings.VOL_TARGET_ATR_PCT,
                min_risk_pct=settings.MIN_RISK_PER_TRADE_PCT,
            )
        elif self.sizing_mode == "anti_martingale":
            risk_pct *= self.anti_mult ** min(self._win_streak, self.anti_max_steps)
            risk_pct = min(risk_pct, 10.0)
        elif self.sizing_mode == "kelly":
            # WMA strategy sizing: warm up at 0.5% until 20 trades; then use
            # half-Kelly over the last 50 closed trade R multiples, clipped to
            # the strategy's 0.25%-2.5% risk band.
            outcomes = self._kelly_r_multiples[-50:]
            risk_pct = 0.5
            if len(outcomes) >= 20:
                wins = [value for value in outcomes if value > 0]
                losses = [-value for value in outcomes if value < 0]
                if not wins:
                    risk_pct = 0.25
                elif not losses:
                    risk_pct = 2.5
                else:
                    win_rate = len(wins) / len(outcomes)
                    win_loss_ratio = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
                    half_kelly = 0.5 * (win_rate - (1.0 - win_rate) / win_loss_ratio)
                    risk_pct = min(2.5, max(0.25, half_kelly * 100.0))
        size = calculate_position_size(
            equity=account.equity,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            risk_pct=risk_pct,
            max_leverage=float(settings.MAX_LEVERAGE),
            max_notional=settings.MAX_NOTIONAL_USD,
        )

        if size <= 0:
            reason = (
                f"Computed position size is zero or negative after sizing/caps "
                f"(equity={account.equity}, entry={signal.entry_price}, "
                f"stop={signal.stop_loss})"
            )
            logger.warning("[risk] Rejected — %s", reason)
            return RiskDecision(approved=False, size=0.0, reason=reason)

        approval_reason = (
            f"Approved with volatility-adjusted equity risk of {risk_pct:.3f}%"
            if self.sizing_mode == "volatility_fixed"
            else "Approved by risk manager"
        )
        logger.info(
            "[risk] APPROVED — %s %s size=%.4f risk=%.3f%% entry=%.2f sl=%.2f tp=%.2f",
            signal.side.upper(),
            signal.symbol,
            size,
            risk_pct,
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit,
        )
        return RiskDecision(approved=True, size=size, reason=approval_reason)

    def is_halted(self) -> bool:
        return self._halted

    def halt(self, reason: str) -> None:
        logger.warning("[risk] HALT — %s", reason)
        self._halted = True
        self._halt_reason = reason

    def resume(self) -> None:
        logger.info("[risk] Trading RESUMED — previous halt reason was: %s", self._halt_reason)
        self._halted = False
        self._halt_reason = ""
        self._daily_halt_day = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanity_check(signal: Signal) -> str:
        """Return an error string if the signal geometry is invalid, else empty string."""
        entry = signal.entry_price
        sl = signal.stop_loss
        tp = signal.take_profit

        if entry <= 0 or sl <= 0 or tp <= 0:
            return "entry, stop_loss, and take_profit must all be positive"

        if signal.side == "buy":
            if not (sl < entry):
                return f"BUY: stop_loss ({sl}) must be below entry ({entry})"
            if not (tp > entry):
                return f"BUY: take_profit ({tp}) must be above entry ({entry})"
        elif signal.side == "sell":
            if not (sl > entry):
                return f"SELL: stop_loss ({sl}) must be above entry ({entry})"
            if not (tp < entry):
                return f"SELL: take_profit ({tp}) must be below entry ({entry})"
        else:
            return f"Unknown side: {signal.side!r}"

        return ""

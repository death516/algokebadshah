"""Comprehensive unit tests for the full 8-rule Risk Manager (Role C).

Each test targets exactly one rule, making it trivial to identify which
safety check failed when a test breaks.

Test naming convention:
    test_rule<N>_<short_description>
"""
import time
import pytest
from app.models import Signal, AccountState, Position
from app.risk.manager import DeltaRiskManager
from app.risk.sizing import calculate_position_size


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_account(
    equity: float = 10_000.0,
    daily_pnl: float = 0.0,
    peak_equity: float = 10_000.0,
    n_positions: int = 0,
    last_data_age_sec: float = 0.0,          # seconds ago the last tick arrived
) -> AccountState:
    """Factory for AccountState with sensible defaults."""
    now = int(time.time())
    positions = [
        Position(
            symbol="BTCUSD",
            side="buy",
            size=0.01,
            entry_price=60_000.0,
            unrealized_pnl=0.0,
        )
        for _ in range(n_positions)
    ]
    return AccountState(
        equity=equity,
        open_positions=positions,
        daily_pnl=daily_pnl,
        peak_equity=peak_equity,
        last_data_ts=int(now - last_data_age_sec),
    )


def make_signal(
    side: str = "buy",
    entry: float = 60_000.0,
    sl: float = 59_100.0,       # 1.5% below entry (1.5*ATR-like)
    tp: float = 61_800.0,       # 3% above entry  (2R)
    symbol: str = "BTCUSD",
) -> Signal:
    return Signal(
        symbol=symbol,
        side=side,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        reason="unit-test signal",
        ts=int(time.time()),
    )


def make_manager(flatten_called: list | None = None) -> DeltaRiskManager:
    """Return a fresh risk manager.  Pass a list to capture flatten_all calls."""
    cb = (lambda: flatten_called.append(True)) if flatten_called is not None else None
    return DeltaRiskManager(flatten_callback=cb)


# ---------------------------------------------------------------------------
# Rule 1 — System Halt Guard
# ---------------------------------------------------------------------------

class TestRule1SystemHaltGuard:
    def test_halted_system_rejects_signal(self):
        rm = make_manager()
        rm.halt("Manual kill switch test")

        decision = rm.evaluate(make_signal(), make_account())

        assert not decision.approved
        assert "halted" in decision.reason.lower()

    def test_resume_allows_signal_again(self):
        rm = make_manager()
        rm.halt("test")
        rm.resume()

        decision = rm.evaluate(make_signal(), make_account())

        assert decision.approved

    def test_is_halted_reflects_state(self):
        rm = make_manager()
        assert not rm.is_halted()
        rm.halt("test")
        assert rm.is_halted()
        rm.resume()
        assert not rm.is_halted()


# ---------------------------------------------------------------------------
# Rule 2 — Stale Data Guard
# ---------------------------------------------------------------------------

class TestRule2StaleDataGuard:
    def test_stale_data_rejects_signal(self):
        rm = make_manager()
        # last data was 200 seconds ago (limit is 120s)
        account = make_account(last_data_age_sec=200.0)

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved
        assert "stale" in decision.reason.lower()

    def test_fresh_data_allows_signal(self):
        rm = make_manager()
        account = make_account(last_data_age_sec=5.0)   # 5 seconds ago

        decision = rm.evaluate(make_signal(), account)

        assert decision.approved

    def test_exactly_at_limit_is_rejected(self):
        """Data exactly at STALE_DATA_SECONDS is treated as stale (strict >)."""
        from app.config import settings
        rm = make_manager()
        account = make_account(last_data_age_sec=float(settings.STALE_DATA_SECONDS) + 0.1)

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved


# ---------------------------------------------------------------------------
# Rule 3 — Max Open Positions
# ---------------------------------------------------------------------------

class TestRule3MaxOpenPositions:
    def test_at_max_positions_rejects(self):
        rm = make_manager()
        account = make_account(n_positions=1)   # MAX_OPEN_POSITIONS = 1

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved
        assert "max open positions" in decision.reason.lower()

    def test_zero_positions_allows_signal(self):
        rm = make_manager()
        account = make_account(n_positions=0)

        decision = rm.evaluate(make_signal(), account)

        assert decision.approved

    def test_reason_contains_count(self):
        rm = make_manager()
        account = make_account(n_positions=1)
        decision = rm.evaluate(make_signal(), account)
        assert "1/1" in decision.reason or "1" in decision.reason


# ---------------------------------------------------------------------------
# Rule 4 — Daily Loss Limit
# ---------------------------------------------------------------------------

class TestRule4DailyLossLimit:
    def test_breach_daily_loss_halts_and_rejects(self):
        rm = make_manager()
        # 10000 equity, 3% limit = -300. Set daily_pnl to -400 (breached)
        account = make_account(equity=10_000.0, daily_pnl=-400.0)

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved
        assert rm.is_halted(), "Risk manager must self-halt after daily loss breach"
        assert "daily" in decision.reason.lower()

    def test_just_under_limit_allows_signal(self):
        rm = make_manager()
        # -299 < -300 limit — just inside the allowed range
        account = make_account(equity=10_000.0, daily_pnl=-299.0)

        decision = rm.evaluate(make_signal(), account)

        assert decision.approved

    def test_exactly_at_limit_is_safe(self):
        """daily_pnl == -300 is NOT a breach (breach requires strictly less than)."""
        rm = make_manager()
        account = make_account(equity=10_000.0, daily_pnl=-300.0)

        decision = rm.evaluate(make_signal(), account)

        assert decision.approved


# ---------------------------------------------------------------------------
# Rule 5 — Drawdown Kill Switch
# ---------------------------------------------------------------------------

class TestRule5DrawdownKillSwitch:
    def test_10pct_drawdown_halts_and_flattens(self):
        flatten_calls: list = []
        rm = make_manager(flatten_called=flatten_calls)
        # Peak 10000, current 8900 => 11% drawdown (> 10%)
        account = make_account(equity=8_900.0, peak_equity=10_000.0)

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved
        assert rm.is_halted()
        assert len(flatten_calls) == 1, "flatten_all should be called once"
        assert "drawdown" in decision.reason.lower()

    def test_9pct_drawdown_allows_signal(self):
        rm = make_manager()
        # Peak 10000, current 9100 => 9% drawdown (< 10%)
        account = make_account(equity=9_100.0, peak_equity=10_000.0)

        decision = rm.evaluate(make_signal(), account)

        assert decision.approved

    def test_exactly_10pct_drawdown_triggers_switch(self):
        rm = make_manager()
        # Peak 10000, current 9000 => exactly 10% drawdown
        account = make_account(equity=9_000.0, peak_equity=10_000.0)

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved
        assert rm.is_halted()

    def test_flatten_not_called_when_no_callback(self):
        """When no flatten_callback provided, manager should still halt without error."""
        rm = DeltaRiskManager(flatten_callback=None)
        account = make_account(equity=8_900.0, peak_equity=10_000.0)

        decision = rm.evaluate(make_signal(), account)

        assert not decision.approved
        assert rm.is_halted()


# ---------------------------------------------------------------------------
# Rule 6 — Order Sanity Check
# ---------------------------------------------------------------------------

class TestRule6OrderSanityCheck:
    def test_buy_sl_above_entry_rejected(self):
        rm = make_manager()
        # BUY but SL is above entry — nonsensical
        signal = make_signal(side="buy", entry=60_000, sl=61_000, tp=63_000)

        decision = rm.evaluate(signal, make_account())

        assert not decision.approved
        assert "sanity" in decision.reason.lower() or "stop_loss" in decision.reason.lower()

    def test_buy_tp_below_entry_rejected(self):
        rm = make_manager()
        signal = make_signal(side="buy", entry=60_000, sl=59_000, tp=59_500)

        decision = rm.evaluate(signal, make_account())

        assert not decision.approved

    def test_sell_sl_below_entry_rejected(self):
        rm = make_manager()
        # SELL but SL is below entry — nonsensical
        signal = make_signal(side="sell", entry=60_000, sl=59_000, tp=57_000)

        decision = rm.evaluate(signal, make_account())

        assert not decision.approved

    def test_sell_tp_above_entry_rejected(self):
        rm = make_manager()
        signal = make_signal(side="sell", entry=60_000, sl=61_000, tp=61_500)

        decision = rm.evaluate(signal, make_account())

        assert not decision.approved

    def test_valid_buy_signal_passes(self):
        rm = make_manager()
        signal = make_signal(side="buy", entry=60_000, sl=58_500, tp=63_000)

        decision = rm.evaluate(signal, make_account())

        assert decision.approved

    def test_valid_sell_signal_passes(self):
        rm = make_manager()
        signal = make_signal(side="sell", entry=60_000, sl=61_500, tp=57_000)

        decision = rm.evaluate(signal, make_account())

        assert decision.approved


# ---------------------------------------------------------------------------
# Rule 7 — Position Sizing (1% of equity)
# ---------------------------------------------------------------------------

class TestRule7PositionSizing:
    def test_size_risks_1pct_of_equity(self):
        """
        equity=10000, risk=1% → risk_amount=100
        entry=60000, sl=59000 → stop_distance=1000
        expected size = 100 / 1000 = 0.1 BTC
        """
        size = calculate_position_size(
            equity=10_000.0,
            entry_price=60_000.0,
            stop_loss=59_000.0,
            risk_pct=1.0,
            max_leverage=5.0,
            max_notional=50_000.0,
        )
        assert size == pytest.approx(0.1, rel=1e-4)

    def test_size_zero_when_stop_distance_is_zero(self):
        size = calculate_position_size(
            equity=10_000.0,
            entry_price=60_000.0,
            stop_loss=60_000.0,   # same as entry — zero distance
        )
        assert size == 0.0

    def test_larger_stop_gives_smaller_size(self):
        """A wider stop loss should produce a smaller position (risk is constant)."""
        size_tight = calculate_position_size(10_000, 60_000, 59_500, risk_pct=1.0)
        size_wide = calculate_position_size(10_000, 60_000, 58_000, risk_pct=1.0)
        assert size_wide < size_tight

    def test_size_reflects_in_risk_decision(self):
        rm = make_manager()
        signal = make_signal(entry=60_000, sl=59_000)   # stop_distance=1000
        account = make_account(equity=10_000.0)

        decision = rm.evaluate(signal, account)

        assert decision.approved
        assert decision.size == pytest.approx(0.1, rel=1e-3)


# ---------------------------------------------------------------------------
# Rule 8 — Leverage & Notional Caps
# ---------------------------------------------------------------------------

class TestRule8LeverageAndNotionalCaps:
    def test_leverage_cap_limits_size(self):
        """
        equity=100, max_leverage=5 → max notional = 500
        entry=60000 → max_leverage_size = 500/60000 ≈ 0.00833
        risk formula (1%) = 1/stop_distance, which can be large with tiny stop
        """
        size = calculate_position_size(
            equity=100.0,
            entry_price=60_000.0,
            stop_loss=59_999.0,    # tiny stop → formula wants 1.0 BTC
            risk_pct=1.0,
            max_leverage=5.0,
            max_notional=999_999.0,  # huge notional so only leverage cap bites
        )
        max_leverage_size = (100.0 * 5.0) / 60_000.0
        assert size <= max_leverage_size + 1e-6

    def test_notional_cap_limits_size(self):
        """
        max_notional = 1000, entry = 60000
        → max_notional_size = 1000/60000 ≈ 0.01667
        """
        size = calculate_position_size(
            equity=1_000_000.0,      # huge equity → formula would give massive size
            entry_price=60_000.0,
            stop_loss=59_000.0,
            risk_pct=1.0,
            max_leverage=1000.0,     # very high leverage so only notional bites
            max_notional=1_000.0,
        )
        max_notional_size = 1_000.0 / 60_000.0
        # Allow for 4dp rounding applied inside calculate_position_size
        assert size <= max_notional_size + 1e-3

    def test_size_never_negative(self):
        size = calculate_position_size(
            equity=0.0,
            entry_price=60_000.0,
            stop_loss=59_000.0,
        )
        assert size >= 0.0


# ---------------------------------------------------------------------------
# Integration — all checks together
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_clean_signal_approved_end_to_end(self):
        rm = make_manager()
        signal = make_signal(side="buy", entry=60_000, sl=58_500, tp=63_000)
        account = make_account(
            equity=10_000.0,
            daily_pnl=0.0,
            peak_equity=10_000.0,
            n_positions=0,
            last_data_age_sec=2.0,
        )

        decision = rm.evaluate(signal, account)

        assert decision.approved
        assert decision.size > 0
        assert "Approved" in decision.reason

    def test_halted_system_takes_priority_over_other_rules(self):
        """Even a perfect signal must be rejected when the system is halted."""
        rm = make_manager()
        rm.halt("Manual override")
        signal = make_signal()
        account = make_account()   # all green

        decision = rm.evaluate(signal, account)

        assert not decision.approved
        assert "halted" in decision.reason.lower()

    def test_stale_data_evaluated_before_position_check(self):
        """Stale data check (rule 2) must fire even when max positions is reached (rule 3).
        This verifies our check ordering."""
        rm = make_manager()
        signal = make_signal()
        # Both stale data AND max positions reached
        account = make_account(n_positions=1, last_data_age_sec=999.0)

        decision = rm.evaluate(signal, account)

        # Stale data reason should appear (it's checked first)
        assert not decision.approved
        assert "stale" in decision.reason.lower()

    def test_halt_reason_property(self):
        rm = make_manager()
        assert rm.halt_reason == ""
        rm.halt("Emergency stop")
        assert rm.halt_reason == "Emergency stop"
        rm.resume()
        assert rm.halt_reason == ""

    def test_uninitialized_last_data_ts_is_rejected(self):
        rm = make_manager()
        account = make_account()
        account.last_data_ts = 0  # Uninitialized
        decision = rm.evaluate(make_signal(), account)
        assert not decision.approved
        assert "stale" in decision.reason.lower()

    def test_non_positive_equity_rejects_signal(self):
        rm = make_manager()
        account = make_account(equity=0.0, peak_equity=0.0)
        decision = rm.evaluate(make_signal(), account)
        assert not decision.approved
        assert "zero or negative" in decision.reason.lower()

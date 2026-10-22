"""Position-sizing rules: how much equity to risk on the NEXT trade.

Modes (cfg.sizing):
  fixed            - constant base_risk_pct of equity (the default).
  kelly            - fractional Kelly from a rolling window of recent trades.
  martingale       - multiply base after each consecutive LOSS (reset on win).
  anti_martingale  - multiply base after each consecutive WIN  (reset on loss).
  kelly_martingale - Kelly base, scaled by the martingale (loss-streak) multiplier.
  kelly_anti       - Kelly base, scaled by the anti-martingale (win-streak) mult.

Every mode returns a RISK FRACTION of equity (the amount lost if the stop is hit),
hard-capped by cfg.sizing_cap_pct. The engine applies leverage/notional caps on top.

Caveats (why the caps exist):
  - Martingale recovers small losing streaks but blows up on long ones; mart_max_steps
    bounds the geometric growth. Treat it as a cautionary baseline, not a recommendation.
  - Kelly needs a real, stable edge estimate; with few trades or a non-positive edge it
    falls back to the floor. Full Kelly is very high-variance, hence fractional (half).
"""
from statistics import mean


def kelly_fraction(pnls, min_trades: int = 10):
    """Edge-based fraction f = W - (1-W)/b from recent trade PnLs. None if unreliable."""
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]
    if len(pnls) < min_trades or len(wins) < 3 or len(losses) < 3:
        return None
    w = len(wins) / len(pnls)
    b = mean(wins) / mean(losses)
    if b <= 0:
        return None
    return max(0.0, w - (1.0 - w) / b)


def risk_fraction(cfg, win_streak: int, loss_streak: int, recent_pnls) -> float:
    """Risk fraction of equity for the next trade, per cfg.sizing. Hard-capped."""
    mode = cfg.sizing
    base = cfg.risk_pct / 100.0
    floor = cfg.risk_floor_pct / 100.0

    if "kelly" in mode:
        fk = kelly_fraction(recent_pnls, cfg.kelly_min_trades)
        if fk is None:
            base_frac = base
        else:
            base_frac = min(cfg.kelly_fraction * fk, cfg.kelly_cap_pct / 100.0)
        base_frac = max(base_frac, floor)
    else:
        base_frac = base

    mult = 1.0
    if "anti" in mode:                                   # anti-martingale: press wins
        mult = cfg.anti_mult ** min(win_streak, cfg.anti_max_steps)
    elif "martingale" in mode:                           # martingale: press losses
        mult = cfg.mart_mult ** min(loss_streak, cfg.mart_max_steps)

    return min(base_frac * mult, cfg.sizing_cap_pct / 100.0)

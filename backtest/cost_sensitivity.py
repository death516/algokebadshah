"""Cost & slippage sensitivity for the production regime strategy.

Costs are ALWAYS modelled in the engine (taker fee per side + slippage per fill);
this sweeps them to show the edge is not a low-cost artifact and survives realistic
-to-harsh Delta-testnet frictions (thin books slip more). Risk-managed config with
anti-martingale sizing (the production default). Reproduce:
    python -m backtest.cost_sensitivity
"""
import logging
from pathlib import Path

import pandas as pd

from app.strategy.trend import compute_signals
from backtest.data import load_candles
from backtest.engine import BTConfig, run_backtest
from backtest.metrics import compute_metrics
from backtest.run import PROD

logging.basicConfig(level=logging.ERROR)
OUT = Path(__file__).resolve().parent.parent / "backtest" / "results" / "costs"

# (label, taker_fee_pct per side, slippage_bps per fill)
SCENARIOS = [
    ("frictionless",        0.00,  0.0),
    ("maker/limit (0.02%/1bp)", 0.02, 1.0),
    ("default (0.03%/2bps)",  0.03,  2.0),
    ("taker (0.05%/2bps)",    0.05,  2.0),
    ("realistic Delta",      0.05,  5.0),
    ("conservative",         0.08, 10.0),
    ("harsh (thin testnet)", 0.10, 15.0),
    ("extreme",              0.15, 25.0),
]


def main():
    df_tf, df_htf = load_candles()
    sig = compute_signals(df_tf, df_htf, PROD)
    rows = {}
    for label, fee, slip in SCENARIOS:
        cfg = BTConfig(taker_fee_pct=fee, slippage_bps=slip, sizing="anti_martingale",
                       daily_loss_pct=3.0, max_dd_pct=10.0, dd_cooldown_days=2.0)
        trades, equity, state = run_backtest(sig, cfg)
        m = compute_metrics(trades, equity, state)
        # round-trip cost in bps = fee*2 (both sides, %) *100 + slip*2 (both fills)
        rt_bps = fee * 2 * 100 + slip * 2
        rows[label] = {
            "roundtrip_bps": round(rt_bps, 1),
            "return_%": m.get("total_return_pct"), "PF": m.get("profit_factor"),
            "sharpe": m.get("sharpe"), "maxDD_%": m.get("max_drawdown_pct"),
            "trades": m.get("trades"), "fees_$": m.get("total_fees_usd"),
            "final_$": m.get("final_equity"),
        }
    tbl = pd.DataFrame(rows).T
    OUT.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(OUT / "cost_sensitivity.csv")
    pd.set_option("display.width", 200)
    print("\n=== Cost & slippage sensitivity (regime strategy, risk-managed) ===")
    print(tbl.to_string())
    print("\nroundtrip_bps = fee(both sides) + slippage(both fills), in basis points")


if __name__ == "__main__":
    main()

"""Compare position-sizing rules on the chosen regime strategy.

All modes share the same strategy, signals, fees, caps and risk halts; only the
sizing rule changes. Runs under the realistic risk-managed config (daily -3%,
kill -10%, 2-day cooldown). Prints a table and saves a combined equity-curve PNG.
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
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backtest" / "results" / "sizing"
MODES = ["fixed", "kelly", "martingale", "anti_martingale", "kelly_martingale", "kelly_anti"]


def main():
    df_tf, df_htf = load_candles()
    sig = compute_signals(df_tf, df_htf, PROD)
    rows, curves = {}, {}
    for mode in MODES:
        cfg = BTConfig(sizing=mode, risk_pct=1.0,
                       daily_loss_pct=3.0, max_dd_pct=10.0, dd_cooldown_days=2.0)
        trades, equity, state = run_backtest(sig, cfg)
        m = compute_metrics(trades, equity, state)
        rows[mode] = {
            "return_%": m.get("total_return_pct"), "cagr_%": m.get("cagr_pct"),
            "PF": m.get("profit_factor"), "sharpe": m.get("sharpe"),
            "maxDD_%": m.get("max_drawdown_pct"), "win_%": m.get("win_rate_pct"),
            "trades": m.get("trades"), "kills": m.get("kill_switch_hits"),
            "final_$": m.get("final_equity"),
        }
        curves[mode] = equity["equity"]

    tbl = pd.DataFrame(rows).T
    OUT.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(OUT / "sizing_comparison.csv")
    _plot(curves, OUT / "sizing_comparison.png")
    pd.set_option("display.width", 200)
    print("\n=== Sizing comparison (regime strategy, risk-managed) ===")
    print(tbl.to_string())


def _plot(curves, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(11, 6))
    for mode, eq in curves.items():
        ax.plot(eq.index, eq.values, lw=1.0, label=mode)
    ax.set_title("Position sizing rules — equity curves (regime strategy, risk-managed)")
    ax.set_ylabel("equity (USD)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()

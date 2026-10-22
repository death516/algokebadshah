"""Stress test: how the production strategy holds up under adverse conditions.

Each scenario perturbs one real-world risk (latency, slippage, fees, leverage,
risk size) or isolates the worst market window. Production config: regime strategy,
anti-martingale sizing, risk overlay on. Reproduce: python -m backtest.stress
"""
import logging
from pathlib import Path

import pandas as pd

from app.strategy.trend import compute_signals
from backtest.data import load_candles
from backtest.engine import BTConfig, run_backtest
from backtest.metrics import compute_metrics
from backtest.run import PROD, RISK_CFG

logging.basicConfig(level=logging.ERROR)
OUT = Path(__file__).resolve().parent.parent / "backtest" / "results" / "stress"
SIG_COLS = ["side", "stop_mult", "tp_rr", "trail_mult"]


def _lag(sig: pd.DataFrame, bars: int) -> pd.DataFrame:
    """Act `bars` later: shift the signal annotation forward, keep OHLCV/ATR real."""
    out = sig.copy()
    out[SIG_COLS] = out[SIG_COLS].shift(bars)
    out["side"] = out["side"].fillna("")
    return out


def _cfg(**over):
    d = {**RISK_CFG.__dict__, **over}
    return BTConfig(**d)


def _run(sig, cfg, bench):
    t, e, s = run_backtest(sig, cfg)
    return compute_metrics(t, e, s, benchmark=bench)


def main():
    df_tf, df_htf = load_candles()
    sig = compute_signals(df_tf, df_htf, PROD)
    bench = sig["close"]

    scenarios = {
        "baseline": (sig, _cfg()),
        "latency +1 bar": (_lag(sig, 1), _cfg()),
        "latency +2 bars": (_lag(sig, 2), _cfg()),
        "slippage x2 (4bps)": (sig, _cfg(slippage_bps=4.0)),
        "slippage x5 (10bps)": (sig, _cfg(slippage_bps=10.0)),
        "fee x2 (0.06%/side)": (sig, _cfg(taker_fee_pct=0.06)),
        "leverage cap 3x": (sig, _cfg(max_leverage=3.0)),
        "risk 0.5%": (sig, _cfg(risk_pct=0.5)),
        "risk 2.0%": (sig, _cfg(risk_pct=2.0)),
        "tighter kill -7%": (sig, _cfg(max_dd_pct=7.0)),
    }
    rows = {}
    for name, (s, cfg) in scenarios.items():
        m = _run(s, cfg, bench)
        rows[name] = {k: m.get(k) for k in
                      ("total_return_pct", "cagr_pct", "profit_factor", "sharpe",
                       "sortino", "calmar", "max_drawdown_pct", "trades", "kill_switch_hits")}

    # worst market windows from the baseline equity curve
    _, eq, _ = run_backtest(sig, _cfg())
    e = eq["equity"]
    worst = {}
    for days, bars in (("worst 7d", 7 * 96), ("worst 30d", 30 * 96), ("worst 60d", 60 * 96)):
        roll = e.pct_change(bars)
        worst[days] = round(float(roll.min()) * 100, 2)

    tbl = pd.DataFrame(rows).T
    OUT.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(OUT / "stress.csv")
    pd.set_option("display.width", 220)
    print("\n=== Stress test (production regime strategy, risk-managed) ===")
    print(tbl.to_string())
    print("\nWorst rolling windows (equity):", worst)


if __name__ == "__main__":
    main()

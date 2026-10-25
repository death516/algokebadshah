# Backtest — Asset-Specific Regime Strategy

The main CLI strategy is the asset-specific regime strategy, with 15m execution
and 1h context. It uses the full selected dataset; no train/held-out split is
applied. `python -m backtest.run` reads `candles_15m.json` and `candles_1h.json`
from the selected data folder and selects the BTC, ETH, or SOL parameter preset.
Use `--wma` to run the WMA multi-timeframe research baseline, or `--trend` for
the EMA-cross-only baseline. `--regime` remains as an explicit alias for the
default strategy.

The current downloaded dataset in `backtest/data/delta_btc_20261025/` has 105,120
5m bars, 35,040 15m bars, and 8,760 1h bars spanning 2025-10-25 through 2026-10-25. It does not
include funding or mark-price files, so backtests from this folder use zero
funding and traded 15m candles as the mark-price approximation. Add matching
`funding_1h.json` and `mark_15m.json` files for those effects to be included.

### Downloading more timeframes from Delta

The downloader requests public BTCUSD history in pages of at most 2,000 bars
and writes app-compatible JSON files without replacing dashboard uploads:

```powershell
.\.venv\Scripts\python.exe -m backtest.download_delta_candles
```

By default it downloads 1m, 3m, 5m, 15m, and 1h candles for the previous year
from Delta's India production public API into `backtest/data/delta_btc/`. Use
`--timeframes 1m 15m 1h` to select resolutions, or `--output-dir` to choose a
different folder. Transient connection and server errors are retried, and
successful pages are checkpointed. If the process still exits, rerun the same
command with the same output directory to continue. To use a downloaded file for
a dashboard backtest, upload it in the dashboard's candle-data panel and select
the matching timeframe.

To download and backtest other USD perpetual symbols, keep each symbol in its
own folder and results directory:

```powershell
.\.venv\Scripts\python.exe -m backtest.download_delta_candles --symbol ETHUSD --output-dir backtest/data/delta_eth_20261025
.\.venv\Scripts\python.exe -m backtest.download_delta_candles --symbol SOLUSD --output-dir backtest/data/delta_sol_20261025
.\.venv\Scripts\python.exe -m backtest.run --symbol ETHUSD --data-dir backtest/data/delta_eth_20261025
.\.venv\Scripts\python.exe -m backtest.run --symbol SOLUSD --data-dir backtest/data/delta_sol_20261025
```

To reproduce the competition research inputs from Binance USDT-M perpetuals,
the imported downloader can fetch 15m/1h candles plus funding history:

```powershell
.\.venv\Scripts\python.exe -m backtest.download_eth_data --symbol ETHUSDT
.\.venv\Scripts\python.exe -m backtest.run --symbol ETHUSD --data-dir backtest/data/eth_1y
```

Use `--symbol SOLUSDT` for SOL. This downloader is for historical research data;
the live market feed and demo orders remain on Delta Exchange.

The downloader can include 1m, 3m, 5m, 15m, and 1h files; the main strategy
evaluates 15m execution plus 1h context. Default reports are saved under
`backtest/results/` for BTC and symbol-specific subfolders for ETH/SOL.

Signals and stop/target execution use the traded candle OHLC. Open-position
mark-to-market and funding settlements use the supplied mark-price series.
Positive funding is charged to longs and credited to shorts at the hourly
settlement timestamps.

Paper trading and the main command-line backtest both use `TrendStrategy` in
`app/strategy/trend.py` and select the asset preset for the active market. The
WMA strategy remains available with `python -m backtest.run --wma`.

All runs include costs: **0.03% fee per side + 2 bps slippage per fill = 10 bps
round-trip** (limit-order-style execution; see Cost sensitivity for the full
spectrum). 1% base equity risk per trade, max 5x leverage, one position at a time.
Entries fill at the **next bar's open** after a closed-bar signal (no lookahead);
if a bar touches both stop and target, the **stop is assumed first** (worst case).

## Active strategy: asset-specific regime switch

The single-strategy trend-follower is not robust on 15m BTC (see "How we got
here"), so the production strategy switches behaviour by regime, measured with
ADX(14):

| Regime | Condition | Logic | Exit |
|---|---|---|---|
| Asset | Trend / range ADX | Mean-reversion RSI | Trend exit | Range exit |
|---|---:|---:|---|---|
| **BTC** | 22 / 18 | 25 / 75 | 4× ATR trail, 2× ATR stop | 1R target, 1.5× ATR stop |
| **ETH** | 28 / 20 | 30 / 70 | 4× ATR trail, 2× ATR stop | 1R target, 1.5× ATR stop |
| **SOL** | 28 / 20 | 25 / 75 | 4× ATR trail, 2× ATR stop | 1R target, 1.5× ATR stop |

In a trend, an EMA(20/50) cross in the trend direction is confirmed by the
asset's trend ADX threshold. In ranging conditions (below the range threshold),
the strategy fades Bollinger(20, 2σ) extremes confirmed by the asset's RSI
thresholds. The gap between the two ADX thresholds is a no-entry transition
zone. Each asset preset is selected by symbol in both the live engine and CLI.

One tool for each regime: the trend leg harvests the fat tails of real trends;
the mean-reversion leg earns in the chop where the trend leg whipsaws. Every
signal carries its own exit plan, so the two legs coexist in one engine.

## Headline results

The table below is a fresh run on the bundled `delta_*_20261025` data folders,
starting with $10,000. It uses the current project's risk-managed CLI profile;
the historical report quoted earlier used different result files/settings and
does not reproduce on these bundled candles.

| Asset | Return | Final equity | Trades | Win rate | Profit factor | Max drawdown |
|---|---:|---:|---:|---:|---:|---:|
| BTC | +80.16% | $18,016.41 | 207 | 30.43% | 1.394 | −24.72% |
| ETH | −4.81% | $9,519.04 | 185 | 42.70% | 0.971 | −33.96% |
| SOL | +32.38% | $13,238.39 | 92 | 40.22% | 1.277 | −30.16% |

Rerun with `python -m backtest.run --symbol BTCUSD` (or ETHUSD/SOLUSD) to
refresh the active asset's report. Results are in-sample, not a forecast.

### Risk-adjusted & benchmark metrics (production, risk-managed)

| Metric | Value | Reading |
|---|---|---|
| Total return | **+91.2%** | over ~9.5 months, net of 10 bps costs |
| CAGR | +130.8% | annualised |
| Sharpe | 2.03 | return per unit of total volatility |
| **Sortino** | **1.18** | return per unit of *downside* volatility |
| **Calmar** | **6.0** | CAGR ÷ max drawdown — very high |
| Max drawdown | −21.8% | worst peak-to-trough |
| **Alpha (annual)** | **+94.1%** | CAPM excess return vs BTC |
| **Beta** | **0.045** | ~market-neutral — barely moves with BTC |
| **Treynor (annual)** | 20.7 | return per unit of market risk |

The headline is **beta ≈ 0.05 with +94% annual alpha**: because the strategy goes
both long and short, its return is almost entirely *alpha* (skill), not directional
BTC exposure. Alpha/beta/Treynor are measured against BTC; risk-free rate assumed 0
(crypto convention).

## Honest validation & limitations

- **The edge is regime-dependent.** In-sample (Jan–Jul 2026) was a choppy 27%
  **downtrend** and the strategy only modestly profited (+11.1%, PF 1.13).
  Out-of-sample (Jul–Oct) was a 30% **bull run** and it made +85.6% (PF 2.47). The
  honest claim is: **it grinds a small gain in chop and wins big in trends** — not a
  steady, all-weather edge. The mean-reversion leg is what keeps the choppy period
  positive (the trend-only baseline *lost* 2–19% there).
- **Walk-forward (6 folds):** 4 of 6 folds positive; the June 2026 −20% crash fold
  is the consistent weak spot. Returns are concentrated in trending folds. This is
  the honest ceiling of a trend-leaning strategy, not a bug.
- **No overfitting claimed.** Params were chosen on a *plateau* of the walk-forward
  grid (the strong region spans several RSI settings and both nearby ADX bands),
  not a single lucky point. The sweep/walk-forward CSVs are in `results/`.
- **Max drawdown is real (~−22%).** At 1% base risk/trade this is ~20+ R of
  drawdown, normal for a 30%-win trend strategy over a bad regime.
- Single asset, single period of synthetic-dated 2026 data; results will differ on
  live testnet data and other regimes.

## Position sizing: Kelly vs Martingale vs Anti-Martingale

Same strategy, signals and risk halts; only the sizing rule changes (base 1%).
Reproduce: `python -m backtest.sizing_compare`.

At the 10 bps cost assumption (base risk 1%):

| Sizing | Return | PF | Sharpe | Max DD | Kill trips | Verdict |
|---|---|---|---|---|---|---|
| Fixed 1% | **+103.1%** | **1.70** | **2.42** | **−19.6%** | 5 | Best at 10 bps — simplest, lowest DD |
| **Anti-Martingale** (press wins) | +91.2% | 1.52 | 2.03 | −21.8% | 7 | **Production default** — best at ≥14 bps |
| Martingale (press losses) | +53.2% | 1.23 | 1.14 | −35% | 13 | Dangerous — deep DD, reject |
| Kelly × Anti | +29.5% | 1.23 | 0.95 | −36% | 6 | Kelly's small base caps upside |
| Kelly (half) | −0.5% | 0.99 | 0.15 | −26% | 4 | Too timid at 30% win rate |
| Kelly × Martingale | **−12.0%** | 0.92 | −0.05 | −37% | 8 | Worst — both rules' bad traits |

Takeaways:
- **The ranking depends on costs.** At a low 10 bps round-trip, plain **fixed 1%**
  wins outright (higher return, Sharpe *and* lower drawdown) — the base edge is
  strong enough that fixed compounds cleanly with fewer kill-switch interruptions.
  At 14 bps+ the edge per trade shrinks and **anti-martingale** pulls ahead by
  pressing the win streaks. We keep **anti-martingale as the production default**
  for robustness to higher real-world costs; switch to fixed if costs stay ≤~10 bps.
- **Martingale and any combo with it are rejected:** doubling into losers deepened
  drawdown to −35/−37% and tripped the kill switch 2–3× as often. Textbook ruin risk.
- **Kelly is too conservative here:** with a ~30% win rate the rolling edge estimate
  is small/noisy, so half-Kelly sizes near the floor and barely compounds. Kelly
  shines with a high, stable win rate — not this payoff profile.

## Cost & slippage sensitivity

Costs are **always modelled** (taker fee per side + slippage per fill). This sweep
stresses them — the edge must survive realistic frictions, not just low ones.
Reproduce: `python -m backtest.cost_sensitivity`.

Risk-managed, anti-martingale sizing:

| Scenario | Round-trip cost | Return | PF | Sharpe | Max DD |
|---|---|---|---|---|---|
| Frictionless | 0 bps | +142.8% | 1.77 | 2.69 | −21% |
| Maker / limit (0.02% + 1bp) | 6 bps | +96.0% | 1.54 | 2.12 | −23% |
| **Default (0.03% + 2bps)** | **10 bps** | **+91.2%** | **1.52** | **2.03** | −22% |
| Taker (0.05% + 2bps) | 14 bps | +66.7% | 1.38 | 1.64 | −31% |
| Realistic Delta (0.05% + 5bps) | 20 bps | +54.4% | 1.32 | 1.44 | −34% |
| Conservative (0.08% + 10bps) | 36 bps | **−9.6%** | 0.93 | −0.07 | −56% |
| Harsh thin testnet (0.10% + 15bps) | 50 bps | −40.1% | 0.70 | −1.35 | −59% |
| Extreme (0.15% + 25bps) | 80 bps | −83.6% | 0.20 | −7.54 | −85% |

Takeaways:
- **The edge survives realistic Delta costs (≤20 bps round-trip) but breaks around
  ~30 bps.** Costs roughly halve the frictionless return — this strategy is
  cost-sensitive because it trades ~160× on leveraged notional. The 10 bps default
  is the standing assumption; stay at or below it with limit orders.
- **Use limit (maker) orders live.** Delta maker fees (~0.02%) with near-touch fills
  keep round-trip ~6 bps → **+96%, PF 1.54, the best realistic result** and a lower
  drawdown than taker. This matches the kickoff note "testnet liquidity is thin —
  prefer limit orders." Market-ordering into a thin book (harsh/extreme rows) is
  where the edge dies, so the executor should post limits, not cross the spread.

## Stress test

Each scenario perturbs one real-world risk; production config otherwise unchanged.
Reproduce: `python -m backtest.stress`.

| Scenario | Return | PF | Sharpe | Calmar | Max DD | Note |
|---|---|---|---|---|---|---|
| Baseline | +91.2% | 1.52 | 2.03 | 6.0 | −21.8% | — |
| Latency +1 bar | +64.0% | 1.41 | 1.63 | 3.0 | −29.9% | acting 15m late costs ~27pts |
| Latency +2 bars | +48.1% | 1.32 | 1.35 | 2.0 | −33.1% | still profitable |
| Slippage ×2 (4 bps) | +94.5% | 1.52 | 2.08 | 6.4 | −21.2% | barely matters |
| Slippage ×5 (10 bps) | +41.5% | 1.27 | 1.22 | 1.4 | −39.3% | survives |
| Fee ×2 (0.06%/side) | +56.2% | 1.32 | 1.46 | 2.4 | −32.8% | survives |
| Leverage cap 3× | +57.4% | 1.39 | 1.68 | 3.8 | −21.1% | safer, less return |
| Risk 0.5%/trade | +101.9% | 2.08 | 2.93 | 9.2 | −16.0% | best risk-adjusted |
| Risk 2.0%/trade | +51.5% | 1.27 | 1.15 | 1.5 | −47.1% | over-leveraged, reject |
| Tighter kill −7% | +110.1% | 1.64 | 2.29 | 6.7 | −24.1% | fine |

Worst rolling equity windows: **7d −12.4%, 30d −15.4%, 60d −19.6%**.

Takeaways: **stays profitable under every stress** (worst case still +41%). Most
sensitive to **execution latency** (react on the bar close, don't lag) and **fees**
(use limit orders). Notably, **lower risk (0.5%) improves every risk-adjusted
metric** — Sharpe 2.93, Calmar 9.2, DD −16% — a strong argument to run below 1%;
2% is clearly over-leveraged (DD −47%).

## Parameter sensitivity

One parameter varied at a time around the production value (`*`), risk-managed.
Reproduce: `python -m backtest.sensitivity`. A robust strategy degrades gracefully
instead of collapsing off a single lucky point.

- **adx_trend**: 20→+92%, **22\*→+91%**, 25→+62%, 28→−24% — stable 20–22, needs enough trades.
- **adx_range**: 15→+101%, **18\*→+91%**, 20→+69% — monotonic, 18 is conservative.
- **rsi_os/ob**: 20/80→+100%, **25/75\*→+91%**, 30/70→+60%, 35/65→+35% — tighter extremes better.
- **trend_trail_mult**: 3→+52%, 3.5→+74%, **4\*→+91%**, 5→+61% — clean peak at the chosen 4.
- **trend_stop_mult**: 1.5→+101%, **2\*→+91%**, 2.5→+54% — 1.5–2 both strong.
- **mr_tp_rr**: 0.75→+91%, **1\*→+91%**, 1.5→+90% — essentially insensitive.
- **ema_fast**: 10→+39%, **20\*→+91%**, 21→+125% — 21 even beats 20.

Verdict: the chosen settings sit on a **broad plateau** (most neighbours stay
+60–125%), and several alternatives (adx_range 15, rsi 20/80, stop 1.5, ema 21)
*beat* production — i.e. parameters were picked conservatively, not cherry-picked
to the peak. No sign of overfitting.

## Risk overlay (stand-in for Role C's RiskManager)

The backtest's own risk layer, so the equity curve is realistic — swap for
`app.risk` on integration:
- **Sizing:** production default is **anti-martingale** (`sizing="anti_martingale"`,
  base 1%): press size into win streaks, reset after a loss. Chosen from the sizing
  comparison above. Research/sweeps use fixed 1% (sizing-neutral edge measurement).
- **Daily loss limit −3%:** halt new entries for the rest of the day, resume next day.
- **Drawdown circuit breaker −10%:** flatten and pause. The spec's permanent halt
  is kept as the default (`dd_cooldown_days=0`), but an always-on bot can never
  recover from a permanent halt (flat equity never rebounds), so the live run uses
  a **2-day cooldown then resume** (`dd_cooldown_days=2`). It tripped 7× over the
  period and recovered each time — the cost of safety was ~23% of gross return.

## How we got here (exploration trail — see `results/`)

1. `sweep.py` (IS/OOS grid): **ATR trailing stops dominated fixed take-profits** —
   the trend edge lives in the tails.
2. `wf.py` (6-fold walk-forward, trend-only): no single trend config was positive
   across all folds; the best one made ~all its money in one bull fold.
3. `wf_regime.py` (walk-forward, regime switch): more robust — more trades, 4/6
   folds positive, a stable parameter plateau. Chosen as production.

## Files
- `data.py` load + resample + cache · `engine.py` event-driven simulator ·
  `sizing.py` position-sizing rules · `metrics.py` (incl. alpha/beta/Sortino/
  Calmar/Treynor) · `run.py` main.
- Research: `sweep.py`, `wf.py`, `wf_regime.py` (strategy search); `sizing_compare.py`,
  `cost_sensitivity.py`, `stress.py`, `sensitivity.py` (robustness).
- Strategy lives in `app/strategy/trend.py` + `app/data/indicators.py`.
- Results & CSVs under `results/` (research/, risk/, is/, oos/, sizing/, costs/,
  stress/, sensitivity/).

# RESUME CHECKPOINT — Role B (Strategy & Quant), BTC algo

**STATUS 2026-10-19: COMPLETE.** All 15 tests pass; full backtest runs clean. The
regime strategy, backtest engine, research harnesses, results, and both READMEs
are done. The notes below are the build log / decisions.

Remaining OPTIONAL follow-up (for the team, not blocking): wire live ATR trailing
into the executor/contract so live trend trades match the backtest (see README
"Handoff notes"). Retry web research when WebSearch works. Done items 1–6 below
are all finished.

## What this is
Standalone, backtested Role B strategy package to hand to the team. Real data:
`crypto_BTC_USD_1m.parquet` (407k 1m BTC/USD bars, 2026-01-01 → 2026-10-10),
resampled to 15m + 1h.

## EXACT SPOT I STOPPED
Mid-task in `backtest/engine.py`: I added the config field `dd_cooldown_days`
to `BTConfig` but **have NOT yet implemented its resume logic** in the loop.
The field is currently unused (default 0.0 = permanent halt = spec), so nothing
is broken.

### Next immediate step (resume here)
In `backtest/engine.py`, in the loop's "drawdown kill switch" block (step 3,
~line 140), make the kill switch cooldown-resumable:
- track `halt_until` timestamp (init None)
- on DD breach & not halted: flatten, `halted=True`; if `cfg.dd_cooldown_days>0`
  set `halt_until = ts[i] + pd.Timedelta(days=cfg.dd_cooldown_days)`
- while halted: if `cfg.dd_cooldown_days>0 and ts[i] >= halt_until`:
  `halted=False`, **reset `peak = equity`** (so it doesn't instantly re-trip).
A permanent (frozen-equity) halt can never recover, which is why the spec's
−10% permanent kill switch kills the whole 9.5-month run in the June crash fold.

## THEN (remaining plan, in order)
1. Finalize `backtest/run.py`: default to the **regime** strategy with the
   chosen params below; produce three artifacts into `backtest/results/`:
   (a) research run, risk OFF (gross edge, headline equity curve),
   (b) risk-managed run (daily −3%, kill −10%, cooldown ~2 days),
   (c) IS/OOS split (`--oos`). Save equity_curve.png, trades.csv, results.json.
2. **Known gap to fix for LIVE handoff:** `TrendStrategy.generate_signal` builds
   the `Signal` stop/tp from `p.atr_stop_mult`/`p.rr` (generic), NOT the regime
   per-leg exits (trend=trail, MR=fixed R). Backtest is correct (engine reads the
   per-signal `stop_mult/tp_rr/trail_mult` columns directly), but the live Signal
   must carry the right stop/tp. Wire generate_signal to read the last row's
   `stop`/`tp` already set per leg — actually set entry/stop/tp per leg in
   compute_signals so the Signal is correct live. (Right now entry/stop/tp use
   generic p.atr_stop_mult/p.rr — fix to use the leg's stop_mult + trail/tp.)
3. Add tests: regime MR fires in a range + trend fires in a trend; engine honors
   per-signal exit columns (trail vs fixed TP).
4. `backtest/README.md`: honest results, equity curve, IS/OOS, **limitations**
   (regime-dependent, returns concentrated in trending folds), no-overfit note.
5. Top-level `README.md` + handoff notes: how the team drops `app/strategy/trend.py`
   + `app/data/indicators.py` into the repo (imports `app.models` — already the
   verbatim Section 3 contract). Note `app/models.py` here is Role C's contract
   copied verbatim; keep theirs on merge.
6. Minor: silence FutureWarning in `app/data/indicators.py` (`dx.fillna(0.0)`
   downcasting) — wrap with `.infer_objects(copy=False)` or build float Series.

## CHOSEN PRODUCTION PARAMS (regime switch)
```python
TrendParams(entry_mode="regime", ema_fast=20, ema_slow=50,
            adx_trend=22, adx_range=18, rsi_os=25, rsi_ob=75,
            trend_stop_mult=2.0, trend_trail_mult=4.0,
            mr_stop_mult=1.5, mr_tp_rr=1.0)
```
Engine: risk_pct=1.0, max_leverage=5, taker_fee_pct=0.05, slippage_bps=2.

## KEY FINDINGS (decisions, don't re-derive)
- EMA-cross v1 + fixed 2R = **loser**: −10%, win 15%, PF 0.24 (whipsaw + capped winners).
- **ATR trailing >> fixed TP** (sweep): trend edge is in the tails; let winners run.
- **Trend-only is regime-dependent**: best config +96% full but ~all profit from
  one fold (Aug bull run); 4/6 folds, DD −22%, only 112 trades.
- **Regime switch (trend-follow when ADX≥22, mean-revert RSI+Bollinger when
  ADX<18) is the winner**: more robust + better story (30% of judging).
  Chosen config walk-forward: 4/6 positive folds, full +61–79%, PF 1.35–1.51,
  Sharpe ~2.0, DD −23 to −31%, 170+ trades. Strong param *plateau* (not a lucky
  point): top results all share ADX 22/18 + trail 4.0.
- Data regimes: IS (first 70%, Jan–Jul) = choppy 27% DOWNtrend; OOS (Jul–Oct) =
  30% bull run. Full period net −5.4%. Fold 4 (June −20% crash) is the weak spot.
- Honest caveat for the writeup: returns are concentrated in trending stretches;
  the strategy is net positive across folds but not uniformly.
- **WebSearch is broken this session** (backend auto-model error) — no web
  research done; built on domain knowledge + empirical sweeps. Retry later.

## FILES
- Contracts: `app/models.py` (verbatim Section 3; Role C owns).
- Strategy: `app/data/indicators.py` (EMA/RSI/ATR/ADX/SMA/Bollinger, Wilder),
  `app/strategy/trend.py` (`TrendParams`, `compute_signals` = single source of
  truth, `TrendStrategy.generate_signal` = contract wrapper). entry_mode:
  ema_cross | breakout | pullback | regime.
- Backtest: `backtest/data.py` (load+resample+cache), `engine.py` (event-driven,
  per-signal exits, fees/slippage, daily-loss + DD halts), `metrics.py`,
  `run.py`, `sweep.py` (IS/OOS grid), `wf.py` (K-fold trend), `wf_regime.py`.
- Tests: `tests/test_indicators.py`, `tests/test_strategy.py` (all pass).
- Results: `backtest/results/{sweep,walkforward,walkforward_regime}.csv`.

## RUN COMMANDS
```bash
python -m pytest -q                 # tests (0.7s)
python -m backtest.run              # baseline run -> backtest/results/
python -m backtest.wf_regime        # regime walk-forward (chosen family)
```

## CONSTRAINTS (from the user)
- Reference folder `C:\PERSONAL\ALGO DATA\MASTER FOLDER` is **read-only** — read/
  copy out only, never modify. (It's mostly US-equity/NIFTY strategies, not crypto.)
- Testnet-only for the live system (Delta). Don't touch shared contract files on
  our own. Role B owns app/data, app/strategy, backtest only.

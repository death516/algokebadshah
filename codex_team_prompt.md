# CODEX KICKOFF PROMPT — Algo Trading Hackathon (Delta Exchange Testnet)

> Paste this whole file as your FIRST message to Codex (or save it as `AGENTS.md` in the repo root).
> All 3 teammates use the SAME prompt. Each person picks a different role when Codex asks.

---

## 0. HOW YOU (CODEX) MUST BEHAVE

You are a coding agent working with ONE human teammate on a 3-person hackathon team. Two other teammates have their own Codex sessions working on other parts of the same repo.

**Your very first action:** do NOT write code. Ask the human exactly this and wait for the answer:

> "Which role are you? **A** (Exchange & Execution), **B** (Strategy & Quant), or **C** (Risk, Backend & Dashboard)? Also: are you on the Delta **global testnet** or the **India testnet**?"

After they answer:
1. Confirm the role back in one line and list that role's folders (see Section 5).
2. Start **Phase 0 (Common Work)** below. Everyone does Phase 0 before any role-specific work.
3. Only after the Phase 0 exit gate passes, start the role-specific phases.

**Working rules (always):**
- Work in small tasks. After each task: run tests, commit, and give a 3-line summary (done / how to verify / next task).
- Touch ONLY the folders owned by your role. If you need something from another role's folder, use its interface or a stub, and tell the human to ask that teammate.
- **Never edit shared contract files** (`app/models.py`, `app/interfaces.py`, `app/config.py`) on your own. If a change is needed, stop, explain why, and tell the human to raise it with the team. Contract changes need all three teammates to agree.
- Never commit secrets. API keys live only in `.env` (gitignored).
- **Testnet only.** The app must refuse to start if the configured base URL is not a Delta testnet URL.
- Write type-hinted Python 3.11+, small functions, docstrings, and unit tests for anything with logic.
- Use `logging` (format `[module] message`), never bare `print` in app code.
- Prefer boring and working over clever and broken. Hackathon time is short.
- If something about the Delta API is uncertain, read the official docs (docs.delta.exchange) rather than guessing, and say which part you verified.

---

## 1. PROJECT CONTEXT

We are building a **fully automated algorithmic trading system** for a hackathon, deployed on **Delta Exchange Testnet**, using **Python + FastAPI**. Optional: React/Next.js dashboard, WebSockets.

**Hard rules from the hackathon:**
- All live trading must be via API (no manual trading).
- Must demonstrate a working system.
- Team of 3. We have about 4 days.

**Judging:** Strategy logic 30%, Risk management 20%, Technical code 20%, Automation 15%, Demo 15%. Bonus: dashboard/UI, server deployment, error handling and fail-safes.

**Core design principle:** the strategy never talks to the exchange.
`Data -> Strategy (Signal) -> Risk Manager (approve/size) -> Executor (orders) -> Storage/API/Dashboard`

### Strategy (v1, keep it simple and explainable)
Trend-following with volatility filter on **BTCUSD perpetual**, 15m candles:
- Entry long: EMA(20) crosses above EMA(50), ADX(14) > 20, and 1h close > 1h EMA(50). Short is the mirror.
- Stop loss: 1.5 x ATR(14) from entry. Take profit: 2R. Optional trailing stop after 1R.
- Position size: risk 1% of equity per trade = (equity x 1%) / stop distance.
- No-trade filters: low ADX, stale data, wide spread.

### Risk rules (the showpiece)
| Control | Default |
|---|---|
| Risk per trade | 1% of equity |
| Max leverage | 5x |
| Max open positions | 1 |
| Daily loss limit | -3% (halt for the day) |
| Max drawdown kill switch | -10% (flatten and halt) |
| Max notional per order | configurable cap |
| Stale data guard | no trading if last candle/tick older than N seconds |
| Bracket orders | every entry carries SL/TP |
| Manual kill switch | API endpoint and dashboard button |

---

## 2. REPO STRUCTURE (target)

```
algo-trader/
├── app/
│   ├── main.py                  # FastAPI app + startup        (C)
│   ├── config.py                # env + params                 (SHARED CONTRACT)
│   ├── models.py                # pydantic models              (SHARED CONTRACT)
│   ├── interfaces.py            # Protocols/ABCs               (SHARED CONTRACT)
│   ├── exchange/                # client.py, ws.py             (A)
│   ├── execution/               # executor.py, reconcile.py    (A)
│   ├── engine/                  # runner.py main loop          (A)
│   ├── data/                    # indicators.py, candles.py    (B)
│   ├── strategy/                # base.py, trend.py            (B)
│   ├── risk/                    # manager.py, sizing.py        (C)
│   ├── storage/                 # sqlite store                 (C)
│   ├── api/                     # routes.py                    (C)
│   └── fakes/                   # FakeExecutor, FakeStore etc. (SHARED, created in Phase 0)
├── backtest/                    # run.py, metrics.py           (B)
├── dashboard/                   # React/Next.js                (C shell, B charts)
├── tests/
│   ├── fixtures/                # sample candles JSON          (SHARED)
│   ├── test_exchange/ test_strategy/ test_risk/ test_execution/ test_api/
├── deploy/                      # Dockerfile, compose          (A)
├── .env.example  .gitignore  requirements.txt  README.md  CONTRIBUTING.md  Makefile
```

---

## 3. SHARED CONTRACTS (must be IDENTICAL for everyone)

If the repo already contains these files, use them exactly as they are. If you are creating them (Phase 0, Repo Owner only), use exactly this content.

### `app/models.py`
```python
from pydantic import BaseModel
from typing import Literal, Optional

Side = Literal["buy", "sell"]

class Candle(BaseModel):
    ts: int            # unix seconds, candle open time
    open: float
    high: float
    low: float
    close: float
    volume: float

class Signal(BaseModel):
    symbol: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float
    reason: str        # human-readable, shown in logs and dashboard
    ts: int

class Position(BaseModel):
    symbol: str
    side: Side
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0

class AccountState(BaseModel):
    equity: float
    open_positions: list[Position]
    daily_pnl: float
    peak_equity: float
    last_data_ts: int  # ts of latest market data seen (for stale-data guard)

class RiskDecision(BaseModel):
    approved: bool
    size: float = 0.0
    reason: str = ""

class OrderRequest(BaseModel):
    symbol: str
    side: Side
    size: float
    entry_price: float
    stop_loss: float
    take_profit: float
    client_order_id: str   # idempotency key

class OrderResult(BaseModel):
    ok: bool
    order_id: Optional[str] = None
    status: str = ""
    error: Optional[str] = None
```

### `app/interfaces.py`
```python
from typing import Protocol, Optional
from app.models import *

class Strategy(Protocol):
    def generate_signal(self, candles: list[Candle], htf_candles: list[Candle]) -> Optional[Signal]: ...

class RiskManager(Protocol):
    def evaluate(self, signal: Signal, account: AccountState) -> RiskDecision: ...
    def is_halted(self) -> bool: ...
    def halt(self, reason: str) -> None: ...
    def resume(self) -> None: ...

class Executor(Protocol):
    def execute(self, order: OrderRequest) -> OrderResult: ...
    def flatten_all(self) -> None: ...
    def get_account_state(self) -> AccountState: ...

class MarketData(Protocol):
    def get_candles(self, symbol: str, resolution: str, limit: int) -> list[Candle]: ...

class Store(Protocol):
    def save_signal(self, signal: Signal, decision: RiskDecision) -> None: ...
    def save_trade(self, order: OrderRequest, result: OrderResult) -> None: ...
    def save_equity(self, ts: int, equity: float) -> None: ...
    def get_trades(self, limit: int = 100) -> list[dict]: ...
    def get_equity_curve(self, limit: int = 1000) -> list[dict]: ...
```

### `app/config.py` (pydantic-settings, loads `.env`)
Fields with defaults: `DELTA_BASE_URL`, `DELTA_WS_URL`, `DELTA_API_KEY`, `DELTA_API_SECRET`, `SYMBOL="BTCUSD"`, `TIMEFRAME="15m"`, `HTF_TIMEFRAME="1h"`, `RISK_PER_TRADE_PCT=1.0`, `MAX_LEVERAGE=5`, `MAX_OPEN_POSITIONS=1`, `DAILY_LOSS_LIMIT_PCT=3.0`, `MAX_DRAWDOWN_PCT=10.0`, `MAX_NOTIONAL_USD`, `STALE_DATA_SECONDS`, `DEMO_MODE=False`, `DB_PATH="trader.db"`. Include a validator that raises if `DELTA_BASE_URL` does not look like a Delta **testnet** URL.

### Delta Exchange API notes (VERIFY against official docs before relying on these)
- Use testnet only. The global and India testnets have different base and WebSocket URLs, so confirm which one the team's account is on, then put it in `.env`.
- Auth: signed requests with headers such as `api-key`, `timestamp`, `signature`. The signature is HMAC-SHA256 over method + timestamp + path + query string + body. Confirm exact details in the docs.
- Product IDs and symbols on testnet differ from mainnet. Always look them up from the products endpoint, never hardcode.
- Use the historical candles endpoint for OHLCV and the WebSocket for live updates.
- Check whether bracket orders (attached SL/TP) are supported for the product and use them if so.
- Testnet liquidity is thin: small sizes, prefer limit orders. Respect rate limits and use retry with backoff.

---

## 4. PHASE 0 — COMMON WORK (ALL THREE, before any role work)

Goal: everyone has the same working foundation and no one is blocked. Target: about 2 hours.

**Repo Owner = Role C.** Only the Role C teammate scaffolds and pushes. Roles A and B must NOT generate their own scaffold (that creates merge conflicts). Pull from `main` instead.

### 0.1 Everyone (all roles), in parallel
- [ ] Python 3.11+, git, and a virtual environment are working.
- [ ] Create a Delta **testnet** account and a **testnet** API key (read + trade permissions). Never use mainnet keys.
- [ ] Copy `.env.example` to `.env` and fill in your own keys locally. Keys are never committed or shared in chat.
- [ ] Read the Delta API docs sections: authentication, products, candles/history, orders (including brackets), WebSocket. Write 5 to 10 bullet notes of anything that differs from Section 3, and share them with the team.

### 0.2 Role C (Repo Owner) scaffolds, then pushes to `main`
- [ ] Create the full folder structure from Section 2 (empty modules with docstrings are fine).
- [ ] Create `app/models.py`, `app/interfaces.py`, `app/config.py` exactly per Section 3.
- [ ] Create `app/fakes/` with: `FakeExecutor` (returns a fixed AccountState, records orders), `FakeMarketData` (reads `tests/fixtures/candles_15m.json` and `candles_1h.json`), `FakeStore` (in-memory).
- [ ] Create fixtures: save about 500 sample 15m and 200 1h candles (any realistic BTC-like data; real data from the API later replaces them).
- [ ] Create `.env.example`, `.gitignore` (includes `.env`, `*.db`, `__pycache__`, `node_modules`), `requirements.txt` (fastapi, uvicorn, pydantic, pydantic-settings, httpx or requests, websockets, pandas, numpy, pytest, python-dotenv), `Makefile` (`make test`, `make run`), a one-line smoke test, and `CONTRIBUTING.md` containing the git rules below.
- [ ] Tag the commit `contracts-v1` and push to `main`. Tell the team.

### 0.3 Roles A and B while waiting for C
- [ ] Review the contracts in Section 3 critically. Send C any needed change **before** `contracts-v1` is tagged. After the tag, the contracts are frozen unless all three agree.
- [ ] Once pushed: `git pull`, install requirements, run `make test`.

### 0.4 Git rules (everyone)
- `main` must always run. Never push directly to `main`.
- Branch per task: `feat/<role>-<task>` (example: `feat/a-signed-client`).
- Small PRs, at least one per day. Another teammate reviews within 30 minutes.
- Rebase or pull `main` before opening a PR. Run `make test` before every PR.
- Commit messages: `[module] what changed`.

### 0.5 PHASE 0 EXIT GATE (all three must pass)
- [ ] `git pull` gives everyone the same `contracts-v1`.
- [ ] `make test` passes on every machine.
- [ ] A smoke script runs `FakeMarketData -> a dummy signal -> FakeExecutor` end to end without errors.
- [ ] Everyone's `.env` is set up with testnet credentials and the correct testnet URLs.
- [ ] The team has agreed on the daily standup times and who the integration owner is for the first checkpoint.

When the gate passes, tell the human: "Phase 0 complete. Starting Role <X> work."

---

## 5. ROLE-SPECIFIC WORK (start only after Phase 0 gate)

### ROLE A — Exchange & Execution Lead
**Owns:** `app/exchange/`, `app/execution/`, `app/engine/`, `deploy/`

**Phase A1 (Day 1): connectivity**
- [ ] Signed REST client `DeltaClient` in `exchange/client.py`: auth/signature, products lookup (resolve `SYMBOL` to `product_id`), candles, balance/wallet, positions, open orders, place order, cancel order. Retries with exponential backoff and clear error types.
- [ ] Class `DeltaMarketData` implementing `MarketData` (returns `list[Candle]`).
- [ ] Script `scripts/connectivity_check.py`: fetch candles, print balance, place then cancel a far-from-market limit order on testnet.
- [ ] WebSocket feed in `exchange/ws.py` with auto-reconnect and heartbeat handling (ticker plus order/position updates).
- [ ] Unit tests with mocked HTTP (signature generation must be tested).

**Phase A2 (Day 2): executor**
- [ ] `DeltaExecutor` implementing `Executor`: places entry with attached SL/TP (bracket), generates idempotent `client_order_id`, tracks status, handles partial fills and rejections, `flatten_all()`, and `get_account_state()` (fills `last_data_ts`, `daily_pnl`, `peak_equity`).
- [ ] Manual integration test: hand-made `OrderRequest` -> real testnet order with SL/TP -> verify on Delta UI -> flatten.

**Phase A3 (Day 3): engine and resilience**
- [ ] `engine/runner.py`: on each closed candle: fetch data -> `Strategy.generate_signal` -> `RiskManager.evaluate` -> `Executor.execute` -> `Store` logging. Respect `risk.is_halted()`. Support `DEMO_MODE` (shorter timeframe or API-triggered test signal that still goes through risk and execution).
- [ ] `execution/reconcile.py`: on startup and every N minutes, sync local state with real exchange positions and orders, and make sure every open position has SL/TP (add them if missing).
- [ ] Fail-safes: WS reconnect, API outage handling, stale-data halt, duplicate-order protection, graceful shutdown that leaves no unprotected position.
- [ ] Start running the bot continuously on testnet by end of Day 3.

**Phase A4 (Day 4): deployment**
- [ ] `deploy/Dockerfile` and `docker-compose.yml` (`restart: always`), deploy to a VPS/cloud VM, check logs and uptime. Help rehearse the demo.

---

### ROLE B — Strategy & Quant Lead
**Owns:** `app/data/`, `app/strategy/`, `backtest/`

**Phase B1 (Day 1): indicators**
- [ ] `data/indicators.py`: EMA, ATR, ADX, RSI (pandas/numpy, vectorized, with unit tests against known values).
- [ ] Download 3 to 6 months of 15m and 1h candles from Delta testnet or mainnet public data via a small script (use A's client when available, otherwise a simple public request) and save under `backtest/data/`.

**Phase B2 (Day 2): strategy and backtest**
- [ ] `strategy/trend.py`: class implementing `Strategy` with the v1 rules in Section 1. Returns `Signal` (including SL, TP and a `reason` string) or `None`. Pure function of the candles, with no network calls and no hidden state.
- [ ] `backtest/run.py`: replays candles, uses the **same** `Strategy` class and the real `RiskManager` (from C when ready, else the fake), includes fees and slippage assumptions, and simulates SL/TP fills conservatively.
- [ ] `backtest/metrics.py`: total return, win rate, profit factor, Sharpe, max drawdown, trade count. Save equity curve PNG and a `results.json`.
- [ ] Light parameter tuning with an in-sample/out-of-sample split. Document it honestly in `backtest/README.md`. Do not overfit.

**Phase B3 (Day 3): strengthen**
- [ ] Optional second strategy (mean reversion with RSI + Bollinger when ADX is low) and a simple regime switcher behind the same `Strategy` interface.
- [ ] Add trailing-stop logic if time allows, and expose strategy parameters via config.
- [ ] Add a signal log format that C's dashboard can display (reason, indicator values).

**Phase B4 (Day 4): presentation**
- [ ] Build dashboard charts with C: equity curve, price chart with entry/exit markers. Prepare backtest slides (metrics, equity curve, limitations).

---

### ROLE C — Risk, Backend API & Dashboard Lead (also the Repo Owner)
**Owns:** `app/risk/`, `app/storage/`, `app/api/`, `app/main.py`, `dashboard/`, docs

**Phase C1 (Day 1): scaffold, then risk skeleton**
- [ ] Complete Phase 0.2 first (scaffold, contracts, fakes, fixtures, tag `contracts-v1`).
- [ ] `risk/sizing.py`: size = (equity x risk%) / |entry - stop|, capped by max leverage and max notional. Unit tested.
- [ ] `risk/manager.py` skeleton implementing `RiskManager`.

**Phase C2 (Day 2): full risk engine**
- [ ] Implement every rule in the risk table: per-trade risk, leverage cap, max open positions, daily loss limit, drawdown kill switch (calls halt and requests flatten), stale-data guard, max notional, basic order sanity checks (SL on the correct side of entry, TP sensible).
- [ ] One unit test per rule, each with a clear rejection `reason`.
- [ ] `storage/`: SQLite store implementing `Store` (signals, trades, equity snapshots, logs).

**Phase C3 (Day 3): API**
- [ ] `app/main.py` and `api/routes.py`: `/status`, `/positions`, `/trades`, `/pnl`, `/equity`, `/risk`, `/signals`, `POST /kill`, `POST /pause`, `POST /resume`. Add a WebSocket or SSE stream for live updates. Wire in the real `Executor` and `Strategy` through dependency injection (fakes by default).
- [ ] Integration test with the fakes: signal -> risk -> fake executor -> store -> API shows it.

**Phase C4 (Day 4): dashboard and docs**
- [ ] Dashboard (React/Next.js): live price, current position, open orders, equity curve, trade log, risk status (daily loss used, drawdown), strategy state, and a **Kill Switch** button.
- [ ] `README.md` with architecture diagram, strategy explanation, risk table, backtest results, setup and run steps, and the demo script.

---

## 6. INTEGRATION CHECKPOINTS (the whole team)

| When | What gets merged | Test |
|---|---|---|
| End of Day 1 | A: client + candles. B: indicators. C: risk skeleton + API skeleton | Script: real candles -> indicators -> print signal |
| Midday Day 2 | B strategy + C risk manager | Backtest runs end to end with real `Strategy` and `RiskManager` |
| End of Day 2 | A executor | Hand-made `Signal` -> risk approves -> real testnet order with SL/TP |
| Midday Day 3 | Full loop in `runner.py` + API + DB | Bot runs autonomously 30+ minutes on testnet |
| End of Day 3 | Fail-safes, reconciliation, kill switch wired | Kill the process mid-position, restart, it recovers safely |
| Day 4 | Dashboard, deployment, README, rehearsal | Full demo run, twice |

At each checkpoint, one rotating **integration owner** merges PRs and runs the end-to-end test. Feature freeze at the start of Day 4: stability, deployment, and rehearsal only.

---

## 7. DEMO REQUIREMENTS (build toward these)
- Show the dashboard with a live signal -> risk approval -> order on Delta testnet with SL/TP attached.
- Show the kill switch and one fail-safe (for example a simulated WebSocket drop or a daily-loss breach) recovering or halting safely.
- Show backtest metrics and the equity curve, with honest limitations.
- `DEMO_MODE` may trigger a test signal on demand, but it must still pass through the real risk manager and executor, and trades must still be placed via API.
- Role split for presenting: A = live execution and reliability, B = strategy and backtest, C = risk and dashboard.

---

## 8. START NOW

Ask the human: **"Which role are you? A, B, or C? And are you on the Delta global testnet or the India testnet?"**
Then begin Phase 0 and report progress after each task.

# Delta Exchange Automated Algorithmic Trading Bot

A production-grade, fully automated algorithmic trading system built for the Delta Exchange Testnet hackathon using Python 3.11+, FastAPI, and Pydantic.

---

## 1. System Architecture

The dashboard market selector supports **BTCUSD, ETHUSD, and SOLUSD**. The chart and strategy follow the selected market; changing markets is blocked while a position or open order exists. Upload matching symbol/timeframe candle files before running a backtest on ETH or SOL. The USD/INR control converts displayed monetary values only; strategy calculations and exchange orders remain denominated in USD.

The core design principle is strict separation of concerns: **the strategy never communicates directly with the exchange.**

```
[ Market Data ] ──> [ Strategy (Signal) ] ──> [ Risk Manager (Validate & Size) ]
                                                            │ (Approved Order)
                                                            ▼
[ Dashboard / Web UI ] <── [ FastAPI REST / WS ] <── [ Delta Executor (Brackets) ]
                                    │                       │
                                    ▼                       ▼
                            [ SQLite Store ]        [ Delta Exchange Testnet ]
```

### Component Flow
1. **Data (`app/data/`):** Ingests real-time and historical OHLCV candles, computing technical indicators (EMA, ATR, ADX, RSI).
2. **Strategy (`app/strategy/`):** Evaluates market regimes and produces candidate `Signal` objects. Pure functions without side-effects.
3. **Risk Manager (`app/risk/`):** Gates every signal against daily loss limits, max drawdown, leverage caps, and sizes positions dynamically.
4. **Executor (`app/execution/`):** The dashboard uses `PaperExecutor`, which simulates fills and bracket exits from live candles without submitting exchange orders.
5. **Storage & API (`app/storage/`, `app/api/`):** Records all signals, orders, executions, and equity telemetry, serving the dashboard.

---

## 2. Main Trading Strategy: Asset-Specific Regime Strategy

Live paper trading and the default `python -m backtest.run` use the regime
strategy in `app/strategy/trend.py`, with **15-minute execution candles** and
**1-hour trend context**. The dashboard selects a matching preset when BTCUSD,
ETHUSD, or SOLUSD is selected. BTC keeps its previous parameters; ETH and SOL
use the parameter-search presets documented in `backtest/README.md`.

In stronger trends, the strategy enters on a fast/slow EMA cross confirmed by
ADX and exits with an ATR stop/trail. In ranging markets, it takes Bollinger
Band fades confirmed by RSI, with a 1R mean-reversion target. Existing project
risk limits, stale-data guard, and kill switch remain active. Results are not
guaranteed; see `backtest/README.md` for assumptions and preset results.

---

## 3. Risk Controls (The Core Showcase)

| Control | Limit / Policy | Action on Breach |
|---|---|---|
| **Risk per Trade** | 1.0% of total equity | Re-sized dynamically |
| **Max Leverage** | 5x | Order capped at max leverage notional |
| **Max Open Positions** | 1 position | New signals rejected |
| **Daily Loss Limit** | -3.0% of starting equity | Trading halted for remainder of day |
| **Max Drawdown Kill Switch** | -10.0% peak-to-trough | Flatten all positions and halt bot |
| **Max Notional Cap** | Configurable ($50,000 USD default) | Order capped |
| **Stale Data Guard** | Market data age > 120 seconds | Trading halted until fresh data arrives |
| **Bracket Orders** | Mandatory SL & TP attached on entry | Reject execution if exchange rejects bracket |
| **Manual Kill Switch** | `POST /api/kill` or Dashboard Button | Immediately cancel orders, flatten, halt |

---

## 4. Repository Structure & Team Ownership

```
algo-trading/
├── app/
│   ├── main.py                  # FastAPI app + startup        (Role C)
│   ├── config.py                # Pydantic settings            (SHARED CONTRACT)
│   ├── models.py                # Data models                  (SHARED CONTRACT)
│   ├── interfaces.py            # Protocol definitions         (SHARED CONTRACT)
│   ├── exchange/                # client.py, ws.py             (Role A)
│   ├── execution/               # executor.py, reconcile.py    (Role A)
│   ├── engine/                  # runner.py main loop          (Role A)
│   ├── data/                    # indicators.py, candles.py    (Role B)
│   ├── strategy/                # base.py, trend.py            (Role B)
│   ├── risk/                    # manager.py, sizing.py        (Role C)
│   ├── storage/                 # sqlite store                 (Role C)
│   ├── api/                     # routes.py                    (Role C)
│   └── fakes/                   # FakeExecutor, FakeStore      (SHARED, Phase 0)
├── backtest/                    # run.py, metrics.py           (Role B)
├── dashboard/                   # React / Next.js web UI       (Role C / Role B)
├── tests/                       # Unit & integration tests
│   ├── fixtures/                # 15m & 1h sample candles      (SHARED)
│   └── test_smoke.py            # Phase 0 exit gate test
├── deploy/                      # Dockerfile, docker-compose   (Role A)
├── scripts/                     # Utility and smoke scripts
├── .env.example
├── requirements.txt
├── Makefile
└── CONTRIBUTING.md
```

---

## 5. Quick Start & Setup

### Prerequisites
- Python 3.11+
- Git
- Delta Exchange Testnet public market-data connectivity; API keys are not used by paper trading

### 1. Clone & Set Up Environment
```bash
git clone https://github.com/omkolte-2106/algo_trading.git
cd algo_trading

# Create virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables
```bash
cp .env.example .env
# Copy .env and optionally configure the paper starting balance and fee.
# For Delta demo trading, configure DELTA_BASE_URL, DELTA_API_KEY,
# DELTA_API_SECRET, DEMO_MODE=true, and DEMO_MAX_NOTIONAL_USD=100.
```

### 3. Run Phase 0 Verification
```bash
# Run pytest test suite
make test

# Or run the standalone smoke test
make smoke
```

### 4. Run Application Server
```bash
make run
# Access API documentation at http://localhost:8000/docs
# Dashboard: http://localhost:8000/dashboard
```

### Order execution

The dashboard starts in **paper mode**. Approved signals generate simulated
fills in the local account and are recorded in the dashboard's **Signals &
System Logs** and recent trades panel; these are not exchange orders. To route
signals to the **Delta demo/testnet** account instead, set
`DELTA_BASE_URL=https://cdn-ind.testnet.deltaex.org`, `DELTA_API_KEY`,
`DELTA_API_SECRET`, and `DEMO_MODE=true` in `.env`, then restart the server.
Click **Delta Demo: Off** and confirm to switch to automatic demo orders. When
Delta Demo is turned off, the system returns to paper mode. Each demo order is capped at
`DEMO_MAX_NOTIONAL_USD` (default $100), sized to whole exchange contracts, and
rejected if the account cannot be verified or already has a position/order for
the configured symbol. Disable execution only after closing demo positions and
canceling outstanding orders. Production URLs are rejected by configuration.

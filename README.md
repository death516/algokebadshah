# Delta Exchange Automated Algorithmic Trading Bot

A production-grade, fully automated algorithmic trading system built for the Delta Exchange Testnet hackathon using Python 3.11+, FastAPI, and Pydantic.

---

## 1. System Architecture

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

## 2. Trading Strategy (BTCUSD Perpetual)

- **Instrument:** BTCUSD Perpetual (`15m` execution timeframe, `1h` higher timeframe trend filter)
- **Long Entry:**
  - `EMA(20)` crosses above `EMA(50)` on 15m.
  - `ADX(14) > 20` (volatility & trend filter).
  - Current 1h candle close > 1h `EMA(50)`.
- **Short Entry:** Mirror of long entry.
- **Stop Loss:** `1.5 x ATR(14)` from entry price.
- **Take Profit:** `2.0R` (Risk-to-Reward ratio of 1:2).
- **Position Sizing:** Risk `1%` of equity per trade:
  $$\text{Size} = \frac{\text{Equity} \times 1\%}{|\text{Entry} - \text{Stop Loss}|}$$

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
# The dashboard paper executor does not use exchange API credentials.
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

### Paper trading and backtest analysis

The dashboard starts in **Paper Trading** mode. It uses the live BTC candle feed
to simulate orders in a $10,000 account; it never sends orders to Delta Exchange.
Paper fills include a configurable fee (`PAPER_FEE_RATE`, default `0.0003` per
side) and exits are simulated from candle stop/target touches. The in-memory paper
account and its trade history reset when the server restarts. Set
`PAPER_START_EQUITY` to change the starting balance.

Choose **Backtest Analysis** and click **Run Backtest** to review the bundled
full-year BTC data, trade markers, full-period performance, and a chronological
70/30 in-sample / held-out report. The held-out period is more useful for
estimating how the strategy may generalize, but historical results do not
guarantee future performance.

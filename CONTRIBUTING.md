# Contributing Guidelines & Git Rules

Welcome to the Delta Exchange Algo Trading Hackathon repository!

To ensure seamless collaboration across our 3 teammates (Roles A, B, and C) and prevent merge conflicts, all contributors must strictly adhere to these rules.

---

## 1. Branching & PR Workflow

- **`main` branch is protected & always deployable:** `main` must always run without errors. Never push directly to `main` (except the initial `contracts-v1` tag by Role C).
- **Branch naming convention:**
  - Branch per task: `feat/<role>-<task>`
  - Examples:
    - `feat/a-signed-client`
    - `feat/b-indicators-ema`
    - `feat/c-sqlite-store`
- **Rebase before PR:** Always rebase on or pull latest `main` before opening a PR.
- **Verification gate:** You must run `make test` (or `python scripts/smoke_test.py`) before opening every PR. All tests must pass.
- **Review turnaround:** Teammates review PRs within 30 minutes. Small PRs at least once per day.

---

## 2. Commit Message Standards

Commit messages must clearly state the affected module and change:
```
[module] what changed
```
**Examples:**
- `[exchange] add HMAC-SHA256 signature generator`
- `[strategy] implement EMA crossover entry rules`
- `[risk] add daily loss limit check`
- `[api] wire kill switch endpoint`

---

## 3. Strict Folder Ownership

Work in small tasks and touch **ONLY** the folders owned by your role:

| Role | Title | Owned Folders / Files |
|---|---|---|
| **Role A** | Exchange & Execution Lead | `app/exchange/`, `app/execution/`, `app/engine/`, `deploy/` |
| **Role B** | Strategy & Quant Lead | `app/data/`, `app/strategy/`, `backtest/` |
| **Role C** | Risk, Backend & Dashboard (Repo Owner) | `app/risk/`, `app/storage/`, `app/api/`, `app/main.py`, `dashboard/` |

> If you need functionality from another role's folder, use its protocol interface or a stub, and coordinate with that teammate.

---

## 4. Shared Contract Lockdown

The following files define the team's shared contract:
- `app/models.py`
- `app/interfaces.py`
- `app/config.py`

**Never edit shared contract files unilaterally.**
If a modification is needed:
1. Stop work.
2. Explain why the change is necessary.
3. Propose the change to all teammates.
4. Changes require unanimous agreement from all three team members before modifying and tagging `contracts-v2`.

---

## 5. Security & Testnet Policy

- **No Secrets in Git:** Never commit API keys, secrets, or `.env` files. Place keys exclusively in local `.env` (gitignored).
- **Testnet Only:** The application must refuse to start if configured with a non-testnet URL.
- **Logging:** Use Python `logging` with format `[module] message`. Avoid raw `print` statements in application code.

# BinanceQuant

Language: English | [简体中文](README.zh-CN.md)

Automated crypto quant for Binance spot: BTC DCA core plus altcoin trend rotation. Uses valuation (AHR999, Z-Score) and trend gates (MA200, slope). Compatible with Binance flexible earn (auto redeem/subscribe), USDT buffer, BNB fuel, Telegram alerts, and Firestore state.

**Trend universe source:** Prefer the upstream published pool from CryptoLeaderRotation. This repo now validates upstream payload freshness and contract shape before using it, keeps a last known good upstream payload in state, and only reaches static fallback as an explicit degraded last resort.

**Workspace assumption:** Local replay and monitoring helpers expect the upstream repo to be checked out at `../CryptoLeaderRotation` unless you override the relevant path flags/env vars.

**Python runtime:** Prefer Python `3.11`. CI is pinned to 3.11, and local helper commands now prefer `python3.11` when available while still falling back to `python3`.

## Execution Engine Boundary

`BinanceQuant` is the downstream execution engine in this two-repo setup.

Upstream inputs it expects from `CryptoLeaderRotation`:

- validated monthly `live_pool.json` / `live_pool_legacy.json`
- publish metadata such as `as_of_date`, `version`, `mode`, `pool_size`, and `source_project`
- Firestore summary payloads that carry the same stable contract

Responsibilities owned here:

- upstream artifact freshness, schema, and contract validation
- degraded-mode fallback ladder and state persistence
- exchange execution, order safety checks, and balance handling
- circuit-breaker logic, runtime error handling, and minimal Telegram alerts
- fixed-input replay and operator smoke checks for the execution path

Non-goals:

- monthly research reporting
- upstream release review packages or artifact change narratives
- deep strategy-analysis commentary beyond the execution decision needed for this cycle

## Repo Shape

- `main.py` is the live orchestration entrypoint.
- `strategy_core.py` contains shared pure strategy logic.
- `research/` contains optional audit-only backtest tools and is not part of the hourly execution path.
- `run_*` scripts are local operators' helpers for fixed-input replay and maintenance.
- `tests/fixtures/` contains fixed inputs used by the replay regression tests.

## Layout

- **main.py** — Live script (run hourly).
- **strategy_core.py** — Shared pure strategy math used by live execution and research backtests.
- **runtime_support.py** — Runtime/report helpers shared by live execution and dry-run replay.
- **runtime_config_support.py** — Environment parsing and live-runtime bootstrap helpers so `main.py` can stay orchestration-focused.
- **market_snapshot_support.py** — Market snapshot assembly helpers used by the live cycle and replay regression tests.
- **degraded_mode_support.py** — Degraded-mode fallback ladder and trend-pool source state helpers.
- **live_services.py** — Firestore state and Telegram notification adapters for live operation.
- **exchange_support.py** — Spot balance, earn-buffer, and exchange quantity-format helpers.
- **trend_pool_support.py** — Upstream trend-pool contract parsing, validation, and fallback helpers.
- **trade_state_support.py** — Trade-state normalization and retired-position tracking helpers.
- **research/backtest.py** — Optional audit-only backtest / strategy-comparison runner; not part of the live execution contract.
- **run_cycle_replay.py** — Fixed-input dry-run executor for one full strategy cycle using local fixtures.
- **requirements.txt** — Human-maintained top-level Python deps.
- **requirements-lock.txt** — Pinned dependency set used by CI/deploy when present.

Generated local outputs under `reports/` are intentionally not committed. Fixed-input fixtures under `tests/fixtures/` are committed because they are part of the dry-run regression harness.

## Strategy Overview

- **BTC core:** Valuation-based DCA (AHR999) and scaled take-profit (Z-Score vs dynamic threshold). Target weight grows with equity.
- **Trend layer:** Monthly refreshed pool (upstream or internal fallback logic), then Top 2 by relative-BTC strength, inverse-vol weighted. Only active when BTC gate is on.

Runs hourly; signals are daily trend and risk, not high-frequency.

## BTC Core

**Indicators:** MA200, MA200 slope, AHR999, Z-Score, dynamic Z-Score sell threshold.

**Logic:** Stronger DCA when AHR999 low; normal when neutral; scaled sells when Z-Score above threshold. Higher Z-Score → larger sell fraction.

**Target weight:** `btc_target_ratio = 0.14 + 0.16 * ln(1 + total_equity / 10000)`, capped. Larger equity → more BTC, less trend.

**DCA size:** Daily base order scales with total equity.

## Trend Rotation

**Universe:** Prefer the upstream live pool. Source hierarchy is: fresh upstream Firestore payload → last known good upstream payload from state → validated local upstream file fallback → static universe emergency fallback.

**Official input pool:** Upstream publishes a 5-coin production pool; this repo consumes that pool as the monthly official input set.

**Actual rotation target:** The live trend sleeve still acts only on the final top-2 rotation decision, or on a defensive "no candidate / keep current stance" outcome when no symbol qualifies.

**Factors:** SMA20/60/200, 20/60/120d returns, 20d vol, ATR14, 30/90/180d avg quote volume, trend persistence, relative BTC strength, risk-adjusted momentum.

**Holdings:** Top 2 from pool by relative-BTC score; inverse-vol weights.

**Entry:** BTC gate on; price above SMA20/60/200; positive relative-BTC score; positive absolute momentum.

**Exit:** Below SMA60; ATR trailing stop; rotated out of Top 2.

## Risk

- **BTC gate:** Trend layer only when `BTC price > MA200` and `MA200 slope > 0`.
- **Circuit breaker:** If trend-layer daily PnL ≤ threshold, flatten trend book; BTC core unchanged.
- **BNB:** Auto top-up for fees; not in trend rotation.

## Earn Compatibility

- Check spot before orders; redeem from flexible earn if needed.
- Maintain USDT spot buffer (subscribe excess, redeem shortfall).

## State (Firestore)

- Trend positions, high-water prices, circuit state, DCA last buy/sell date, monthly pool id, pool symbols. Retired symbols (dropped from pool but still held) tracked until closed.

## Upstream Pool

**Default:** CryptoLeaderRotation monthly output.

1. Firestore `strategy` / `CRYPTO_LEADER_ROTATION_LIVE_POOL` (override: `TREND_POOL_FIRESTORE_COLLECTION`, `TREND_POOL_FIRESTORE_DOCUMENT`).
2. Last known good upstream payload persisted in Firestore state after a successful accepted upstream read.
3. Local `live_pool_legacy.json` or `live_pool.json` style file (override: `TREND_POOL_FILE`).
4. Static `TREND_UNIVERSE` as emergency fallback only.

**Stable upstream contract fields:**

- `as_of_date`
- `version`
- `mode`
- `pool_size`
- `symbols`
- `symbol_map`
- `source_project`

**Accepted legacy-compatible format (`live_pool_legacy.json`):**

```json
{
  "as_of_date": "2026-03-13",
  "version": "2026-03-13-core_major",
  "mode": "core_major",
  "pool_size": 5,
  "symbols": {
    "TRXUSDT": {"base_asset": "TRX"},
    "ETHUSDT": {"base_asset": "ETH"},
    "BCHUSDT": {"base_asset": "BCH"},
    "NEARUSDT": {"base_asset": "NEAR"},
    "LTCUSDT": {"base_asset": "LTC"}
  },
  "symbol_map": {
    "TRXUSDT": {"base_asset": "TRX"},
    "ETHUSDT": {"base_asset": "ETH"},
    "BCHUSDT": {"base_asset": "BCH"},
    "NEARUSDT": {"base_asset": "NEAR"},
    "LTCUSDT": {"base_asset": "LTC"}
  },
  "source_project": "crypto-leader-rotation"
}
```

In runtime output, keep these layers separate:

- upstream official pool: the monthly symbols accepted from the upstream contract
- current monthly execution pool: the local pool retained for this month before final buy gating
- current execution targets: the symbols that actually qualify for this cycle
- degraded source status: whether the cycle is using fresh upstream, last-known-good, local-file, or static fallback

**Validation and degraded mode:**

- Upstream payloads must have a non-empty symbol set, a parseable `as_of_date`, and an acceptable `mode`.
- Freshness is validated with `TREND_POOL_MAX_AGE_DAYS` against the upstream `as_of_date`.
- If the fresh upstream payload is stale or malformed, the runtime does not silently treat weaker fallbacks as equivalent.
- In degraded mode, the script prefers the last known good upstream payload, then a validated local file fallback, and pauses new trend buys by default unless `TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED=1`.
- Retired symbols stay in state until sold; active pool changes are source-tagged in state for auditability.

## Environment

Required:

| Variable | Description |
|----------|-------------|
| `BINANCE_API_KEY` | Binance API key |
| `BINANCE_API_SECRET` | Binance API secret |
| `TG_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Telegram chat ID for alerts |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON (or use `GCP_SA_KEY` and write to `gcp-key.json` before run) |

Optional:

| Variable | Description |
|----------|-------------|
| `BTC_STATUS_REPORT_INTERVAL_HOURS` | Interval for BTC status report (default 24) |
| `TREND_POOL_FILE` | Path to `live_pool_legacy.json` |
| `TREND_POOL_FIRESTORE_COLLECTION` | Firestore collection for live pool (default `strategy`) |
| `TREND_POOL_FIRESTORE_DOCUMENT` | Firestore document for live pool (default `CRYPTO_LEADER_ROTATION_LIVE_POOL`) |
| `TREND_POOL_MAX_AGE_DAYS` | Max allowed age for upstream `as_of_date` before payload is treated as stale (default `45`) |
| `TREND_POOL_ACCEPTABLE_MODES` | Comma-separated allowed upstream modes (default `core_major`) |
| `TREND_POOL_EXPECTED_SIZE` | Expected upstream live-pool size for contract checks (default `5`) |
| `TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED` | Allow trend buys when running on last-known-good or fallback pool sources (default `false`) |

## Deploy (self-hosted runner + workflow)

The repo is intended to run on a **self-hosted GitHub Actions runner** (e.g. a VPS). The runtime workflow checks out code, installs dependencies, writes GCP credentials into a runner temp file inside the execution step, removes that file automatically on exit, then runs `main.py`. The runner is expected to receive `workflow_dispatch` requests from one external scheduler instead of relying on GitHub's built-in hourly scheduler.

### 1. Self-hosted runner

- In the repo: **Settings → Actions → Runners**, add a new self-hosted runner (Linux recommended).
- On the machine (e.g. Oracle Cloud VPS): install the runner, register it, and keep it running so it can pick up jobs.

### 2. Workflow and runtime trigger

- **`.github/workflows/ci.yml`** is the push/manual validation workflow. It runs on GitHub-hosted runners and is limited to install/compile/test checks.
- **`.github/workflows/main.yml`** is the runtime workflow. It runs on the self-hosted runner, prepares the local `venv`, writes a temporary GCP credential file inside the execution step, runs `venv/bin/python main.py`, and cleans the temp file via shell trap.
- **Triggers:** `ci.yml` runs on `push` to `main` and `workflow_dispatch`; `main.yml` runs on `workflow_dispatch` only.
- **Runtime cadence:** GitHub Actions no longer schedules hourly runtime execution for this repo. The expected production model is one external scheduler, such as VPS cron + `curl`, calling the GitHub `workflow_dispatch` API for `main.yml`.
- Use `Actions -> Runtime -> Run workflow` for one-off manual runs.
- If you automate dispatch from a VPS, keep a single scheduler of record and avoid firing a new dispatch while the previous runtime job is still running.

Example API dispatch:

```bash
curl -L \
  -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/<owner>/<repo>/actions/workflows/main.yml/dispatches \
  -d '{"ref":"main"}'
```

### 3. Repository secrets

In **Settings → Secrets and variables → Actions**, add:

| Secret | Description |
|--------|-------------|
| `BINANCE_API_KEY` | Binance API key |
| `BINANCE_API_SECRET` | Binance API secret |
| `TG_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Telegram chat ID |
| `GCP_SA_KEY` | Full JSON content of the GCP service account key (written by the runtime workflow to a temp file and exported as `GOOGLE_APPLICATION_CREDENTIALS` only for the strategy step) |

The runtime workflow passes these into the “执行交易策略” step; it does not use a `.env` file on the runner.

### 4. GCP / Firestore

- The service account in `GCP_SA_KEY` must have **Firestore** access (read/write) for the project that hosts the Firestore database used by this app.
- **Invalid grant / account not found:** Usually means the key is for a deleted or wrong service account, or the key is from another project. Re-create a key for the correct account in the same project as Firestore and update the `GCP_SA_KEY` secret.

### Local run (optional)

For local testing only:

```bash
cd /path/to/BinanceQuant
python3 -m venv venv && source venv/bin/activate
REQ_FILE="requirements-lock.txt"
if [ ! -f "$REQ_FILE" ]; then REQ_FILE="requirements.txt"; fi
pip install -r "$REQ_FILE"
export BINANCE_API_KEY=... BINANCE_API_SECRET=... TG_TOKEN=... TG_CHAT_ID=...
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json
python main.py
```

### Local research backtest

For audit-only local research work outside the live execution path:

```bash
python3 -m research.backtest
```

### Fixed-input cycle replay

To execute one fully dry-run strategy cycle from local fixtures, without live Binance or Firestore writes:

```bash
python3 run_cycle_replay.py --run-id local-check
```

This emits a structured JSON report with:

- selected upstream pool and execution candidates
- trend buy/sell intents
- BTC DCA intents
- earn subscribe/redeem intents
- suppressed vs executed side-effect counts

### Tests

Committed unit tests can be run with:

```bash
python3 -m unittest \
  tests.test_strategy_core \
  tests.test_cycle_replay_runtime \
  tests.test_market_snapshot_support \
  tests.test_trend_pool_loading \
  -v
```

Repository hygiene:

- ignore local/runtime artifacts such as `reports/`, `venv/`, `.venv/`, `gcp-key.json`, and `.venv_requirements_hash`
- keep `tests/fixtures/` tracked so the cycle replay stays reproducible across machines

Operational behavior for degraded mode, Firestore failures, Binance API failures, Telegram failures, and workflow secret handling is documented in `docs/operator_runbook.md`.

## Notes

- The upstream CryptoLeaderRotation project is the primary selector and contract owner for the monthly live pool.
- Local stable-quality pool ranking logic in this repo remains as a runtime fallback and execution convenience, not the preferred healthy input.
- Monthly research reporting, shadow candidate evaluation, and monthly release review should live in the upstream CryptoLeaderRotation project, not in this downstream execution repo.

## Telegram

Alerts: trend buys/sells, BTC DCA, earn redeems, circuit breaker, errors. Optional periodic BTC status (AHR999, Z-Score, gate, trend PnL). Default once per day at UTC 00:00; set `BTC_STATUS_REPORT_INTERVAL_HOURS` to change.

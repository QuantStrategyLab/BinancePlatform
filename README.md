# BinanceQuant

Automated crypto quant for Binance spot: BTC DCA core plus altcoin trend rotation. Uses valuation (AHR999, Z-Score) and trend gates (MA200, slope). Compatible with Binance flexible earn (auto redeem/subscribe), USDT buffer, BNB fuel, Telegram alerts, and Firestore state.

**Trend universe source:** Prefer the upstream published pool from CryptoLeaderRotation. This repo now validates upstream payload freshness and contract shape before using it, keeps a last known good upstream payload in state, and only reaches static fallback as an explicit degraded last resort.

## Layout

- **main.py** — Live script (run hourly).
- **shadow_replay.py** — Local end-to-end shadow replay for the trend sleeve using historical upstream artifacts.
- **requirements.txt** — Python deps.

## Strategy Overview

- **BTC core:** Valuation-based DCA (AHR999) and scaled take-profit (Z-Score vs dynamic threshold). Target weight grows with equity.
- **Trend layer:** Monthly refreshed pool (upstream or internal stable-quality rank), then Top 2 by relative-BTC strength, inverse-vol weighted. Only active when BTC gate is on.

Runs hourly; signals are daily trend and risk, not high-frequency.

## BTC Core

**Indicators:** MA200, MA200 slope, AHR999, Z-Score, dynamic Z-Score sell threshold.

**Logic:** Stronger DCA when AHR999 low; normal when neutral; scaled sells when Z-Score above threshold. Higher Z-Score → larger sell fraction.

**Target weight:** `btc_target_ratio = 0.14 + 0.16 * ln(1 + total_equity / 10000)`, capped. Larger equity → more BTC, less trend.

**DCA size:** Daily base order scales with total equity.

## Trend Rotation

**Universe:** Prefer the upstream live pool. Source hierarchy is: fresh upstream Firestore payload → last known good upstream payload from state → validated local upstream file fallback → static universe emergency fallback.

**Monthly pool:** Upstream publishes a 5-coin production pool; this repo consumes it. “Stable quality” favours: stable trend structure, relative BTC strength, liquidity, low liquidity variance, trend persistence.

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
    "ETHUSDT": {"base_asset": "ETH"}
  },
  "symbol_map": {
    "TRXUSDT": {"base_asset": "TRX"},
    "ETHUSDT": {"base_asset": "ETH"}
  },
  "source_project": "crypto-leader-rotation"
}
```

**Validation and degraded mode:**

- Upstream payloads must have a non-empty symbol set, a parseable `as_of_date`, and an acceptable `mode`.
- Freshness is validated with `TREND_POOL_MAX_AGE_DAYS` against the upstream `as_of_date`.
- If the fresh upstream payload is stale or malformed, the runtime does not silently treat weaker fallbacks as equivalent.
- In degraded mode, the script prefers the last known good upstream payload, then a validated local file fallback, and pauses new trend buys by default unless `TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED=1`.
- Retired symbols stay in state until sold; active pool changes are source-tagged in state for auditability.
- A bounded soft sizing tilt from optional upstream `selection_meta` is available behind flags only; it is not enabled by default.

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
| `TREND_POOL_SOFT_TILT_ENABLED` | Enable optional bounded sizing tilt from upstream `selection_meta` (default `false`) |
| `TREND_POOL_SOFT_TILT_FIELD` | Upstream `selection_meta` field to use for tilt, e.g. `final_score` |
| `TREND_POOL_SOFT_TILT_STRENGTH` | Bounded tilt strength, typically small like `0.10` to `0.20` |

## Deploy (self-hosted runner + workflow)

The repo is intended to run on a **self-hosted GitHub Actions runner** (e.g. a VPS). The workflow checks out code, installs dependencies, writes GCP credentials from a secret to `gcp-key.json`, then runs `main.py`. No manual “download and cron on your PC” flow.

### 1. Self-hosted runner

- In the repo: **Settings → Actions → Runners**, add a new self-hosted runner (Linux recommended).
- On the machine (e.g. Oracle Cloud VPS): install the runner, register it, and keep it running so it can pick up jobs.

### 2. Workflow and schedule

- **`.github/workflows/main.yml`** defines the job: checkout → write `gcp-key.json` from secret → create/update venv and install deps → run `venv/bin/python main.py`.
- **Triggers:** `push` to `main` (job runs; strategy step runs only on `workflow_dispatch` or `schedule`), and optionally **schedule** (e.g. hourly) so the strategy runs periodically without a push.
- To run the strategy on a schedule, add `schedule` to the workflow `on:` block, for example:

```yaml
on:
  push:
    branches: [ main ]
  workflow_dispatch:
  schedule:
    - cron: '0 * * * *'   # every hour at :00
```

- The “执行交易策略” step is gated by `if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule'`, so it does not run on every push unless you change that condition.

### 3. Repository secrets

In **Settings → Secrets and variables → Actions**, add:

| Secret | Description |
|--------|-------------|
| `BINANCE_API_KEY` | Binance API key |
| `BINANCE_API_SECRET` | Binance API secret |
| `TG_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Telegram chat ID |
| `GCP_SA_KEY` | Full JSON content of the GCP service account key (written by the workflow to `gcp-key.json` as `GOOGLE_APPLICATION_CREDENTIALS`) |

The workflow passes these into the “执行交易策略” step; it does not use a `.env` file on the runner.

### 4. GCP / Firestore

- The service account in `GCP_SA_KEY` must have **Firestore** access (read/write) for the project that hosts the Firestore database used by this app.
- **Invalid grant / account not found:** Usually means the key is for a deleted or wrong service account, or the key is from another project. Re-create a key for the correct account in the same project as Firestore and update the `GCP_SA_KEY` secret.

### Local run (optional)

For local testing only:

```bash
cd /path/to/BinanceQuant
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export BINANCE_API_KEY=... BINANCE_API_SECRET=... TG_TOKEN=... TG_CHAT_ID=...
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json
python main.py
```

### Local shadow replay

To replay the downstream trend sleeve against locally generated upstream shadow releases:

```bash
python3 shadow_replay.py --release-index ../CryptoLeaderRotation/data/output/shadow_releases/release_index.csv --name baseline
```

## Notes

- The upstream CryptoLeaderRotation project is the primary selector and contract owner for the monthly live pool.
- Local stable-quality pool ranking logic in this repo remains as a runtime fallback and execution convenience, not the preferred healthy input.
- `shadow_replay.py` is the additive end-to-end research path for the downstream trend sleeve. It uses historical upstream shadow-release artifacts plus local daily price history; it does not require live Firestore or Binance connectivity.

## Telegram

Alerts: trend buys/sells, BTC DCA, earn redeems, circuit breaker, errors. Optional periodic BTC status (AHR999, Z-Score, gate, trend PnL). Default once per day at UTC 00:00; set `BTC_STATUS_REPORT_INTERVAL_HOURS` to change.

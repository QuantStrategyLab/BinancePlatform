# Operator Runbook

## Scope

This runbook covers the live execution path in `BinancePlatform`.

Primary entrypoints:

- `main.py` for live hourly execution
- `.github/workflows/main.yml` for self-hosted manual/API-triggered runs
- `run_cycle_replay.py` for fixed-input dry-run replay

Supporting modules with operational impact:

- `runtime_config_support.py` for runtime env parsing and bootstrap
- `degraded_mode_support.py` for trend-pool fallback ladder and source metadata
- `trend_pool_support.py` for upstream payload validation
- `live_services.py` for Firestore and Telegram adapters

## Execution Boundary

`BinancePlatform` is the downstream execution engine.

It is responsible for:

- consuming upstream live-pool artifacts and Firestore summary payloads
- validating freshness, contract shape, and fallback eligibility
- preserving the accepted upstream `symbols` order when passing the pool into strategy code
- executing orders, persisting runtime state, and emitting minimal operator alerts

It is not responsible for:

- monthly research reporting
- monthly live-pool selection, ranking, or local reranking
- upstream release summaries or review packages
- maintaining a second copy of the upstream publish narrative

## Normal Live Flow

1. Load runtime credentials and Firestore state.
2. Resolve the upstream ordered strategy artifact in this order:
   - fresh upstream Firestore payload
   - last known good upstream payload from state
   - validated local upstream file fallback
   - built-in static universe as last resort
3. Refresh trend-pool metadata in state.
4. Capture Binance balances and market snapshots.
5. Run trend rotation, BTC DCA, and earn-buffer maintenance.
6. Persist updated state and notifications.

Runtime output should stay operational:

- current upstream source and degraded status
- upstream official pool order and current local execution pool logged as separate concepts
- current execution targets and intents
- explicit gating / no-trade reasons and side-effect suppression counts
- zero-trade diagnostics grouped by BTC core / trend sleeve and gate
- exceptions, circuit breakers, and alert-worthy failures

The monthly execution pool is locked to the accepted upstream `version` / `as_of_date`. It refreshes when upstream release metadata changes and otherwise reuses the accepted ordered artifact pool; BinancePlatform should not rebuild the monthly pool with local ranking logic.

## Runtime Trigger Model

- `main.yml` is `workflow_dispatch` only.
- GitHub Actions no longer owns the hourly cadence for runtime execution in this repo.
- Production cadence should come from one external scheduler, for example VPS cron calling the GitHub Actions dispatch API.
- The VPS dispatch guard retries bounded transient failures such as network errors and GitHub `500`/`502`/`503`/`504`, but still alerts immediately for configuration and permission failures.
- Runtime Heartbeat writes a structured `qsl.runtime_heartbeat_assessment.v1` record to its workflow log. A completed Runtime failure is still escalated immediately, while one missing external dispatch is recorded as `DEFERRED`; it only alerts after `RUNTIME_HEARTBEAT_MAX_CONSECUTIVE_MISSES` expected intervals (default: two). A recent in-progress dispatch is `PARKED` until it completes.
- Repository variable changes are consumed by the next externally scheduled dispatch; they do not reconfigure the VPS scheduler cadence.
- Avoid overlapping dispatches from multiple schedulers or from a second manual run while the current runtime job is still in progress.

### Runner security boundary

The current production runtime still uses a persistent self-hosted runner. Treat this as a temporary, higher-risk boundary until the runtime moves to an ephemeral runner or an isolated Cloud Run Job:

- dedicate the runner to the dispatch-only `main.yml` runtime; do not run pull-request or untrusted branch jobs on it;
- restrict the Binance API key to the required trading scope, disable withdrawals, and apply an IP allowlist where the account supports it;
- keep the `binance-runtime` environment protection and reviewed OIDC identity contract in place;
- rebuild the runner after suspected compromise instead of trusting cached workspaces or virtual environments.

The broker job has repository read permission only. Successful execution reports are transferred to a separate GitHub-hosted job, which alone receives `contents: write` for the `logs` branch. This prevents the broker credentials and repository write token from sharing one job, but it does not make a persistent runner equivalent to an ephemeral one.

## Degraded Mode Ladder

Healthy mode:

- Source is `fresh_upstream`
- New trend entries are allowed
- Monthly pool refresh is allowed

Degraded mode:

- Source is `last_known_good`, `local_file`, or `static`
- New trend buys are paused by default
- Set `STRATEGY_ARTIFACT_ALLOW_NEW_ENTRIES_ON_DEGRADED=1` only if you intentionally want degraded-mode entries

Interpretation:

- `last_known_good` means fresh upstream validation failed, but a previously accepted upstream payload is still available in state
- `local_file` means upstream live access failed and the runtime fell back to a validated local file from the configured `STRATEGY_ARTIFACT_FILE`, the repo-local artifact, or a compatible `CryptoLivePoolPipelines` checkout
- `static` is emergency-only and should be treated as lowest-confidence operation

## Strategy Artifact Settings

Use the generic `STRATEGY_ARTIFACT_*` names for crypto strategy artifacts.

Primary settings:

- `RUNTIME_TARGET_JSON`: canonical runtime target written by QuantRuntimeSettings; when present, `STRATEGY_PROFILE` and `BINANCE_DRY_RUN` must match it or the run fails closed
- `STRATEGY_PROFILE`: live profile selector; current supported value is `crypto_live_pool_rotation`
- `STRATEGY_ARTIFACT_FIRESTORE_COLLECTION`: upstream artifact collection, default `strategy`
- `STRATEGY_ARTIFACT_FIRESTORE_DOCUMENT`: upstream artifact document, default `CRYPTO_LIVE_POOL_ROTATION_LIVE_POOL`
- `STRATEGY_ARTIFACT_FILE`: local fallback artifact path
- `STRATEGY_ARTIFACT_MAX_AGE_DAYS`: freshness window for upstream `as_of_date`
- `STRATEGY_ARTIFACT_ACCEPTABLE_MODES`: comma-separated accepted upstream modes
- `STRATEGY_ARTIFACT_EXPECTED_SIZE`: expected live-pool size
- `STRATEGY_ARTIFACT_ALLOW_NEW_ENTRIES_ON_DEGRADED`: explicit degraded-entry override

## Runtime Expectations By Failure Type

### Upstream stale or malformed

Expected behavior:

- Runtime does not silently treat stale upstream as healthy
- Falls back to last known good, then local file, then static universe
- State keeps source metadata so the degraded source is visible in audit trails

Operator action:

- Inspect upstream Firestore payload freshness and shape
- Verify the upstream project published the expected `version`, `mode`, and `pool_size`
- Prefer fixing upstream rather than enabling degraded new entries

### Firestore unavailable

Expected behavior:

- If state load fails, the cycle aborts before trading
- If trend-pool Firestore read fails but state load works, runtime can still fall back to last known good / local file / static

Operator action:

- Validate `GOOGLE_APPLICATION_CREDENTIALS` for local runs, or validate the GitHub OIDC / Workload Identity binding for the runtime workflow
- Check service account validity and Firestore API availability
- Use `run_cycle_replay.py` for dry-run confirmation while Firestore is unavailable

### Binance API failure

Expected behavior:

- Client bootstrap retries before aborting
- If connection cannot be established, cycle exits with an error notification and no trades

Operator action:

- Check Binance API key validity, IP restrictions, and runner connectivity
- Re-run manually only after the connectivity issue is confirmed resolved

### Telegram unavailable

Expected behavior:

- Telegram transport and response-body acknowledgement are both validated
- A delivery failure does not roll back completed trading actions, but the execution report and workflow are marked failed
- Persisted notification receipts contain only delivery metadata and message hashes, never tokens, chat IDs, or message text
- A failed periodic status delivery does not advance its report bucket, so a later cycle can retry

Operator action:

- Verify `TG_TOKEN` / `TG_CHAT_ID`
- Treat this as an observability incident, not a trading-signal incident

## Local Operator Commands

Preferred local install path:

```bash
cd /path/to/BinancePlatform
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip uv
uv sync --frozen --extra test
```

Replay one fixed cycle:

```bash
python3 run_cycle_replay.py --run-id local-check
```

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

## Workflow Runtime Auth

- The runtime workflow now authenticates to Google Cloud with GitHub OIDC + Workload Identity Federation.
- For safe runner-side verification, dispatch `main.yml` with `validate_only=true`; that checks Google Cloud + Firestore auth without running live trades.
- Local manual runs can still use `GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json` when needed.

## Escalation Guidelines

- If the runtime falls to `static`, treat it as an operator-visible degraded incident.
- If Firestore state cannot load, do not bypass the abort by force-running live trades.
- If upstream remains stale for multiple cycles, coordinate with the upstream publisher before changing degraded-mode buy policy.

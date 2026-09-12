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

## Binance LIVE risk authority (PENDING)

The runtime accepts a LIVE authority only from the three protected repository
variables `BINANCE_RISK_AUTHORITY_FILE`, `BINANCE_RISK_AUTHORITY_SHA256`, and
`BINANCE_RISK_AUTHORITY_SOURCE_REVISION`. On the Oracle runtime job, an approved
JSON may be stored in the protected `binance-runtime` environment secret
`BINANCE_RISK_AUTHORITY_JSON`; the workflow writes it only to a `0600` temporary
file and removes that file after the strategy step. The secret and a configured
runner file are mutually exclusive. Until an approved source is supplied, keep
all source values absent and leave execution closed. The reviewable template
below uses the loader's exact field names; `decision=PENDING` and the null or
empty values make it unusable and it must not be loaded as a source file.

```json
{
  "decision": "PENDING",
  "authority_scope": null,
  "runtime_target": {
    "platform_id": "binance",
    "strategy_profile": "crypto_live_pool_rotation",
    "account_scope": "crypto_combo",
    "account_selector": ["crypto_combo"],
    "deployment_selector": "crypto_combo"
  },
  "strategy_revision": null,
  "runner_revision": null,
  "config_sha256": null,
  "continuous_inputs_allowed": null,
  "mandate": {
    "mandate_id": null,
    "mandate_version": null,
    "effective_at": null,
    "expires_at": null,
    "validity_mode": null,
    "max_snapshot_age_seconds": null,
    "effective_exposure_cap": null,
    "loss_budget": null,
    "budget_policy": null,
    "product_caps": {},
    "nominal_caps": {},
    "product_leverage_factors": {},
    "allowed_nonzero_assets": [],
    "product_effective_caps": {},
    "max_nonzero_assets": null
  }
}
```

The account and deployment values above are the currently reviewed target
shape and still require final source verification. A real source becomes
usable only after external approval provides `decision=APPROVE` and
`authority_scope=LIVE` with every required field.

The approval decision must specify the account, tradable assets, risk limits,
including whether BNB is fuel-only, and whether the strategy may dynamically
allocate all approved managed funds. For that policy, set
`budget_policy={"mode":"managed_usdt_dynamic"}` and leave `loss_budget` absent
or null. The loader derives the current QPK USDT allocation ceiling from the
validated Spot-plus-Flexible-Earn managed balance and the approved exposure
headroom; the strategy request cannot raise it. A numeric `loss_budget` remains
available for a separately approved fixed ceiling. In both modes, the amount
is a QPK USDT allocation ceiling for the cycle, not a claimed maximum loss.

`validity_mode="until_revoked"` expresses a continuing source policy without
an arbitrary far-future expiry. Each evaluation still receives a short QPK
validity interval bounded by `max_snapshot_age_seconds`, and the source file,
digest, revisions, runtime identity and current snapshot are revalidated. A
fixed `expires_at` remains supported and is never rolled forward. Planned
managed funds are not the same as immediately spendable Spot funds: each real
order still requires the existing Spot balance, any necessary Earn redemption,
and confirmed funding reconciliation.

Engineering fills and verifies Git revisions and digests from the approved
source record, the installed strategy, and the clean runner checkout; operators
do not need to calculate or hand-enter hashes. `BINANCE_RISK_AUTHORITY_SOURCE_REVISION`
must be the 40-character Git revision of that approved source record; a secret
timestamp or storage-object generation is not a source revision.
<<<<<<< HEAD

The reviewed 2026-09-13 policy scope is the existing `crypto_combo` account and
`crypto_live_pool_rotation`: all already managed Spot plus Flexible-Earn funds
may be allocated dynamically under the existing Spot-only, no-borrowing,
stop-loss and BNB fuel-only rules. This statement does not itself create an
approval source; the protected source must carry its actual Git provenance.

For a bounded no-submit check, dispatch `validate_only=true` together with
`full_cycle=true` while `RUNTIME_TARGET_ENABLED=false`. The validator loads the
LIVE source and real runtime identity, reads the broker through its explicit
read allowlist, runs `main.execute_cycle` with local `dry_run=True`, and closes
state, owner, notification, performance and platform-record writes. It only
passes after the existing cycle reaches `cycle_complete` with a real risk
`APPROVE`; a startup pass or a synthetic replay is not a recovery or activation
result. Missing or invalid source material fails closed before broker reads.
=======
>>>>>>> origin/main

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

### Runtime target control and lifecycle evidence

`RUNTIME_TARGET_ENABLED` is the single operator control for this target.  It
must be the literal `true` or `false`; a missing value fails closed.  The
current repository value is read back as `false`; keep it disabled until the
separate LIVE authority source and recovery prerequisites are verified.

- When it is `false`, `main.yml` finishes without checkout, cloud
  authentication, dependency installation, strategy startup, broker-secret
  injection, report publication, or a failure notification.  The existing
  external scheduler may still dispatch the workflow, but that dispatch is a
  harmless no-op.
- When it is `true`, the existing mode declared by `RUNTIME_TARGET_JSON`
  remains authoritative.  This control neither changes `paper`/`live`, nor
  changes a strategy profile, leverage, or any order parameter.
- `Runtime Target Lifecycle` is an hourly GitHub-hosted, read-only workflow.
  It checks the GitHub runtime dispatch and a scoped Binance execution report
  in Cloud Storage, then publishes only sanitized status to the central
  QuantRuntimeSettings lifecycle endpoint.  It never starts the self-hosted
  runner, contacts Binance, or changes this control.
- The existing Runtime report reads the reviewed Ubuntu hourly cron entry and
  the `cron` daemon state on `main` from `binance-quant-runner`. It records only
  `enabled`, `disabled`, or `unknown`; an unexpected runner context, ambiguous
  cron entry, timeout, or read failure remains `unknown`. The lifecycle monitor
  projects that report field and keeps the Runtime report's original timestamp;
  it does not schedule or control execution.
- A disabled target is reported as intentionally disabled, not as a failed
  broker or a successful execution.  A missing report, unavailable monitor,
  malformed target declaration, or failed runtime dispatch is parked for
  operator review; none of those states can automatically re-enable the
  target.

### Frozen balance diagnosis

While the target is `RECONCILE_ONLY`, an authorized operator can dispatch
`Runtime` on reviewed `main` with `reconcile_only=true`,
`diagnose_balances=true`, `reconcile_persist_candidate=false`, and
`validate_only=false`. The input guard rejects other diagnostic combinations
before checkout or authentication. Keep `RUNTIME_TARGET_ENABLED=false`.

This mode reads the signed account snapshot once and compares both legacy
balance digests, including a bounded check for added zero-balance rows. It
prints only a reason code and aggregate counts. It does not load the execution
ledger, collect order history, build a recovery candidate, persist an artifact,
send Telegram, or change the frozen baseline. A diagnostic match is evidence
about the balance representation only; it never permits live execution.
`balance_difference_unexplained` leaves recovery closed and requires an
independently explained balance change before any baseline enrollment.

### Daily-accounting state migration

`accounting_migration_action=rebase-proposal` prepares an **informational new
accounting start** for an operator who chooses to retain unresolved history.
It requires a disabled target, `reconcile_only=true`, no current open orders or
current-day fills, and complete current evidence. It lists managed Spot +
Flexible Earn quantities, average-price valuation estimates, old fields and
proposed new fields. Zero new-period counters do not classify or erase prior
income; the entire old ledger must be archived before any separately approved
write. The circuit-breaker latch and all other fields must remain unchanged.
This output is not an executable migration candidate; existing `preview`/`apply`
schema and zero-activity checks are unchanged.

The operator explicitly approved the concrete proposal from Runtime
`34601984051`. The one-time `accounting_migration_action=rebase-apply` is bound
to that exact old ledger, recovery-control record and managed-quantity set.
Use disabled `main` and `reconcile_only=true`, with no artifact/digest or
certificate inputs. It verifies account identity, current open orders/fills,
complete activity evidence and no non-reward funding activity. Quantities or
source changes stop the action; valuation uses fresh average prices.

One Firestore transaction (`max_attempts=1`) creates the private
`strategy/MULTI_ASSET_STATE__before_rebase_34601984051` archive with the full old
ledger and recovery control, and updates only accounting fields plus an
`accounting_rebase` marker retaining the unresolved-history statement and opening
time. An existing archive blocks replay; archive creation and ledger update
commit together. The original breaker, order records and all other fields are
preserved. Full archive, ledger, control and absent-owner readback are checked.
Unknown write/readback outcomes return `uncertain/no_retry`; never redispatch to
guess the outcome. Logs contain no balances. This action does not change runtime
enablement, recovery authority, or place orders.

This repository is public. Never print the proposal or upload it unencrypted.
The operator generates an X.509 recipient certificate and keeps its private key
on their own computer. Pass only the **public certificate** through
`proposal_recipient_certificate`. The existing runner's OpenSSL CMS encrypts
the in-memory proposal with AES-256; only `proposal.cms` is retained in the
`binance-accounting-rebase-proposal` artifact for one day. Download the artifact
from the verified run and decrypt it locally with the retained private key.
Log output contains status flags only. Execution, if later approved, requires
fresh quantities/prices and a separate guarded write; the proposal's valuation
time and estimates are not a standing permission to overwrite balances.

`accounting_migration_action=audit` with `reconcile_only=true` and a disabled
target performs a read-only ledger diagnosis. It requires the exact preserved
recovery source inspected in Runtime `34586531344`, after its approved downgrade.
It compares current Spot plus Flexible Earn totals with the ledger's managed
asset snapshot, reporting missing assets separately from mismatches. Separately,
it checks BONUS rewards since the recovered source observation against that
source's Spot hashes (at most seven days). Today's rewards must not be compared
against an older frozen baseline as if they cover the entire intervening period.
The output includes trade counts over the same recovered-source window (using
the existing bounded daily requests), open-order counts, missing assets with
nonzero balances, and the direction of the BTC balance change, not account
amounts. The legacy snapshot has no observation timestamp; document update time
and daily reset date cannot supply that missing provenance. Incomplete history
or changing balances/ledger blocks the audit. A match
does not reconcile the whole account, authorize migration, or restore trading;
the existing zero-activity preview/apply checks remain unchanged.

The migration is a separate, one-time `Runtime` workflow mode for an old
`trend_val` ledger. It does not activate recovery control, grant execution
authority, clear the circuit-breaker latch, or reconstruct historical
accounting. Keep the repository runtime control disabled and use reviewed
`main`; the workflow's existing concurrency group serializes this run with
other `Runtime` runs on `main`.

If a migration is blocked by recovery-control state, first use
`reconcile_only=true` and `accounting_migration_action=inspect` with the target
still disabled. This reads only the control record's sanitized state, source
run, update time and digest, plus ledger/owner existence. It does not connect to
the broker, write a candidate or change any record. It cannot grant approval for
a state transition; the existing preview/apply requirements remain unchanged.

The operator approved one downgrade of the exact control inspected by Runtime
`34586531344`: use `reconcile_only=true` and `accounting_migration_action=quiesce`
with the target still disabled and no other runtime in flight. This single-use
path binds the reviewed control digest and update time in source; it changes
only `state` from `ACTIVE_LKG` to `RECONCILE_ONLY`, inside one non-retrying
transaction requiring no owner and an existing ledger. Readback verifies the
entire resulting control and unchanged ledger. It preserves historical source,
confirmation and transition material. A mismatch blocks; an uncertain commit
or readback prohibits retry. It neither grants accounting-apply approval nor
reactivates trading. Later controls require separately reviewed authorization.

First dispatch `Runtime` with `reconcile_only=true` and
`accounting_migration_action=preview`. Leave the preview run ID, expected
digest, recovery, balance-diagnosis, candidate-persistence, and validation
inputs empty or disabled. Preview reads the exact ledger and owner documents,
the recovery-control state, signed account binding, all open orders, current
UTC-day fills and balance-flow surfaces, and complete Spot plus Flexible Earn
positions. Any current-day trade, deposit, withdrawal, transfer, Earn activity,
open order, owner, unsafe order state, missing page, missing BNB position, or
other incomplete evidence blocks the candidate. The workflow retains the
redacted candidate under the fixed artifact name
`binance-accounting-migration-preview` for one day, but the candidate itself is
valid for only ten minutes.

Review the preview summary and its `candidate_sha256`. A successful preview is
still read-only and is not approval to write. After an operator explicitly
approves that exact digest, dispatch `Runtime` again within the ten-minute
window with:

- `reconcile_only=true`
- `accounting_migration_action=apply`
- `accounting_migration_preview_run_id` set to the exact preview workflow run
- `accounting_migration_expected_digest` set to the approved 64-character
  `candidate_sha256`

Apply downloads only that run's fixed-name artifact, verifies its source commit
and expiry, and repeats the account, order, activity, balance, and control
reads. It then performs one Firestore transaction with `max_attempts=1`, bound
to the original ledger update time and raw ledger/control hashes. The write is
limited to the daily-accounting fields and the newly verified balance snapshot;
order records, action history, unknown fields, and the circuit-breaker latch are
preserved. A final readback verifies both the accounting values and the
preserved-state hash before reporting `applied`.

Treat every blocked or uncertain apply as terminal for that candidate. Do not
retry it. Produce a fresh preview only after the reason is understood and the
same prerequisites can be proven again. The management-site recovery approval
does not approve this ledger write, and migration completion does not permit
runtime activation or trading.

### Post-rebase recovery

If a post-rebase preparation fails at broker collection with only a generic
code, use one explicit `recovery_action=diagnose` with `reconcile_only=true` and
the runtime disabled on main. It runs the same archive and account validators,
then returns before saving control or publishing a candidate. It receives no
console synchronization or confirmation credentials. Only exact allowlisted
application reason codes are reported; provider payloads and amounts remain
hidden. A diagnosis failure stops the operation; do not repeat prepare or
weaken its evidence checks. Diagnosis success never grants recovery authority.

If that diagnosis reports `post_rebase_unknown_spot_balance`, an operator may
run the separate `accounting_migration_action=scope-preview` with the runtime
disabled and `reconcile_only=true`. This action validates the approved private
rebase archive, the unchanged current ledger and the archived account identity,
then calls the Spot account endpoint exactly twice. It does not read order,
transfer, Earn, history or market-price endpoints and does not write Firestore
or recovery control.

On success, the runner sends one bounded HTTPS request directly to the private
strategy console. The report is available only through the authenticated admin
view for 24 hours. Its payload lists only nonzero Spot assets outside the
approved opening scope, with their `free` and `locked` quantities and the
observation time. Zero balances, managed balances, the old ledger and order
records are omitted. No plaintext or encrypted report file is created, and
workflow logs contain no asset names, quantities or provider errors. Asset names
accept 1–20 Unicode letters or digits because the
[Binance Spot REST API](https://developers.binance.com/en/docs/products/spot/rest-api)
may return non-ASCII asset identifiers even when a request contains none.

Any archive, ledger, account identity or Spot row change between the bounded
reads blocks the preview, as does an invalid, negative or nonfinite quantity.
The action performs no automatic retry. A network failure or invalid
acknowledgement after publication starts is `uncertain/no_retry`; inspect the
authenticated console before deciding whether any later collection is needed.
The private list is evidence for a later manual asset-scope decision; it does
not approve adding or ignoring an asset, changing the ledger, preparing
recovery, activating runtime or placing an order. It also makes no claim about
assets outside the Spot account response.

After the approved accounting rebase, the legacy recovery source cannot be
reused because it is bound to the archived ledger digest and its older history
window. A new recovery `prepare` uses the existing recovery controller and
console contract, but records `source_kind=post_rebase` and keeps
`historical_difference_unresolved=true`. Human confirmation applies to resuming
from the approved new-account opening. It does not state that the earlier
accounting difference was reconstructed or cleared.

Keep `RUNTIME_TARGET_ENABLED=false`, `reconcile_only=true`, and the recovery
control at `RECONCILE_ONLY`. The controller accepts only migration Runtime
`34606795875` on `ed7ee6e96cea0addb292f3f45338652095d0da58`, the private archive
`strategy/MULTI_ASSET_STATE__before_rebase_34601984051`, and the approved old
ledger, old control, opening-quantity hashes and marker. The current ledger must
still equal the archive's `new_ledger_sha256`; every field outside the accounting
patch must equal the archived ledger. The runtime state normalizer preserves an
existing `accounting_rebase` marker during ordinary saves and does not add one
to an older state.

At runtime, the prospective Earn checkpoint fixes the managed asset keys and
the balance snapshot must keep the same keys. Fresh pool symbols outside that
scope are excluded from strategy candidates when they have no position, while
all approved assets remain in valuation and balance snapshots. Any active
out-of-scope position or malformed pair stops state loading.

The opening must be no more than seven days old. The controller requires zero
open orders, zero fills for every configured strategy symbol, complete bounded
history pages, and zero non-reward deposits, withdrawals, Earn subscriptions or
redemptions, and configured universal transfers since that opening. Reward rows
may be present but do not explain or excuse a quantity difference. Spot and
Flexible Earn totals for each approved opening asset must equal the opening at
the explicit eight-decimal comparison basis on two reads. Every Spot `free` and
`locked` value and each returned Earn `totalAmount` must be finite and
nonnegative; any locked amount, nonzero Spot asset outside the approved opening,
incomplete page, identity change, or quantity change blocks recovery.

The Earn endpoint is read once per approved asset. This proves the configured
managed-asset scope; it does not prove that an unknown asset has no Earn
position elsewhere in the account. Likewise, fill history is complete only for
the configured strategy symbols. Do not describe this bounded evidence as a
whole-account audit.

The stored source contains workflow metadata, hashes and aggregate counters,
not balance rows or amounts. `verify` and `activate` revalidate its workflow
provenance, exact archive digest, current ledger and fresh broker evidence.
Activation uses the existing candidate, dual-review confirmation and atomic
transition. The transaction reads the owner, ledger, previous control and
archive together with `max_attempts=1`, then checks all four again after the
write. A committed ACTIVE control remains valid after the short candidate
review window; runtime consumption still validates every source, candidate,
confirmation and transition binding. An uncertain control write, readback, or
candidate publication returns `uncertain/no_retry`; inspect the stored control
and console state before deciding any later action.

### Notification language and format

Set `NOTIFY_LANG` to `zh` or `en`; Chinese locale variants such as `zh-CN`
also select Chinese. Unsupported locales use English. Human-facing startup
errors and periodic summaries use the same local catalog, while machine reason
codes and execution-report fields remain stable. Periodic summaries include
the strategy name, equity, trend holding, BTC gate and target, AHR999 and Z-score
in at most five lines. Existing frequency and delivery acknowledgement rules
still control deduplication; low AHR999 no longer adds discretionary-buy advice.

### Runner security boundary

The current production runtime still uses a persistent self-hosted runner. Treat this as a temporary, higher-risk boundary until the runtime moves to an ephemeral runner or an isolated Cloud Run Job:

- dedicate the runner to the dispatch-only `main.yml` runtime; do not run pull-request or untrusted branch jobs on it;
- restrict the Binance API key to the required trading scope, disable withdrawals, and apply an IP allowlist where the account supports it;
- keep the `binance-runtime` environment protection and reviewed OIDC identity contract in place;
- rebuild the runner after suspected compromise instead of trusting cached workspaces or virtual environments.

The broker job has repository read permission only. Successful execution reports are transferred to a separate GitHub-hosted job, which alone receives `contents: write` for the `logs` branch. This prevents the broker credentials and repository write token from sharing one job, but it does not make a persistent runner equivalent to an ephemeral one.

The staged replacement architecture, no-order shadow proof, deployment preflight,
and rollback fence are documented in
[`runtime_isolation_migration.md`](runtime_isolation_migration.md). That plan is
informational until a separately reviewed live cutover is approved; the current
runtime remains authoritative.

The current host is operator-attested as a user-owned Oracle Cloud Compute
instance. The preferred future boundary is a separate on-demand OCI instance
launched from a pinned custom image, registered as a one-job JIT runner, routed
from a private subnet through an OCI NAT gateway with a reserved public IP, and
terminated with its boot volume after durable evidence. This is a migration
decision only: no OCI resource, Vault secret, allowlist entry, or live route has
been created or changed.

Before selecting a replacement host, manually run `Runtime Isolation Host
Profile`. It has repository read permission only, receives no GitHub environment,
OIDC token, or secret, and writes a redacted artifact. Provider or network fields
that cannot be proven remain `UNVERIFIED`. To verify the current Binance
allowlisted egress without publishing the address, configure both
`BINANCE_RUNTIME_EGRESS_CHECK_URL` and `BINANCE_RUNTIME_EGRESS_SHA256`; the
workflow records only whether they match.

`Runtime Isolation Shadow Fixture` always runs the portable no-order fixture on
a clean GitHub-hosted runner. Set `include_current_runner=true` only outside the
live scheduling window to run the same fixture on the current self-hosted runner
and compare semantic report digests. Neither job references Binance credentials,
the `binance-runtime` environment, or Google OIDC. Passing proves fixture parity,
not live readiness or host ephemerality.

`OCI JIT Shadow Preflight` implements the next non-applying preparation step. It
checks the committed private-subnet, reserved-NAT, defined-tag,
instance-principal, one-job runner, and delete-on-termination contract; reports
which operator-owned OCI repository variables are still missing; exercises the
fail-closed orphan audit; and reruns the no-order fixture. It has no OCI OIDC,
broker secret, environment, or mutation authority. The required variables and
redacted attestation format are listed in
[`infra/oci-jit-shadow/README.md`](../infra/oci-jit-shadow/README.md). A clean
fixture audit proves the checker, not the absence of real OCI or GitHub orphans.

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

### Fill accounting and daily loss state

Market orders consume Binance's `FULL` response: `executedQty`,
`cummulativeQuoteQty`, and every fill's `price`, `qty`, `commission`, and
`commissionAsset`. Base-asset fees reduce the received or remaining position,
quote-asset fees adjust USDT, and BNB fees use the BNBUSDT price and balance
already captured in the same market snapshot. A missing, non-finite, or
inconsistent field, or a third-asset fee without a same-cycle USDT price and
balance, leaves the durable order state at `FILLED_ACCOUNTING_PENDING`. The
fill is known and must not be resubmitted; new funding calls remain blocked
until reconciliation completes.

Trend daily loss uses marked trend holdings plus the cash returned to or spent
by that sleeve. Internal rotation does not create PnL; slippage and fees do.
When the sleeve starts the UTC day empty, the first net investment establishes
the risk denominator. A new UTC day resets this basis under the existing
policy. A same-day state using the old `trend_val` basis, or a new-basis state
with missing/non-finite fields, blocks instead of clearing accumulated loss or
the circuit-breaker latch.

Balance snapshots include USDT, BTC, BNB, and trend assets. Changes not already
persisted from a known fill or the observed Earn/fuel reconciliation are
`balance_change_unexplained`: keep the prior daily bases and latch, stop new
submissions, and reconcile the account. Do not assume that an unexplained
difference is a deposit, withdrawal, or zero PnL. Dry-run effects remain
explicit estimates and are not actual-fill evidence.

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
- For safe runner-side verification, dispatch `main.yml` with `validate_only=true`; that loads the actual configuration, committed recovery control and strategy entrypoint without broker credentials or live execution. It also works while the runtime is paused.
- A failed startup check reports only an exact allowlisted `reason_code`. In particular, `runtime_recovery_not_active` means the legacy strategy has no active recovery grant; validation still fails and does not activate it. Profile/dry-run conflicts and invalid recovery controls have distinct safe codes; unknown exceptions remain `runtime_startup_validation_failed`, without raw provider details. Investigate the stated prerequisite before another authorized validation attempt.
- Local manual runs can still use `GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json` when needed.

## Escalation Guidelines

- If the runtime falls to `static`, treat it as an operator-visible degraded incident.
- If Firestore state cannot load, do not bypass the abort by force-running live trades.
- If upstream remains stale for multiple cycles, coordinate with the upstream publisher before changing degraded-mode buy policy.

### Bounded balance-activity history

When zero-row diagnosis cannot explain a frozen digest difference, the same
private diagnostic dispatch can accept `balance_history_start` and
`balance_history_end` (ISO 8601 with timezone). Both must be supplied, describe
at most seven days, and fall within the past thirty days. The script validates
the window before contacting the broker. Use the independently recorded
baseline observation time as the start, not an inferred balance timestamp.

The additional GET-only diagnostic counts deposits, withdrawals, Flexible Earn
subscriptions/redemptions/rewards, and twelve Spot-related universal transfer
directions. Each surface is limited to one page. A full page, inconsistent
total, malformed response or failed read stops collection without a retry;
unknown counts remain null. It prints no asset names, amounts or provider
errors and never changes the baseline or runtime state.

An activity count is a lead for investigation, not proof that the old balance
has been reconstructed. These surfaces omit other account operations such as
Convert, dust and isolated-margin movements. `complete_balance_reconciliation`
and `execution_authority_granted` always remain false. Recovery still needs an
independent account/ledger check and an explicitly confirmed baseline.

API contracts: [Wallet capital history](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/capital),
[Flexible Earn history](https://developers.binance.com/en/docs/catalog/investment-and-services-simple-earn/api/rest-api/flexible-locked),
[Universal transfers](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/asset).

### Automatic external cash-flow accounting

The ordinary strategy cycle has one deliberately narrow automatic path for a
manual external deposit: a confirmed (`status=1`) USDT row from the capital
deposit history, credited to Spot (`walletType=0`, `transferType=0`). The cycle
reads deposit and withdrawal history once each over a rolling seven-day window,
with no retry. It accepts fewer than 1,000 rows per surface, hashes provider IDs
and payloads into the existing private trade state, and stops if the page is
full, a finalized record changes, the cursor reaches its bounded capacity, or
the balance evidence is incomplete.

The first unchanged cycle establishes the cursor and does not re-account old
deposits. A later confirmed deposit is applied once only when its completion is
in the current UTC accounting day and its principal exactly explains the Spot
USDT quantity increase while every other managed quantity is unchanged. The
daily opening equity remains frozen. `daily_external_principal_usdt` is removed
from the daily return numerator, so a deposit is not P&L and cannot dilute an
existing loss percentage. Trend-sleeve cash flow, risk base, and a latched
circuit breaker are unchanged. A UTC-day reset clears only the new day's
external-principal accumulator under the existing reset policy.

Withdrawals remain outside automatic accounting. The official history response
exposes separate `amount` and `transactionFee` values but does not define their
combined Spot debit; its `applyTime` and `completeTime` strings have no timezone,
and the documented `startTime`/`endTime` filter does not say which one it uses.
The cursor hashes withdrawal rows so an unchanged historical row inside the
rolling window does not stop an otherwise unchanged cycle. A new or changed row
combined with an unexplained balance change blocks before orders; the cycle does
not guess principal, fee, or accounting day.

Non-USDT deposits, Funding-wallet deposits, nonzero/unknown transfer types,
Flexible Earn rewards, and internal transfers are also outside the automatic
path. Unchanged historical rows can be enrolled only while establishing an
unchanged balance cursor; a current quantity change still blocks unless a new
supported deposit explains it exactly. None of these rows modifies the frozen
opening, enrolls the post-rebase recovery source, clears the historical
difference, resets the breaker, or grants execution authority. A supported
deposit whose completion arrives outside the current accounting day is held for
operator review rather than posted to a later day.

This adjustment protects the local daily-loss calculation only. The existing
per-cycle performance record has no exactly-once delivery for a cash-flow amount:
emitting a daily cumulative value would double count it, while emitting it once
could lose it if the performance write failed after the private cursor advanced.
Binance therefore records `external_cash_flow=null` and remains incomparable in
cross-cycle performance monitoring until that separate durable contract exists.

While the account remains disabled, dispatch the existing Runtime workflow with
`reconcile_only=true` and `accounting_migration_action=cash-flow-preview` to
preflight this path without loading or activating the strategy. The command
uses the original account identity, safe-order/owner checks, private ledger,
managed Spot-plus-Flexible-Earn balances and the same two bounded history reads.
It repeats balance and ledger reads for stability, then runs the production
cash-flow consumer on an in-memory ledger copy. No candidate file, cursor,
ledger, recovery control or execution state is written; output contains only
safe status, counts and prerequisite codes.

`reconciled_preview` means a new supported deposit reconciled on the copy;
`baseline_preview` means only an initial cursor could be established and does
not apply it; `no_new_deposit` contains no new deposit acceptance evidence.
A missing baseline with changed balances, late/unsupported flows or a mismatch
returns `blocked` and exit 2. All outcomes keep full account reconciliation and
execution authority false, including when out-of-scope Spot assets are present.
Stop on the first real failure; do not initialize a cursor or activate trading
to make this diagnostic pass. A successful preview is not durable accounting.

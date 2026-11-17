# Read-Only Reconciliation for the Frozen Binance Live Baseline

English | [简体中文](binance-reconciliation-recovery.zh-CN.md)

When the Binance target is in `RECONCILE_ONLY`, an operator can manually start the Runtime
workflow on the self-hosted runner with `reconcile_only=true`. It defaults to
`--no-persist`, outputting only sanitized candidates with no artifact, run report, or
notification; a short-lived artifact is stored only when `reconcile_persist_candidate=true`
is explicitly chosen. It runs the standalone `scripts/reconcile_frozen_live_baseline.py`
entrypoint and never invokes `main.py`, triggers strategies, places or cancels orders,
transfers funds, subscribes/redeems Earn products, or writes to Firestore.

The runner performs read-only reads of signed account state, balances, all open orders, and
recent 7-day trades strictly scoped by `BINANCE_RECONCIATION_SYMBOLS`; that variable must
cover BTC, all current strategy trend candidates, and BNB fee assets rather than querying
full history. Binance Spot `myTrades` limits each query window to 24 hours, so the system
queries and deduplicates across non-overlapping daily windows and fails closed if any window
reaches the API 1,000-record cap instead of guessing pagination and missing trades. It also
reads local execution state read-only. Account UID, balances, orders, and trades are never
written to logs, reports, or artifacts. When persistence is enabled, artifacts keep only the
`binance_reconciliation_candidate.v1` summary and stable blocking codes for 7 days.

If the exchange returns no account UID, managed symbols are unconfigured, any read fails,
private expected digests are absent, or any digest mismatches, the run completes as
`candidate blocked` rather than retrying, degrading, or resuming trading.

## Bounded Recovery Controller

Explicitly running Runtime with `recovery_action=prepare` together with
`reconcile_only=true` generates and publishes a recovery candidate. The runtime target must
already be disabled, the branch must be `main`, and no other diagnostic or artifact modes
may be mixed in. Repository variable `BINANCE_RECOVERY_WORKFLOW_SHA` must pin the approved
`main` workflow commit while the application still runs the approved
`BINANCE_RUNTIME_RELEASE_SHA`. The two recovery credentials are consumed only inside the
corresponding prepare/verify/activate steps; normal runs do not obtain them. The console
remains the fixed QSL private console with redirects and custom targets disallowed.

This adapter binds strictly to the existing 2026-09-03 frozen receipt (Runtime `33768005187`
/ artifact `9898377291`), the original six expected digests, and the 13 verified managed
symbols. It reads full open orders, recent 7-day trades, the unnormalized real local ledger,
and up to 7 days of funding history starting from the frozen sample timestamp. Account,
orders, trades, and ledger must all match; balances may only match the original balance
digest by rolling back verified BONUS ledger entries item by item. Other capital activity,
unexplained deltas, incomplete reads, window overflows, or balance changes during sampling
all block cleanly. Original frozen digests are never overwritten.

The controller uses QPK `c4f9b7599041406d4693083bf2eb4e85d6e10059` source-bound candidate
v2: at least one complete, fresh, verifiable reconciliation evidence item is required; model
review is optional advisory context and stays explicitly `unavailable` when absent without
fabricating review tickets. The candidate stores sanitized evidence and the real `main` run
source at `strategy/MULTI_ASSET_STATE__recovery` in Firestore, while the console receives
only summaries. Before saving, a transaction checks that no account execution lock exists,
the ledger digest is unchanged, and the prior control document is unchanged. Subsequent
steps also verify candidate provenance via GitHub API as a successful run of the matching
`main` workflow.

The account holder must explicitly confirm that candidate in the console.
`recovery_action=verify` or `activate` must supply the exact `recovery_id` returned by
prepare, read the console's genuine confirmation receipt, and re-read the exchange. Evidence
must postdate confirmation and match every digest in that candidate; both candidates and
confirmations are subject to a 30-minute freshness window. Verify performs no writes;
activate transitions the same control document atomically to `ACTIVE_LKG` in a single
Firestore transaction while recording the five expected digests and confirmation binding. On
transaction conflicts or unknown remote write outcomes, execution stops without retrying,
clearing execution locks, or modifying the trading ledger.

The runtime reads that committed control record only when
`BINANCE_RECOVERY_CONTROL_ENABLED=true` is explicitly configured. When the full binding is
valid, only continuity state for the verified runtime target is replaced; all other
identity, strategy, and risk fields remain unchanged. `RUNTIME_TARGET_ENABLED` stays an
independent disable switch untouched by activate. Operators may resume existing schedules
only under explicit authorization after recovery and verification succeed. None of the
recovery steps invoke `main.py`, submit/cancel orders, or move funds.

### Confirming and Verifying in GitHub

Configure required reviewers on the `binance-recovery-review` environment, allow `main`
only, disable administrator bypass, and pin the reviewer's GitHub user ID in repository
variable `BINANCE_RECOVERY_REVIEWER_ID`. This approval environment holds no credentials,
while existing runtime identities stay in `binance-runtime`.

1. Run Runtime on the approved `main` commit with `reconcile_only=true` and `recovery_action=prepare-github`. Copy `recovery_id` and candidate SHA-256 from run output.
2. Run Runtime on the same approved commit with `reconcile_only=true` and `recovery_action=verify-github`, providing `recovery_id` and `recovery_candidate_sha256`. The reviewer checks the candidate in run summary and approves `binance-recovery-review` via **Review deployments**.
3. The workflow reads the run's GitHub approval record, verifies reviewer and candidate, and re-reads the exchange. Candidates remain valid for 30 minutes from the last observation; expired candidates, altered content, or missing approval records halt verification. A `verified` result leaves control state in `RECONCILE_ONLY`. Start a new approval run to retry.

### Restoring Production Control and Running a No-Order Full Cycle in GitHub

On `main`, select `activate-github` and provide the same valid candidate ID and SHA-256. The
workflow accepts independent owner approval only from the `live-trading` environment, which
allows `main` only, disables administrator bypass, and uses required reviewers matching
`BINANCE_RECOVERY_REVIEWER_ID`.

After approval, it re-collects exchange data, validates ledger and execution locks, and
writes `ACTIVE_LKG` together with the GitHub approval run ID in one transaction. After
read-back confirmation, run Runtime in `runtime-production` with `validate_only=true` and
`full_cycle=true` to execute a read-only strategy cycle on the production-approved version.
Both steps require trading switches OFF.

This historical window is intentionally bounded; if expired or reconfigured, keep trading
closed and investigate the corresponding baseline rather than widening the window or blindly
re-sampling to accept unknown accounting changes. Failed recovery steps emit only phase and
fixed reason codes; diagnose read-only first without blindly retrying mutating remote
operations.

## Out-of-Scope Spot Assets at Approved New Opening Baseline

Confirmed 2026-09-12: retain original strategy management scope; out-of-scope Spot assets
participate only in whole-account read-only reconciliation and are excluded from strategy
trading. The post-rebase controller explicitly sets `observe_only` across both collection
passes and post-confirmation re-sampling; the source digest binds that policy and the count
of non-zero out-of-scope assets. All Spot balances still enter the account digest, and any
locked balance, open order, sampling delta, post-confirmation balance shift, or incomplete
history blocks the run. The count of managed assets must match the approved opening baseline
exactly; legacy ledger and archive frozen digests stay unchanged, and historical deltas
remain marked unresolved.

When runtime loads the strategy pool, ledgers with prospective Earn accounting enabled
anchor approved scope from `earn_accrual_checkpoint.assets` and require
`last_balance_snapshot` to hold the exact same asset keys; older post-rebase ledgers
continue reading scope from their existing snapshot. Unheld upstream out-of-scope candidates
are filtered solely from strategy candidates while preserving source payloads and versions;
actual holdings in out-of-scope assets, invalid pairs, or dedicated reserve assets mixed
into the trend pool still fail closed. Approved assets not selected in the current cycle
still participate in balance reads, valuation, and snapshots without becoming buy
candidates. BTC, BNB, and USDT keep fixed roles and cannot double-count inside the trend
sleeve. No new asset-inventory service is introduced here.

`diagnose` validates materials only; passing does not mean candidates are published, humans
confirmed, or trading resumed. Initial ledger cursor initialization remains deferred, and
frozen ledgers may never be modified to bypass recovery checks.

`earn-forward-diagnose` also emits an independent `forward_accounting_eligible` flag,
indicating solely that the current `Spot1 -> Earn1 -> Spot2 -> Earn2` interval is stable,
product lifecycles did not change, no unsupported external capital flows occurred, order
states are known, and balance deltas are explained by checkpoint/reward evidence;
`forward_accounting_write_permitted` stays `false`. Eligibility never substitutes for full
historical reconciliation, recovery candidates, or trading permission; live writes still
consume the existing owner-protected writer and re-verify control, ledger, and account
state.

## Idle Earn Capital Scope Correction (2026-09-12)

Change #249 mistakenly treated Earn separation as limiting strategy scope to Spot only and
has been reverted; the strategy retains its approved Spot + Flexible Earn asset scope, so
idle Earn balances never imply funds exited the strategy. Failed Earn reads or incomplete
pagination must reject valuation rather than falling back to Spot and reporting false
capital drawdowns.

Subscriptions and redemptions persist pending state first, then save raw receipts once
`purchaseId`/`redeemId` arrives. Read-only queries inspect matching history records,
verifying product, asset, amount, `SPOT` account, `SUCCESS`/`PAID` status, and resulting
Spot balance deltas; incomplete operations remain pending without duplicate POST retries or
continuing to order placement. Unknown outcomes without receipts require independent
verification rather than assuming success. Internal transfers never count as strategy
profits, and auto-subscribe settings are out of scope here. Restart verification executes
only after an existing account owner claim succeeds; legacy owner locks follow existing
manual recovery rules without clearing locks or preempting old processes, so crash recovery
is not claimed to be automatic in all cases.

`audit` reads `opening_balance_observed_at` from the bound archive for approved new opening
baselines rather than old recovery dates, keeping archive bindings unchanged across both
checks. It outputs a BNB reward-count match boolean and counts without printing balances,
amounts, or private details. Numeric matching narrows root cause only and grants neither
ledger edits nor trading permissions; historical BNB deltas still require complete
explanation.

References: [Binance Flexible Earn FAQ](https://www.binance.com/en/support/faq/detail/3bd1a6eba20a445da1e94bf6cfa52e80) and [Simple Earn REST API](https://developers.binance.com/en/docs/catalog/investment-and-services-simple-earn/api/rest-api/flexible-locked). Automated tests use synthetic funds; passing code and CI checks never equate to live subscriptions/redemptions, accounting recovery, or strategy activation.

### Time Granularity and Scope of BNB Delta Diagnostics

Official Flexible FAQ specifies per-minute reward accrual while historical APIs return daily
records. When an approved opening baseline falls intraday, summed daily rewards filtered by
record timestamp cannot be interpreted as accrued rewards between that opening baseline and
the current snapshot. `delta_matches_*` checks numeric equality only;
`realtime_reward_interval_alignment` remains explicitly unverified. Neither proves that
observed deltas came purely from rewards nor authorizes relaxing recovery gates.

Only when `audit` detects a BNB mismatch does it issue one extra read each of Wallet `asset/assetDividend` (BNB, max 500) and `asset/dribblet` (Spot, max 100) across the verified opening-to-current window. Full pages, count mismatches, out-of-range timestamps, or read errors mark status unverified; output contains only counts, failure codes, and BNB delta direction without amounts or raw rows. Neither check enters recovery admission predicates or claims to cover all capital paths, and neither mutates trading, ledgers, or account settings. Reference API: `https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/asset`.

## Current-Time New Opening Preview (2026-09-12)

`rebase-proposal` collects current Spot + Flexible Earn holdings across the original
ledger's managed assets, saving product reward counters and external flow deduplication
cursors. An encrypted preview is emitted only when no open orders exist before or after
sampling, zero fills or external cash-flow changes occur in the sampling window, product
lifecycles match, and quantity deltas equal real-time reward counter increments exactly;
normal per-minute accrual is not rejected for minor snapshot drift. Product changes, counter
regressions, collateralization, locked/unredeemable balances, incomplete pagination, or
unexplained changes still reject.

This preview is a bounded cross-API observation rather than atomic account proof. Prior
archives and historical deltas stay preserved, historical rewards are never booked as
new-period profit, and history is not scanned unboundedly. Start time equals actual
observation time rather than backfilling to midnight UTC. Existing encrypted artifact paths
remain unchanged, and `automatic_accounting_ready`, `recovery_ready`, and
`execution_authority_granted` stay `false`. No ledger writes, migrations, activations, or
auto-subscribe changes occur, and existing apply allowlists remain untouched.

### Establishing a New Prospective Baseline from Today (2026-09-21)

When the user confirms destructive reset semantics, it means anchoring a **new accounting
baseline from this observation time forward**, not deleting old evidence physically. The
operational flow remains `rebase-proposal` -> (human review of encrypted preview) ->
separately authorized `prospective-rebase-apply`:

When running read-only previews on `main`, `BINANCE_REBASE_PREVIEW_SHA` must equal the
approved workflow commit, and the preview loads code from that commit while scheduled
trading and other recovery paths continue checking the production release commit. When
trading switches are OFF and control still holds `ACTIVE_LKG`, preview first validates the
prior recovery confirmation before reading account state. Candidate bindings plan future
`RECONCILE_ONLY` control digests only; actual changes still require separate confirmation.

- Never requires missing old history / `historical_continuity` to prove the new period; legacy ledger and archives stay immutable with `historical_difference_unresolved=true` and pre-baseline PnL classified as `unreconstructed_history`.
- Remains fail-closed: validates account identity, Spot, Flexible Earn, and current ledger/control beforehand; rejects open/pending/unknown orders, in-window trades, and unexplained external cash-flow shifts; requires Spot/Earn conservation across dual reads; and binds encrypted candidates to `GITHUB_RUN_ID` (`proposal_run_id`), `GITHUB_SHA`, ledger/control digests, and observation time.
- `rebase-proposal` writes local encrypted artifacts only, never writing Firestore, placing orders, or altering `RUNTIME_TARGET_ENABLED` / `RECONCILE_ONLY`.
- `prospective-rebase-apply` reads the **full approved proposal JSON** exclusively from protected environment secret/var `BINANCE_APPROVED_PROSPECTIVE_OPENING` (gated by environment/operator approval), deletes it from environment memory right after validation, and never places full JSON in `workflow_dispatch` inputs, Git, logs, or plaintext artifacts.
- Old proposals or fixed digests cannot substitute for new proposals. One-time execution is enforced by exact proposal digest plus dynamic archive existence guard on `MULTI_ASSET_STATE__before_rebase_{proposal_run_id}`: re-applying fails the archive guard without overwriting or clearing prior archive chains.
- Before applying, account identity is verified via protected confirmation chains / control source validation rather than hard-requiring optional `source.original_evidence`; structural mismatches return fixed sanitized reason codes (such as `prospective_control_identity_unverified`) instead of swallowing into contextless `migration_blocked`.
- Downstream `diagnose`/`prepare`/`verify` pass `proposal_run_id` + `source_sha` + dynamic archive/digest for new baselines; the historical `34690028846` archive is recognized read-only and never grants new approvals.

`application/earn_accrual.py` provides pure-`Decimal` incremental conservation
reconciliation requiring callers to supply independently verified non-interest net changes
per asset; balance deltas may never be inverted into assumed cash flows. This helper is not
wired into daily automated accounting or recovery consumers, so it does not mark production
automation complete. Real migration still requires concrete snapshot confirmation plus
end-to-end forward consumer and recovery verification.

Out-of-scope asset names in balance responses may contain Unicode, are deduplicated by raw name without entering managed scope, and still validate amounts and locks per row. Reference: [Binance REST API Unicode example](https://developers.binance.com/en/docs/products/spot/rest-api).

## Forward Earn Accounting Consumer (2026-09-12)

The new path activates only for ledgers already carrying `earn_accrual_checkpoint`; older
ledgers stay on the legacy path with no automatic migration. Checkpoint and
`earn_accounted_net_changes` remain preserved across loads. Diagnostics and recovery checks
perform bounded dual sampling (`Spot1 -> Earn1 -> Spot2 -> Earn2`) comparing quantities,
products, and real-time reward counters; `sampling_stable` now requires Spot/Earn component
deltas to be explained by real-time counters rather than relying only on Firestore
owner/ledger/control markers. Valuation and accounting use one complete Spot/Earn sample;
the owner-protected writer persists the next checkpoint, flow cursor, balances, and cleared
accounted trade net changes atomically. Failed validation never advances state, and failed
writes neither update in-memory checkpoints nor auto-retry.

Quantity deltas reconcile against same-product counter increments, persisted filled trade
net quantities/fees, and verified same-day USDT deposits. Trades accumulate raw `Decimal`
amounts and asset-specific fees during `FILLED_ACCOUNTING_PENDING -> TERMINAL` rather than
deriving flows from balance differences. Mid-cycle funding checks can update current balance
snapshots without advancing reward checkpoints or flow cursors independently, avoiding
missed interval cash flows. Earn rewards already reflected in equity are never
double-counted as extra profit or external principal.

`earn-forward-diagnose` keeps raw sample rows in memory only; public outputs report asset
direction, reward/funding residual direction, record counts, and match flags. BNB Wallet
`dribblet` diagnostics separately report row readability, visible row counts, and window
completeness; missing/invalid `total`, full pages, duplicates, out-of-window items, or
non-BNB dividend rows keep status unverified without silently dropping seen rows or
misreporting partial windows as complete.

If a run fails solely during pure-Earn prep checks on `daily_state`, while this run
successfully acquired owner lock, loaded orders are strictly `RESERVED`/`TERMINAL` and
settled, zero pending funds exist, and prep made no new persistent write intents, it
releases only this run's own owner lock. Unknown orders, uncertain write outcomes, and
pre-existing legacy owner locks stay untouched rather than being auto-cleared.

Unknown/incomplete orders, disappeared or substituted products, counter regressions, new
withdrawals, and unsupported-asset deposits continue rejecting. Internal
subscriptions/redemptions do not count as yield, conservation only applies directly across
continuous same-product quantities without guessing new cumulative baselines on
discontinuity, and this does not claim automatic classification for all capital events.

New opening proposals also carry zeroed `earn_accounted_net_changes` across all assets and
remain non-executable migration candidates; September 11 apply/recovery roots stay
unchanged. New migrations require human confirmation on concrete snapshots followed by
recovery binding over real migration and complete archives; this does not switch baselines,
clear circuit breakers, or authorize live trading.

## History: Approved One-Shot New Opening Migration (2026-09-12, Runtime `34690028846`)

The record below documents a completed historical DB migration and is **not** a current
operating instruction. Current `prospective-rebase-apply` binds to the active
`rebase-proposal`'s `proposal_run_id` and full approved proposal; the old fixed digest and
`MULTI_ASSET_STATE__before_rebase_34690028846` serve solely to recognize that archived
baseline and cannot approve new proposals.

At that time, the user approved Runtime `34690028846`, read in memory via temporary
protected `BINANCE_APPROVED_PROSPECTIVE_OPENING` without persisting account content into
Git, logs, or plaintext artifacts. A single transaction archived the whole legacy ledger to
`MULTI_ASSET_STATE__before_rebase_34690028846`, preserving prior archive chains without
clearing circuit breakers, changing strategies, or resuming schedules/trading.

## Read-Only Verification of the New Opening Baseline (2026-09-12)

When the ledger points to the approved new archive, `recovery_action=diagnose` verifies the
real migration `34690695663@a3ef5660e6d25fcfd5a7dedd10536a32eedde203` against the frozen
ledger, archive, and approval materials. Reusing the forward accounting consumer, it checks
dual Spot/Earn samples, reward counters, and verified flows in memory while enforcing
conservation between both samples without guessing principal from balance deltas. It
inspects trades across managed symbols and full-account open orders since the new opening,
reading back ledger, control, and archive unchanged with no owner lock held. Historical
unknown deltas stay preserved.

The opening-to-observation window remains bounded by a `MAX_HISTORY=7`-day **single window**
that must never be widened, removed, or bypassed. Beyond 7 days, `historical_continuity`
splits opening-to-current into contiguous, millisecond-joined `<= 7`-day chunks requiring
complete pagination, cross-chunk reward deduplication, and rejection of unsupported events
such as trades, deposits, withdrawals, transfers, or Earn subscriptions/redemptions.
Existing archives remain immutable opening anchors while the live ledger binds
`current_ledger_sha256`; ledger `earn_accrual_checkpoint` cannot act as a trusted continuity
start anchor. If quiesced control retains legacy confirmation/`transition_plan`, operators
must verify that prior activation binding first and collect fresh historical evidence before
preparing a new candidate rather than deleting old confirmations to bypass gates.

`scope-preview` selects legacy vs. prospective archive from the ledger's effective
`accounting_rebase.archive_document` and still sends one ack-required POST to the private
console without writing Firestore or auto-enrolling assets. This check neither writes
ledgers, generates candidates, nor publishes console approvals; existing
prepare/verify/activate roots stay unchanged. Passing proves forward accounting consistency
for that opening baseline only, not trading recovery or full daily business acceptance.

## Production Recovery Wiring for the New Opening Baseline (2026-09-12)

`prepare`/`verify`/`activate` and runtime consumers now recognize `prospective_rebase`.
Shared QPK and console protocols remain unchanged: candidates still bind real Spot balances,
open orders, trades, and full ledgers, while Flexible Earn growth is proven separately by
forward conservation from approved checkpoints without copying Earn deltas into expected
balances or altering historical unknown deltas. Final Spot sampling must agree with the
conserved checkpoint and stay stable after order reads.

During prepare, only the original archived control or a replacement candidate of the same
kind whose source, successful production run, and expired 30-minute TTL have all been
strictly verified is permitted. Updates use existing atomic owner/ledger/archive/previous
CAS conditions and publish sanitized candidates to the admin site. Unexpired candidates are
never overwritten; explicit regeneration replaces both `recovery_id` and candidate,
invalidating prior confirmations.

`verify`/`activate` reads admin confirmation for that exact candidate, then re-collects and
verifies forward conservation and all five Spot/order/ledger digests against the confirmed
candidate. Only `activate` performs the atomic control transition, and runtime consumes
`ACTIVE_LKG` only after validating bindings across candidate, source, confirmation, and
transition plan. None of these paths enable `RUNTIME_TARGET_ENABLED`, clear circuit
breakers, alter strategy/risk budgets, or place orders; live activation and runtime require
explicit authorization, and local closed-loop tests never substitute for real human
confirmation or daily cycle acceptance.

### Stored Candidate Readback and Failed-Source Replacement

Firestore encodes tuples as arrays and reads them back as lists. Readback verification
compares full content using the existing canonical digest so equivalent representations do
not trigger false uncertain-commit failures while any real field mutation still rejects.
Regression tests round-trip full candidates through real Firestore SDK codecs rather than
simplified controls.

Completed runs with `failure` conclusions remain ineligible for activation. Only explicit
`prepare` may replace strictly verified unactivated candidates from failed runs, requiring
`RECONCILE_ONLY`, no owner, no confirmation/transition, and exact match on original run
repo, `main`, workflow, SHA, and run ID. In-progress or cancelled runs are rejected;
successful runs remain replaceable only after expiry. Each replacement performs full
re-sampling with a fresh ID under CAS against the prior control record, and
`verify`/`activate` still accepts only successful runs and fresh human confirmations. This
is not an automatic retry; recovery after real failures continues to follow stop and
re-authorization rules.

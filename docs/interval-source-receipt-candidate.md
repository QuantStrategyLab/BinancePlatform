# Interval source receipts: LOCAL CANDIDATE

## Decision

The simpler source-retention seam is feasible in the existing Firestore-native
transaction API: create one per-interval receipt and patch the five existing
forward-accounting fields in the same transaction. A separate pending queue,
PerformanceStore delivery ACK and queue-clear write are unnecessary for that
bounded source-retention property. The candidate now integrates that helper
through one owned/versioned bound runtime port. This is a local source candidate,
not an activated runtime, provider verification or consumer qualification.

Receipts are **create-once under this candidate API**, not storage-level immutable
or WORM. IAM, other writers, retention/PITR and physical production identity remain
unverified. Archival storage/read costs still grow and need a separate decision.

## Verified source seams and precise gaps

- Binance main `ab7daec02c3bf2e58ee5fa363c26c2124f7e497f` and source release
  `8cb56617115fa45028e34d788e71884b6a303d77` have matching local/API Git blobs for
  `live_services.py`, `runtime_support.py`, `trade_state_support.py` and
  `scripts/migrate_daily_accounting_state.py`. The earlier interval review also
  verified the producer/adapter and QPK pin `8e8ec51884bf8abb0a7ca699fc2da39fad134f89`.
- [Bound state access](https://github.com/QuantStrategyLab/BinancePlatform/blob/ab7daec02c3bf2e58ee5fa363c26c2124f7e497f/live_services.py#L48-L86)
  closes load/save/claim/release over one store. However, its load returns only a
  dict, losing snapshot update_time; its save is a blind `set`; the closure does
  not expose a receipt transaction seam. Integration must retain the actual bound
  client, refs and exact snapshot version, not call another provider factory.
- [Migration transaction](https://github.com/QuantStrategyLab/BinancePlatform/blob/ab7daec02c3bf2e58ee5fa363c26c2124f7e497f/scripts/migrate_daily_accounting_state.py#L599-L679)
  already reads ledger/owner/archive before `create` + ledger `update`, then checks
  exact digests after commit. Migration requires owner absent; this runtime-shaped
  candidate instead requires the held owner ID inside both transactions.
- Release/main lockfiles pin native Firestore `2.28.0`. Its exact official source
  exposes reference `_client` and `_document_path`, client `_database_string`,
  and transaction `_client` / `_max_attempts`: [reference](https://github.com/googleapis/google-cloud-python/blob/google-cloud-firestore-v2.28.0/packages/google-cloud-firestore/google/cloud/firestore_v1/base_document.py#L137-L165),
  [client](https://github.com/googleapis/google-cloud-python/blob/google-cloud-firestore-v2.28.0/packages/google-cloud-firestore/google/cloud/firestore_v1/base_client.py#L239-L257),
  [transaction](https://github.com/googleapis/google-cloud-python/blob/google-cloud-firestore-v2.28.0/packages/google-cloud-firestore/google/cloud/firestore_v1/base_transaction.py#L65-L70).
  Candidate checks that private, version-specific seam explicitly and fails closed
  if missing. SDK upgrades require re-verification; no native client was created.
- [Pinned DocumentStore](https://github.com/QuantStrategyLab/QuantPlatformKit/blob/8e8ec51884bf8abb0a7ca699fc2da39fad134f89/src/quant_platform_kit/cloud/ports.py#L108-L146)
  has no receipt enumeration/query contract. The pinned GCP implementation exposes
  a native client, but that does not establish archive pagination, completeness,
  ordering, authorization, indexes or cost. No archive reader is implemented.

## Candidate contract

`application/interval_source_receipt_candidate.py` has no provider imports,
backend defaults, client construction, normal runtime registration or delivery.
The caller supplies the existing bound native client, refs, held owner ID, exact
observed ledger update_time, and a transaction runner. Both callbacks require
`max_attempts=1`; all document reads precede writes.

1. Accept only the existing exact seven-field interval, unchanged. Hash canonical
   JSON of that exact payload. Logical receipt ID hashes scope plus normalized UTC
   start/end; payload hash and source/patch metadata stay outside the payload.
   Same logical ID with changed payload/metadata is a conflict, never a replacement.
2. Freeze serialized plan material, then independently revalidate it before any
   transaction: schema, logical ID/path, payload hash, source digest/version
   bindings, canonical JSON and exact five-field patch hash. Frozen dataclasses
   alone are not a trust boundary; constructible/replaced plans are checked.
3. Before any document `get`, require ledger/owner/receipt/transaction to use the
   exact same bound client object and fully qualified database/document paths.
   Relative paths alone are insufficient. This checks local binding, not an
   attestation of the intended production database or its external policies.
4. Transactionally verify matching owner and full-precision snapshot version.
   Version token preserves seconds plus all nanoseconds; ordinary datetime
   equality/isoformat can discard a Firestore sub-microsecond tail. For create,
   require the actual pre-ledger digest and regenerate the complete expected plan
   from that transactional ledger, allowed patch and interval before any write.
5. `create(receipt)` and `update(ledger, five-field patch)` commit together. Only
   checkpoint, cash-flow cursor, accounted net changes, last balance snapshot and
   daily external principal are eligible. Unrelated audit/fill/fee fields remain
   untouched. This API has no receipt `set`, overwrite, truncation or deletion.
6. A second transaction performs exact receipt + post-ledger readback while the
   owner still matches. Commit/readback exceptions or mismatches raise a fixed
   uncertain result. They never establish an ACK or prove rollback. An explicit
   retry uses the original plan plus a freshly observed current version; exact
   receipt/poststate/patch match is a no-write replay, so principal/fees are not
   applied again. A progressed later ledger fails closed in this helper; old
   receipt retention remains independent and a future reader can inspect it.

The caller must already have passed the existing `prepare_forward_earn_state`
conservation, cursor-history, known-fill, price-sampling, enabled/non-dry-run and
order-certainty gates. The helper checks structural binding, not those business
facts or provider-history coverage. It does not implement withdrawals, expand
account scope, invent historical intervals, reconstruct principal from daily
totals or grant trading authority.

## Reader proposal and stopping point

A separately reviewed read-only reader could return each verified receipt's raw
seven-field `interval` to the [existing QPK interval calculator](https://github.com/QuantStrategyLab/QuantPlatformKit/blob/8e8ec51884bf8abb0a7ca699fc2da39fad134f89/src/quant_platform_kit/strategy_lifecycle/live_equity.py#L294-L482).
That requires a proven archive query/pagination/order contract, scope and cutover
coverage, corruption/conflict handling, access and cost limits, and consumer
adoption tests. Neither this proposal nor existing QPK parsing proves coverage.
No current recorder/monitor starts reading these candidate receipts automatically.

The bound-port candidate below closes the ordinary/submission/forward writer
seam in this source version. Other old binaries, direct native maintenance or
external writers remain outside its fence. Mixed-writer quiescence and the
actual native backend still require qualification before adoption. Existing post-owner-release best-effort recording must not become
the source ACK. Backend identity, IAM,
retention, storage size/cost and historical completeness remain open decisions.
No queue-capacity/trading-stop policy or archival retention policy was selected.

## Original helper prerequisite verification

- 37 dependency-free fake-store tests passed; focused Ruff and py_compile passed
- Tests cover atomic rollback including second-write failure, owner/version races,
  sub-microsecond CAS, mixed backends before reads, create/identical/conflict,
  lost commit response, failed/conflicting readback, malformed and replaced plans,
  source regeneration, exact retry without principal/fee re-accounting, normalizer
  reload and retained earlier receipts after later progress/failure
- Plan-integrity regression was reproduced RED with `dataclasses.replace`, then
  fixed and verified; it was not covered by the first fake-test pass
- Only this new helper, its new test file and this document differ from the
  isolated baseline. Existing live services, runtime writer, normalizer, workflows,
  pins and default entrypoints are byte-for-byte unchanged
- No real Firestore/GCP/broker/provider calls, credentials, business data, costs,
  SDK client construction, code commit, publication, deployment or consumer
  qualification. Read-only source retrieval is separate from fake execution

Run: `python3 -m unittest discover -s tests -p test_interval_source_receipt_candidate.py -v`

## Bound runtime source candidate (local, not activated)

`live_services.bind_trade_state_access` now returns one `BoundTradeStateAccess`,
still iterable as the existing four `load, save, claim, release` callables. It
owns the one already-selected store/client, native ledger/owner references,
claimed owner ID, detached raw ledger and exact seconds+nanos update time. The
working state is a separate copy; mutating it cannot change the source CAS token.
Normal saves merge only the configured normalizer's output onto that detached
raw source, preserving source-only future fields under the same exact CAS. No
unknown caller input bypasses the normalizer.

All candidate normal and order/Earn submission-state writes converge on
`runtime_support._persist_runtime_state`. The native session transaction reads
actual owner and ledger before staging a write, compares the complete raw source
and full version, and verifies the intended body/owner/full version in readback. Changed bodies
require a later version; an exactly unchanged body may retain the prior version,
as documented by native 2.28 `WriteResult`.
Cached runtime owner flags are only a preliminary gate. A revoked/replaced owner
fails even when the local flag remains true. Release checks actual owner and,
when loaded, the same ledger source; it cannot release another owner or a changed
source. No setter calls `DocumentStore.set` or refreshes a stale version.

Public native saver admission deliberately changes: `main.set_trade_state`,
`infra.state_store.save_runtime_trade_state` and `live_services.save_trade_state`
require an explicit matching `bound_access`. A bare store, different ledger,
normalizer or fabricated capability is insufficient; an unbound native saver
returns false without selecting a provider or requesting a write. The bound
session itself raises fixed sanitized failures. Explicit custom memory savers
retain their previous infra keyword contract and cannot also supply native
capability. Startup/ForbiddenWrite, replay/memory, imports, ordinary state reads,
maintenance tools and Telegram/telemetry read paths retain their separate APIs.
Arbitrary external custom writer code cannot be fenced by this Python port.

Receipt retention is a read-only construction option, default false:
`main.build_live_runtime(retain_interval_receipts=True)` selects it explicitly
for a permitted non-dry candidate runtime. No environment variable, source pin,
workflow or deployed switch was added/changed. Dry-run or execution-disabled
builders do not bind this mutation port. Invalid flag types fail before the live
builder. With receipt retention enabled, loading a checkpointless ledger fails
closed, and ordinary saves cannot alter/remove checkpoint or cash-flow cursor.
Forward accounting alone supplies the verified unchanged seven-field interval.
The existing helper creates the receipt and applies its exact five-field patch
before working-state replacement or report interval publication. Later metadata,
daily-reset, action, submission and final saves preserve the advanced checkpoint.
No strategy-loss reader adoption or native archive enumeration is included.

### Exact Firestore 2.28 transport seam

Every BeginTransaction, document BatchGet, Commit and Rollback RPC uses
`retry=None`, `timeout=10`, and the existing client's RPC metadata. Owner
create uses the original native reference with those same retry/timeout bounds.
Each operation has a finite, fixed transaction/read count; there is no pagination
or unbounded retry loop. Transaction callbacks have `max_attempts=1`, but this is
not the transport retry control.

Installed 2.28 `Transaction._begin`, `_commit` and `_rollback` do not accept retry
or timeout controls. The decorator calls them with default GAPIC retries.
Inherited `WriteBatch.commit(retry=None, timeout=10)` also omits the native
transaction ID, so it is not used. The port calls the exact existing bound
client's GAPIC Begin/Commit/Rollback directly, retaining the original native
transaction ID, write protos and clean-up semantics. Native-shaped reference
views inject the bounds into the existing helper's `get` calls; their `parent`
returns another bounded view for receipt reads. Native 2.28 create/update use
`_document_path` by duck typing; reads delegate to the original reference with
the original native transaction. No `transaction.get(view)` is used.

Loads and all readbacks begin actual read-only transactions. A fresh two-call
runner per receipt invocation performs read-write staging then read-only
receipt/poststate readback, rejecting reuse; the port additionally reads the
verified ledger version for its next operation. Read-only/empty transactions
close via one bounded Rollback. Cleanup rollback after a failed Commit never
proves rollback, and is attempted at most once. There is no automatic native
Commit, callback, reload or receipt retry after uncertainty.

Public claim/load/save/release admission and completion are serialized by one
short-held per-session lock. The lock is never held across an RPC. A concurrent
or reentrant public call fails busy immediately and terminally invalidates the
session; it cannot refresh another operation's detached source. `active` is false
while an operation is in flight. External `invalidate()` latches immediately,
and the latch is never reset after construction. Checks before Begin/Commit,
after RPC completion and atomic source/owner publication prevent an in-flight
operation from rearming or reporting success after mid-RPC invalidation. A Commit
already in flight may still take effect; it is not cancelled or rolled back by
local invalidation. Normal operation completion and release closure clear busy
atomically under that same lock, without an invalidation/rearm window.

A failed operation invalidates the source session. Native persistence uncertainty
also clears the runtime's local owner-held flag, so subsequent broker mutation
calls cannot continue even if the caller catches the initial error. A lost
response may leave an atomic receipt/poststate or submission marker committed;
the in-memory checkpoint/report is not advanced and the owner is not released.
Recovery requires a separately qualified decision/readback. The existing pure
helper's explicit original-plan replay contract remains unchanged; it is not
silently exposed as a runtime continuation after uncertainty.

### Source consumers and rollout limits

The actual application pin remains `8cb56617115fa45028e34d788e71884b6a303d77`.
The verified workflow selector is `66d705fd756f5648f603bfd745235b5ef389f668`;
its application/startup/migration/recovery commands check out the selected
application release before running. The account-facts reader independently uses
`ab7daec02c3bf2e58ee5fa363c26c2124f7e497f`. Both selector/reader refs are configured
at repository scope; name-only environment lookup confirms no `binance-runtime`
overrides for these two refs. This is source readback, not runtime qualification.

CI, shadow/replay, heartbeat/target/watchdog and other manual workflows may use
their event/default-branch checkout rather than the application pin. In-repo
shadow/replay uses explicit memory writers, monitors do not call the native
ledger setter, PAPER preview imports only Telegram, and maintenance scripts use
separate owner-absent native transactions. The complete existing compatibility
suites are required for this deliberate public saver API change. External users
of the old native setter must migrate to explicit bound-session admission.

At real cutover, establish source/database/ledger/account/stream identity and
coverage start, quiesce every old binary and maintenance/external writer, and
qualify native identity/rights, retention/PITR, read completeness and costs.
Python fencing cannot revoke another actor's direct Firestore rights. The
maintenance timestamp precision contract remains separately unqualified; do not
claim it is equivalent to this full-nanosecond runtime CAS. Receipt creation off
ends canonical coverage; it cannot synthesize history or undo the checkpoint.
Rolling back to the old unfenced binary is not a qualified source rollback.
A retained source receipt can feed the existing calculator directly; a second
PerformanceStore sink/ACK is unnecessary. No backend, broker, Runtime dispatch,
deployment or credentials were exercised to establish these source properties.

### Verification distinction

The candidate tests exercise the real installed Firestore 2.28 reference,
transaction, protobuf serialization and SDK timestamp objects using an anonymous
synthetic client with all RPCs stubbed, credential lookup and network forbidden.
That establishes local SDK compatibility and request bounds, not native backend
durability, IAM, server transaction semantics or production identity. Independent
native-shaped offline fakes exercise atomicity/races/uncertainty, and full-cycle
synthetic tests establish forward-before-reset/submission ordering. Full suite,
Ruff, compile, exact dependency source provenance and the final frozen write-set
checks must pass before review/publication. The actual runtime is not changed by
this isolated candidate.

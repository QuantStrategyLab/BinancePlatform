# Interval source receipts: LOCAL CANDIDATE

## Decision

The simpler source-retention seam is feasible in the existing Firestore-native
transaction API: create one per-interval receipt and patch the five existing
forward-accounting fields in the same transaction. A separate pending queue,
PerformanceStore delivery ACK and queue-clear write are unnecessary for that
bounded source-retention property. This is an isolated helper/fake-test candidate,
not runtime integration, provider verification or consumer qualification.

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

Future integration needs a reviewed opt-in writer/version seam in the same bound
state backend before the current checkpoint write. Existing ordinary saves remain
blind `set`: enabling this helper alone does not fence concurrent/legacy writers
or prevent a later stale ledger overwrite. Mixed-writer rollout, owner lifecycle,
and bounded native RPC timeout/attempt configuration require integration review
before adoption. Existing post-owner-release best-effort recording must not become
the source ACK. Backend identity, IAM,
retention, storage size/cost and historical completeness remain open decisions.
No queue-capacity/trading-stop policy or archival retention policy was selected.

## Local verification

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

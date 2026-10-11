# Retained interval reader: LOCAL PREREQUISITE

## Result and hard stop

This candidate validates **supplied** retained receipts and prepares an ordered,
detached in-memory preview using the existing lifecycle record envelope. It does
not read a backend, enumerate an archive, deliver records, acknowledge them,
advance a cursor, clear receipts or register with runtime. Every preview reports
`blocked_archive_enumeration_and_durable_ack_unqualified` and
`delivery_acknowledged=False`. The default runtime remains unchanged.

The missing native seam is qualified acquisition of the existing retained source
archive, with explicit physical identity, complete bounded enumeration, cutover
and retention evidence. As the later
[bound source contract](interval_source_receipt_candidate.md#source-consumers-and-rollout-limits)
states, retained receipts can feed the existing calculator directly. A second
PerformanceStore sink or delivery ACK is unnecessary for that read-only path.
The preview's existing delivery-status string is retained for compatibility; it
is not a requirement to create another sink. No ACK adapter is fabricated here.

## Existing consumer contracts inspected

The application pins QPK `8e8ec51884bf8abb0a7ca699fc2da39fad134f89`.

- [PerformanceStore generic writes](https://github.com/QuantStrategyLab/QuantPlatformKit/blob/8e8ec51884bf8abb0a7ca699fc2da39fad134f89/src/quant_platform_kit/strategy_lifecycle/performance_store.py#L159-L171)
  write local material before optional cloud material. The default local root is
  temporary storage. No create-if-absent, atomic/fsynced local write, typed ACK or
  immutable exact-payload readback is established.
- [Live run save/list](https://github.com/QuantStrategyLab/QuantPlatformKit/blob/8e8ec51884bf8abb0a7ca699fc2da39fad134f89/src/quant_platform_kit/strategy_lifecycle/performance_store.py#L386-L469)
  use profile/stream/timestamp keys and overwrite writes. Save returns `None`;
  cloud reads/lists suppress errors. A local read or apparently empty cloud list
  is not evidence of durable acceptance or archive completeness.
- [PerformanceMonitor](https://github.com/QuantStrategyLab/QuantPlatformKit/blob/8e8ec51884bf8abb0a7ca699fc2da39fad134f89/src/quant_platform_kit/strategy_lifecycle/performance_monitor.py#L346-L444)
  returns `ok` after the configured store returns. The best-effort helper returns
  `None` on both success and failure. Neither qualifies consumer durability.
- [Existing interval calculator](https://github.com/QuantStrategyLab/QuantPlatformKit/blob/8e8ec51884bf8abb0a7ca699fc2da39fad134f89/src/quant_platform_kit/strategy_lifecycle/live_equity.py#L294-L557)
  accepts the exact seven fields via `execution_result.external_cash_flow_interval`.
  This reader reuses that envelope and preserves the seven-field payload verbatim.
  Returns use the existing observation/end-flow convention, not exact TWR.

## Pure prerequisite API

`application/interval_receipt_reader_candidate.py` imports the receipt producer's
`_FIELDS`, `_SCHEMA`, `_payload_material`, `_time`, `_validate_path`, `_digest` and
`_json`. There is one payload/identity implementation and no reverse import,
backend dependency, environment lookup, provider default or client factory.

`prepare_retained_interval_preview` takes:

- A materialized list/tuple of `(receipt_path, receipt_dict)` pairs, without SDK
  snapshots or lazy generators. The caller owns acquisition and provenance.
- An explicit relative ledger path and account scope hash; explicit cutover start
  and end timestamps. There is no automatic historical discovery or filtering.
- An explicit strategy profile and lifecycle stream ID. Each is restricted to
  1–100 ASCII letters, digits, underscores or hyphens to avoid introducing key
  sanitizer ambiguity. These labels do not qualify a real destination.
- Required positive `max_receipts` and `max_bytes`, with hard ceilings of 1,000
  supplied entries and 1,048,576 bytes. Bytes are the sum of each UTF-8 path and
  the producer's canonical JSON receipt; duplicates count against both budgets.

Before producer deepcopy or Decimal work, the reader checks the exact producer
field set and bounded string-only values. It then uses the producer's unchanged
numeric limits: finite string amounts of at most 80 characters, magnitude at most
`1e30`, exponent at least `-30`, and positive ending equity. Timestamp precision,
scope, USDT, valuation basis and interval length retain producer validation.

The reader checks exact receipt/source keys, schema, logical ID, payload checksum,
expected receipt path, one ledger/scope, valid nine-digit-nanosecond source version
and the shape of source digest fields. It does **not** regenerate source state or
patch hashes: those underlying materials are absent from a retained receipt.
Hash consistency is not authenticity, immutability or a business-evidence check.

Identical canonical receipt material deduplicates. Same normalized interval ID
with changed raw payload or source metadata conflicts. Thus timezone-equivalent
timestamps produce one logical ID but changed lexical payloads still conflict,
as they do in the producer. Multiple starts at one ending timestamp conflict.

All supplied receipts must be inside the requested cutover. After sorting,
intervals must span its exact endpoints without gaps or overlaps, and source
versions must strictly increase at full nanosecond precision. No later component
is silently substituted for missing earlier material. Intervening unrelated
ledger writes are allowed, so adjacent whole-ledger digests are not falsely
required to be equal. Scope/path/version checks reject visible mixing; identical
relative paths from different physical databases cannot be distinguished here.

The output contains only the existing execution envelope and validated interval,
plus producer interval IDs/payload digests and resource counts. Receipt source
metadata, ledger paths, prior balances and unrelated private ledger fields are
not copied into lifecycle records. JSON material is frozen and returned as fresh
copies; repr omits financial payloads. Errors have fixed reasons without input
material or exception chaining. Constructible preview objects are inspection
values, not trusted authority to persist or acknowledge anything.

## Completeness, retry and adoption limits

Contiguity proves only that the supplied batch spans the caller's requested time
range. It cannot prove that enumeration was complete, conflicting receipts were
not omitted, the cutover was correctly chosen, the source writer was activated,
or history/provider data were complete. No balance, flow, missing interval,
historical checkpoint, withdrawal support or execution authority is reconstructed.

Reader retries are deterministic for identical input and never re-account funds.
The actual store regression exercises its existing local-before-cloud write order
with a synthetic throwing cloud hook: partial local bytes remain after failure,
and retry targets the same timestamp key. This demonstrates uncertainty; it does
not declare success, rollback, exactly-once delivery or durable ACK. No network,
cloud list/read, credential or real store factory is used in that regression.

Future work requires separately qualified archive identity/query/pagination and
completeness, access/cost/retention bounds and explicit cutover. Source/backend
qualification and mixed-writer runtime adoption remain separate. Direct
calculation does not require a sink ACK; adding a sink would require its own
ACK/readback/idempotency qualification. No receipt deletion or new queue/service
is proposed. Runtime switch, source release pin, workflows and ordinary writes
are unchanged; no Runtime dispatch or deployment is authorized by this candidate.

## Native input preflight and existing handoff

The 2026-10-05 offline preflight is against Binance main
`db87064bbd89a67d021694e02bcdbc337c164180` and its unchanged QPK pin
`8e8ec51884bf8abb0a7ca699fc2da39fad134f89`. The existing reader already separates
supplied-batch integrity from native authenticity and completeness. No additional
preflight API, receipt format, archive service or production query is needed to
represent this distinction.

Available evidence establishes the source/reader code and synthetic regressions.
It does not establish a native archive, physical database or authorized protected
read entry, actual account scope, retained window, cutover, retention/PITR policy,
terminal-page enumeration or current runtime adoption. Missing evidence means
unqualified, not proof that an archive does not exist. The default-off
construction option does not establish the flag of a running process.

The following is an evidence handoff checklist, not a new accepted schema or
authorization to acquire protected records:

1. Source and permitted entry: original retained-export location or approved
   read entry, provenance/checksum, observation time, read identity and permitted
   fields/window; exact producing application revision and backend/SDK version.
   Do not construct another client or infer a cloud principal from this document.
2. Physical binding: full `projects/{project}/databases/{database}` identity,
   original relative ledger path, native account-scope digest and its original
   account binding; chosen strategy profile and lifecycle stream. The source
   defaults `strategy/MULTI_ASSET_STATE` and sibling document pattern
   `<ledger>__interval_receipt_<interval_id_sha256>` are code conventions, not a
   verified production entry. Identical relative paths do not bind a database.
3. Window and cutover: exact requested receipt start/end, producing revision,
   first retained checkpoint and full source version, retention-enable and
   disable/gap boundaries, and old/maintenance/external-writer quiescence evidence.
   Rebase dates, current balances or a first observed receipt cannot create an
   earlier retained start or opening equity.
4. Enumeration: original query/filter/ordering definition, one authoritative
   as-of/read snapshot across all pages, each requested/returned cursor, page
   count and bytes, document identities, terminal marker and all errors or
   truncations. Complete source enumeration must precede selection of the
   bounded reader batch. Hash-suffixed document IDs are not chronological, and
   raw interval time strings can use equivalent UTC offsets; neither lexical
   timestamp filtering nor contiguity proves complete enumeration. No native
   query/index or snapshot-pagination contract is qualified by this preflight.
5. Retention and access: actual receipt retention/PITR and deletion history for
   the window, read completeness and limits, permitted principal, indexes and
   storage/read-cost bounds. Create-once API behavior is not WORM or IAM evidence.
6. Business source: original checkpoint/flow/fill/fee/price provenance sufficient
   for the supported Spot plus Flexible Earn scope; USDT external deposits must
   remain distinct from same-account movements. The seven-field receipt alone
   cannot prove these gates. Unsupported withdrawals, unknown fees, FX or scope
   changes remain unavailable; do not substitute wallet estimates or daily totals.

For each supplied receipt, preserve exactly these existing fields:

- `schema_version`, `interval_id_sha256`, `payload_sha256`, `source`, `interval`
- Source: `ledger_path`, `ledger_before_version` (nine-digit UTC nanoseconds),
  `ledger_before_sha256`, `ledger_after_sha256`, `patch_sha256`
- Interval: `account_scope_sha256`, `start_at`, `end_at`, `end_equity_usdt`,
  `net_external_cash_flow`, `currency`, `valuation_basis`

Keep original receipt paths, unchanged field values and export-byte checksums
with external provenance. Do not
change lexical timestamps, regenerate hashes, fabricate ledgers or repair missing
pages to make an export pass. Redaction that changes covered receipt material
cannot be presented as the original native payload. A synthetic fixture, a
redacted inspection copy and an unchanged native export are different inputs;
successful parsing does not promote any of them to verified native evidence.

Once original supplied material is independently qualified, the existing local
calculation path is:

1. Materialize bounded `(original_receipt_path, original_receipt_dict)` pairs.
2. Call `prepare_retained_interval_preview` with the separately verified ledger,
   account-scope digest, exact receipt-window endpoints, explicit profile/stream
   and count/byte budgets. Hard limits stay 1,000 entries and 1,048,576 bytes,
   counting duplicates. Exceeding them blocks this batch; do not silently truncate
   or concatenate independent batches to claim a whole-window pass.
3. Pass `preview.live_run_records()` directly to the existing pinned
   `live_run_records_to_return_series_result(..., domain="crypto")`. No store
   write, delivery ACK, receipt deletion or cursor change is part of this path.
4. Report native qualification separately from the calculator's status, and
   disclose account scope, currency, valuation basis, receipt window, calculable
   return window, gaps/truncation and observation/end-flow method.

The receipt window and return window differ. The pinned calculator uses the
first ending-equity observation as its opening anchor, groups observations/flows
by UTC end date and requires consecutive observed days. Three receipts covering
`t0→t1`, `t1→t2`, `t2→t3` across three successive end dates yield returns for
`t1→t2` and `t2→t3`; they do not establish a return for `t0→t1`. A single receipt
or multiple receipts ending on one UTC date are insufficient. The calculator
can retain only a later component when other inputs have a missing day; this
reader instead rejects a supplied receipt gap. Receipt contiguity and nonempty `ok`
therefore cannot certify a requested whole return window. Coverage disclosure is
the separately coordinated QPK consumer contract; this reader does not change it.
Do not invent an opening-equity point or label these observations exact TWR or
natural-midnight-day returns. Benchmark comparisons also need independently
qualified same-currency, same-return-window total-return inputs.

## Offline preflight verification (2026-10-05)

- The complete 185-file Binance fixed-main tree and modes match the local
  baseline; the only proposed change is this document. All 184 other files,
  including producer, reader, tests, runtime, pins and workflows, are unchanged
- All 496 blobs of the existing exact QPK pin were checked before execution
- The 35 existing reader tests and 37 existing source tests pass with network,
  credential lookup and provider factories denied: zero attempts
- Additional synthetic observations confirm that a single receipt and two
  receipts ending on one UTC date produce `insufficient_observations`; three
  successive end dates produce two return points
- The documentation patch applies to the exact baseline and its relative source
  link/section resolves. No production behavior changed, so no code RED/fix is
  claimed. Full application tests and lint were not rerun for this docs-only
  preflight; the historical results below are separate
- No native archive was acquired or qualified, and no protected data, broker,
  credentials, runtime dispatch, publication, deployment or adoption was used

## Original reader candidate verification (historical)

- 35 new synthetic reader/consumer tests and 37 existing receipt fake tests pass
  with network and cloud factories denied: zero attempts
- Exact pinned QPK calculator/store source hashes are checked before execution;
  actual calculator roundtrip produces synthetic 5% and 10% end-flow returns and
  remains identical after duplicate input
- Coverage includes malformed material, cutover gaps/endpoints/overlaps, conflicts,
  duplicates, source/path/scope mixing, canonical timestamp equivalence, full
  nanosecond source ordering, count/byte/numeric bounds, detached output, privacy,
  deterministic reader retry and actual store partial-local-write failure/retry
- `py_compile` and focused Ruff `0.15.9 --no-cache` pass using an existing tool.
  The project lockfile specifies Ruff `0.15.20`; that exact version was not run
  locally, and no new lint dependency was fetched. These are focused checks, not
  a full application or native backend qualification
- Only this new reader, its new test file and this document are proposed additions;
  all 182 prior files remain byte-for-byte unchanged

With project dependencies installed:

`python3 -m unittest discover -s tests -p test_interval_receipt_reader_candidate.py -v`

The local verification runner uses the retained exact-pin consumer/calculator
sources and installed pandas/numpy. Package-path wiring and denied cloud factories
are test setup, not production installation or provider qualification.

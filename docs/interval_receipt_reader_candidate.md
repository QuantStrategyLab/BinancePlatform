# Retained interval reader: LOCAL PREREQUISITE

## Result and hard stop

This candidate validates **supplied** retained receipts and prepares an ordered,
detached in-memory preview using the existing lifecycle record envelope. It does
not read a backend, enumerate an archive, deliver records, acknowledge them,
advance a cursor, clear receipts or register with runtime. Every preview reports
`blocked_archive_enumeration_and_durable_ack_unqualified` and
`delivery_acknowledged=False`. The default runtime remains unchanged.

The exact missing consumer seam is a qualified, explicitly bound durable sink
that can create-or-verify the same logical interval plus exact payload digest,
detect conflicts, and perform authoritative readback without treating a local
fallback or a swallowed error as confirmation. The existing pinned contracts do
not provide that seam, so no ACK adapter is fabricated here.

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
completeness, access/cost/retention bounds, explicit cutover, and the durable sink
ACK/readback/idempotency contract. Source/backend qualification and mixed-writer
runtime adoption remain separate. No receipt deletion or new queue/service is
proposed. Runtime switch, source release pin, workflows and ordinary writes are
unchanged; no Runtime dispatch or deployment is authorized by this candidate.

## Local verification

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

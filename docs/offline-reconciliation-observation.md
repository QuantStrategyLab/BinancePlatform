# Offline reconciliation observation mock

This standalone development entrypoint uses synthetic JSON only. It does not
import the broker SDK, runtime startup, accounting stores, recovery controller,
or trading workflow. It makes no network calls and requires no credentials.
There are no production dependencies, workflow changes, or deployment assets.

## Run the mock

From this repository, using Python 3.11 or later:

```sh
mock_output=$(mktemp -d)
python3 scripts/collect_reconciliation_observation.py \
  --config tests/fixtures/reconciliation_observation/config.json \
  --fixture tests/fixtures/reconciliation_observation/matched.json \
  --report-dir "$mock_output/reports" \
  --now 2000-01-01T00:02:00Z > "$mock_output/observation.json"
```

`--now` is an explicit simulated clock, not an instruction to refresh evidence.
All fixture acquisition times remain unchanged. Without this option, current UTC
is used and these old fixtures are stale. Use a new output directory per run:
reports are create-only, and a conflicting replay returns
`report_persistence_failed`, `unavailable`, `incomparable`, and a null report ID.
Exit code zero means a diagnostic envelope was emitted; inspect `data_status`
and `comparison_status` to determine its meaning. Invalid inputs exit with code 2
and a sanitized error, without exposing paths or underlying exception messages.

Other fixtures: `mismatched.json`, `missing-ledger.json`,
`missing-reference.json`, and `busy.json`. Missing inputs remain null in the
report. They are never replaced by zero balances or empty ledgers. A complete
disagreement is `ready` / `mismatched`; incomplete or changing inputs are
`incomparable` with bounded reason codes. Observation-only policy is fixed.

The report contains the synthetic inputs. The public envelope contains only
opaque fixture IDs, acquisition times, coverage, comparison and reason codes;
it omits account identity and financial values. `config.json` binds exactly one
invented account, `binance-paper-mock-001`, to the synthetic scope. No real
account binding belongs in this repository or these examples.

The comparison is deliberately limited to exact synthetic balances, ordered
orders/executions and ledger entries against a separately supplied reference.
Decimal balance strings compare numerically. Before/after broker facts and
ledger versions/entries must agree. Fixture `complete` flags simulate coverage;
they are not proof of Binance pagination, permission, or history completeness.
No baseline is enrolled, inferred from current observations, or rewritten.

## View it in QuantRuntimeSettings

Pass the emitted `observation.json` to the companion repository's
`scripts/offline_reconciliation_console.mjs`. See that repository's
`docs/offline-reconciliation-observation.md` for the loopback page and read APIs.
The separate historical section retains the provided recovery fixture exactly,
including its original stale status. No production endpoint is updated.

## Forward native-quantity report

The same offline entrypoint also accepts a forward fixture. It compares native
asset quantities for Spot, Funding, Flexible Earn and locked Earn, independently
for every wallet and asset. It does not value balances or treat USDT as USD.

```sh
forward_output=$(mktemp -d)
python3 scripts/collect_reconciliation_observation.py \
  --forward-fixture tests/fixtures/reconciliation_observation/forward-unreconciled.json \
  --report-dir "$forward_output"
```

The example emits `UNRECONCILED`: Funding and locked Earn are unknown, with null
opening and closing quantities and no combined balance. Unknown wallets are
never zero. All four wallet keys remain visible. Explicit zero rows are retained
in private inputs and the quantity projection; an absent asset is unknown even
if a different asset was observed. A newly appearing asset therefore needs
explicit opening coverage, not an inferred opening zero. Margin, futures,
options and portfolio margin remain outside this scope.

`--forward-fixture` is mutually exclusive with `--fixture`; do not supply
`--config` or `--now` with it. The CLI accepts only `environment=synthetic` and
invented account/source bindings. It writes `forward-synthetic-report.json`
create-only. Concurrent writers have one winner; replay or a conflicting
restart cannot overwrite the first receipt. Invalid input or persistence
failure exits 2 with a fixed diagnostic. Exit 0 means the informational report
was written, including an `UNRECONCILED` result. This status never requests a
halt or grants execution, readiness, recovery or go-live authority.

The Python API `application.forward_reconciliation.build_forward_report` can
also project `environment=private_observation` in memory. Its caller must supply
`expected_account_id` and `expected_binding` separately from the input bundle,
using approved private configuration. This API has no SDK, collector, store,
notification or scheduler dependency. Private raw observations, product IDs,
bindings, quantities and receipt revisions remain in the private report's
`inputs`; use the shared `forward_envelope` allowlist for public diagnostics.
Never log the private report. The offline CLI rejects private observations.

The contract is:

- `anchor_at` is the first authentic observation's actual time, no earlier than
  2026-11-03 UTC. `previous_cutoff` and `cutoff` select only
  `(previous_cutoff, cutoff]`, at most 24 hours. Opening and closing snapshots
  must bind those exact cutoffs. No pre-anchor history, old cost, recovery
  cursor, checkpoint, migration or midnight observation is required or inferred.
- Each snapshot binds the expected account and source and contains four
  `wallets`. A wallet is null when unknown; otherwise it supplies exact Decimal
  strings in `balances` or private `native_rows`, plus a coverage `receipt`.
  Native Spot rows retain `asset/free/locked`; Funding retains
  `asset/free/locked/freeze/withdrawing`; Flexible Earn retains
  `asset/productId/totalAmount`; locked Earn retains
  `asset/positionId/amount`. Duplicate assets or product/position IDs fail the
  comparison. Funding with a nonzero withdrawing amount stays unknown because
  ownership and fee semantics are unqualified. Unsupported product rows remain
  unknown. Multiple valid Earn positions sum by asset without dropping rows.
- Receipts retain binding, collection start/finish, explicit `source_as_of`,
  stable revision and ordered pages. Each page records its number, size, row
  count, stable total, binding, revision, capture interval and `content_sha256`.
  The digest covers that page's canonical JSON rows (sorted keys, compact
  separators, ASCII escaping, no NaN); normalized balance rows are sorted by
  asset with `asset` and `quantity` keys. Native inventory and event rows retain
  source order. This is private corruption detection, not authentication or
  anonymisation. Totals and content digests must match
  every received row, including zeros. Missing, full-but-incomplete, changing,
  extra or misbound pages fail the comparison. A client observation timestamp
  cannot substitute for source-effective time. APIs without a qualified stable
  revision/source cutoff remain `UNRECONCILED`; do not invent either field.
- `event_receipts` covers all nine surfaces: fills, deposits, withdrawals,
  rewards, Convert, dust, wallet transfers, Flexible lifecycle and locked
  lifecycle. Every receipt binds the exact forward window and `all_assets`
  universe, including zero-event surfaces. Counts are checked against canonical
  events. Strategy-symbol lists and diagnostic history counts cannot establish
  full-account coverage. Opening assets, newly appearing assets and fee assets
  must all have explicit wallet inventory coverage.
- Each event has a native identity, successful status and source-effective
  time in the selected window. Fills, Convert and dust supply exact from/to
  assets and quantities plus an independently specified fee asset/quantity and
  `additional_debit` fee semantics. `project_spot_fill` requires native
  `quoteQty`, `qty`, commission, millisecond time and a qualified base/quote
  mapping; it never substitutes `price * qty`. Trade IDs are namespaced by
  symbol. Other conversion or fee semantics remain unknown.
- Deposits/rewards are explicit positive native quantities in a named wallet.
  Withdrawals require separately qualified debit and fee semantics; unknown
  withdrawal fees cannot become zero. A canonical transfer supplies source and
  destination wallets and one exact positive quantity; both signed legs are
  derived once. Earn subscription/redemption similarly pairs Spot with its
  named Earn wallet. The canonical upstream identity must represent one
  economic movement, not separate duplicate wallet sightings. Duplicate native
  identities, missing second-wallet quantities, unsupported boundary wallets,
  transfer fees or lifecycle directions remain `UNRECONCILED`.

Matched synthetic inputs produce `MATCHED_SYNTHETIC`; privately bound inputs
may produce `MATCHED_OBSERVATION`. These describe only the supplied quantity
equation, `closing - opening - native movements = 0`, separately per wallet and
asset. Per-wallet residuals prevent equal and opposite errors from disappearing
in an aggregate. Missing event evidence suppresses the partial movement ledger.
Inputs are copied and all checks recomputed on every invocation; a saved match
flag has no effect. The `runtime_status` remains `NOT_PROVEN` and the policy is
always observation-only. This change does not install a real feed, prove an
authentic opening, establish collection provenance or connect to the console.

## Verify

```sh
python3 -m unittest tests.test_reconciliation_observation_mock
python3 -m pytest -q tests/test_forward_reconciliation_observation.py
```

Tests cover match/difference, missing reference/ledger, incomplete/busy input,
identity and scope mismatch, old/future evidence, create-only report failure,
redacted diagnostics, and an entrypoint run with network connections and runtime
imports blocked. Existing CI discovers these tests with its normal pytest job.

## Later configuration and live approval

`mode=mock` and `environment=synthetic` are the only accepted settings. Changing
either to `live` is rejected; configuration alone cannot enable live collection.
The source ID is also fixed to the mock namespace. No secrets, arbitrary provider
URLs, module paths, or runtime modes are configuration options in this slice.

After separate approval, replace fixture input with an isolated read-only Binance
adapter and approved reference reader, and replace local synthetic report storage
with approved private create-only storage. Preserve the observation envelope and
its observation-only policy; use a distinct environment/source and an explicitly
bound private account identity. Do not connect the mock source to live status.

That later slice must select required surfaces/symbols/intervals, implement real
permission and identity checks, complete pagination/deduplication, consistent
bounded acquisition, and applicable accounting semantics. Approve the permitted
cloud region/egress, dedicated read-only credential, exact reference/ledger
access, identities/IAM, retention, cost cap, effective freshness/cadence, and
operator procedure first. The live receiver needs scoped authentication,
target-authorized reads, an atomic register, replay/conflict checks and unknown
publication-result lookup. None exists in this offline mock. Never reuse the
prepare/live runner, recovery publisher token, or recovery-control state.

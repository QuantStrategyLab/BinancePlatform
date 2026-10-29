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

## Verify

```sh
python3 -m unittest tests.test_reconciliation_observation_mock
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

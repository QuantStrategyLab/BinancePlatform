# One-shot Binance LIVE authority `runner_revision` update

Maintenance-only entry. It does **not** enable trading, does **not** change
`RECONCILE_ONLY`, and does **not** dispatch no-submit `full_cycle`.

## Fixed targets

| Item | Scope | Value |
|---|---|---|
| Repository | — | `QuantStrategyLab/BinancePlatform` |
| Environment | — | `binance-runtime` |
| Runtime stop control | repository variable | `RUNTIME_TARGET_ENABLED` |
| Secret | environment secret | `BINANCE_RISK_AUTHORITY_JSON` |
| Digest variable | environment variable | `BINANCE_RISK_AUTHORITY_SHA256` |
| Source variable (read-only here) | environment variable | `BINANCE_RISK_AUTHORITY_SOURCE_REVISION` |

Only the JSON field `runner_revision` may change. Account, strategy, config
digest, mandate, and `BINANCE_RISK_AUTHORITY_SOURCE_REVISION` stay unchanged.

## Modes

- `plan` (default): verify live preconditions + byte digest + surgical patch;
  **zero** secret/variable writes. Do not inject the write token.
- `apply`: same checks, then submit secret bytes and the new SHA256 variable.
  Requires environment secret `BINANCE_AUTHORITY_UPDATE_TOKEN` (fine-grained
  **Environments: write** on this repository). Ordinary `GITHUB_TOKEN` cannot
  write environment secrets; do not invent `permissions: secrets: write`.

## Preconditions (fail closed)

1. `RUNTIME_TARGET_ENABLED` live value is exactly `false` (repository variable
   via `gh variable get … --repo …` with no `--env`; not copied to the
   environment, not defaulted, and not an operator-claimed input).
2. In-progress Actions runs inventory is queryable; only the exact current
   maintenance run ID is excluded, and every other in-progress run blocks.
3. Live SHA256 and source revision equal the operator-supplied expected values
   (environment variables via `gh variable get … --repo … --env binance-runtime`).
4. Raw secret bytes hash to the expected SHA256.
5. Target trading runner SHA is an explicit 40-character lowercase hex commit;
   never auto-selected from `main` tip or this workflow’s `github.sha`.

The gateway performs fresh read-only `gh` queries for every precondition read,
including the read immediately before `apply`. It does not treat `LIVE_*`
environment snapshots as live state. The workflow passes its trusted
`${{ github.run_id }}` as the only run ID eligible for self-exclusion.

`github.sha` for this workflow is recorded only as
`maintenance_workflow_sha` and is **not** the trading `runner_revision`.

## Non-atomicity and coordination

Secret and SHA256 variable updates are **two** API calls. On first failure or
uncertain outcome: stop, keep runtime disabled, do **not** auto-retry or roll
back. A later cloud consume (Runtime materialize + no-submit full_cycle) must
confirm byte/digest consistency. Workflow `concurrency` does **not** lock
external `gh`/console writers; operators must coordinate before `apply`.

## Not production-ready until

- `BINANCE_AUTHORITY_UPDATE_TOKEN` is created out-of-band (never paste into chat).
- Environment/branch protection and human approval gates are confirmed for
  `binance-runtime` / `main` as required by operators.
- Version triangle is decided: maintenance workflow revision, approved trading
  runner revision, and the revision actually checked out by the existing
  no-submit validation entry (local release-pin patches are unrelated unless
  deliberately published).

## After a successful apply

1. Independently confirm cloud consume sees the new digest.
2. Run existing `validate_only=true` + `full_cycle=true` with runtime disabled.
3. Only `cycle_complete` + real risk `APPROVE` counts; do not auto-enable
   `RUNTIME_TARGET_ENABLED`.

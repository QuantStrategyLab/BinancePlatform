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
  The workflow passes its per-run GitHub App installation token through the
  existing `BINANCE_AUTHORITY_UPDATE_TOKEN` process environment only in apply
  mode. No saved PAT is required by the script.

## GitHub App maintenance identity

Use a dedicated private organization GitHub App installed only on
`QuantStrategyLab/BinancePlatform`, with **Actions: read**, **Variables: read**,
**Environments: write**, and **Metadata: read**. The workflow scopes each
installation token to this repository and requests **Environments: read** for
plan or **Environments: write** for apply. GitHub App Environments permission
is repository-wide; the protected `binance-runtime` environment that stores
the App private key controls which workflow jobs can access it. No webhook or
OAuth configuration is needed.

Store `BINANCE_AUTHORITY_APP_CLIENT_ID` as an environment variable and
`BINANCE_AUTHORITY_APP_PRIVATE_KEY` as an environment secret in
`binance-runtime`. The action mints a token for each run, valid for at most one
hour, and attempts to revoke it in the post-job step. A revocation failure is
reported as a warning; expiry still limits its lifetime. The private key remains long-lived
until manually revoked or rotated. Retain the existing
`BINANCE_AUTHORITY_UPDATE_TOKEN` PAT until the new App identity is validated,
then retire the PAT.

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

- GitHub App client ID and private key are configured out-of-band in
  `binance-runtime`.
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

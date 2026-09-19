# One-shot Binance LIVE authority version reissue

Maintenance-only entry. It does **not** enable trading, does **not** change
`RECONCILE_ONLY` or mandate fields, and does **not** dispatch Runtime /
no-submit `full_cycle`.

## Fixed targets

| Item | Scope | Value |
|---|---|---|
| Repository | — | `QuantStrategyLab/BinancePlatform` |
| Environment | — | `binance-runtime` |
| Runtime stop control | repository variable | `RUNTIME_TARGET_ENABLED` |
| Secret | environment secret | `BINANCE_RISK_AUTHORITY_JSON` |
| Digest variable | environment variable | `BINANCE_RISK_AUTHORITY_SHA256` |
| Source variable | environment variable | `BINANCE_RISK_AUTHORITY_SOURCE_REVISION` |

Only JSON fields `strategy_revision` and `runner_revision` may change. Account,
config digest, mandate, and all other JSON bytes/semantics stay unchanged.
`BINANCE_RISK_AUTHORITY_SOURCE_REVISION` is updated only to the explicit
operator-supplied target SHA.

## Modes

- `plan` (default): verify live preconditions + byte digest + surgical patch;
  **zero** secret/variable writes. Do not inject the write token.
- `apply`: same checks, then submit secret bytes, the new SHA256 variable, and
  the new source revision variable (skipping any write that is already at
  target). Requires environment secret `BINANCE_AUTHORITY_UPDATE_TOKEN`
  (fine-grained **Environments: write**). Ordinary `GITHUB_TOKEN` cannot write
  environment secrets; do not invent `permissions: secrets: write`.

## Explicit inputs (no main-tip selection)

All of the following must be supplied as full lowercase hex values:

- `target_strategy_revision` (40)
- `target_runner_revision` (40)
- `target_source_revision` (40)
- `expected_authority_sha256` (64)
- `expected_source_revision` (40; current live value)

Approved operator values for a later run (inputs, not code defaults):

- strategy `7fcca8ee0280b3e66245eb673caadda8b8b3b9a3`
- runner `5d922afa5093c16fe2470ea7761277fe35f50d6a`
- source `5d922afa5093c16fe2470ea7761277fe35f50d6a`

## Preconditions (fail closed)

1. `RUNTIME_TARGET_ENABLED` live value is exactly `false` (repository variable
   via `gh variable get … --repo …` with no `--env`).
2. In-progress Actions runs inventory is queryable; only the exact current
   maintenance run ID is excluded, and every other in-progress run blocks.
3. Live SHA256 and source revision equal the operator-supplied expected values
   (environment variables via `gh variable get … --repo … --env binance-runtime`).
4. Raw secret bytes hash to the expected SHA256.
5. Target SHAs are explicit 40-character lowercase hex commits; never
   auto-selected from `main` tip or this workflow’s `github.sha`.

`github.sha` is recorded only as `maintenance_workflow_sha` and is **not** a
trading strategy/runner revision.

## Non-atomicity and coordination

Secret, SHA256, and source revision updates are up to **three** API calls. On
first failure or uncertain outcome: stop, keep runtime disabled, do **not**
auto-retry or roll back. A later cloud consume (Runtime materialize +
no-submit full_cycle) must confirm byte/digest consistency. Workflow
`concurrency` does **not** lock external `gh`/console writers; operators must
coordinate before `apply`.

## After a successful apply

1. Independently confirm cloud consume sees the new digest and source revision.
2. Run existing `validate_only=true` + `full_cycle=true` with runtime disabled.
3. Only `cycle_complete` + real risk `APPROVE` counts; do not auto-enable
   `RUNTIME_TARGET_ENABLED`.

# BinancePlatform

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

BinancePlatform is a QuantStrategyLab Binance crypto execution platform. It executes runtime-enabled crypto strategies through Binance-facing workflows and self-hosted orchestration.

It is an execution layer, not a strategy research repository. Strategy logic comes from `CryptoStrategies`; snapshot and validation artifacts come from `CryptoSnapshotPipelines` when a profile requires them.

## Runtime boundary

- Loads only runtime-enabled strategy profiles exposed by the strategy packages.
- Handles broker/API connectivity, dry-run checks, notifications, and deployment settings.
- Must keep credentials in GitHub Secrets, cloud secret stores, or the broker-specific secret system, never in Git.
- Should start with dry-run or paper mode before any live order path is enabled.

## Direct vs snapshot-backed profiles

Direct runtime profiles can usually run from market history or portfolio state. Snapshot-backed profiles need a current artifact bundle from the matching snapshot pipeline before this platform should execute them. The platform should not invent strategy eligibility; it should consume the status and artifacts published by the strategy and snapshot repositories.

## Deploy safely

1. Configure secrets and runtime variables outside Git.
2. Run the workflow or service in dry-run mode.
3. Review generated orders, logs, notifications, and reconciliation output.
4. Confirm rollback steps and artifact versions.
5. Enable scheduled or live execution only after the above checks are clear.

## Repository layout

- `tests/`: unit, contract, and regression tests.
- `docs/`: runbooks, design notes, evidence, and integration contracts.
- `.github/workflows/`: CI, scheduled jobs, release, or deployment workflows.
- `scripts/`: operator scripts and local helpers.
- `research/`: research configs and non-live candidate artifacts.

## Quick start

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

## Useful docs

- [`docs/binance_platform_rename_checklist.md`](docs/binance_platform_rename_checklist.md)
- [`docs/operator_runbook.md`](docs/operator_runbook.md)

## License

See [LICENSE](LICENSE).

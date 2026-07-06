# BinancePlatform


## QSL architecture role

- **Layer**: `runtime-platform`.
- **Responsibility**: Binance crypto execution runtime.
- **Owns**: broker/API connectivity, dry-run/live controls, deployment settings.
- **Consumes**: CryptoStrategies, CryptoLivePoolPipelines artifacts, QuantPlatformKit, QuantRuntimeSettings.
- **Must not**: own strategy research logic or publish live-pool membership.

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## What this repository is

BinancePlatform is a QuantStrategyLab Binance crypto execution platform. It executes runtime-enabled crypto strategies through Binance-facing workflows and self-hosted orchestration.

It is an execution layer, not a strategy research repository. Strategy logic comes from `CryptoStrategies`; live-pool and validation artifacts come from `CryptoLivePoolPipelines` when a profile requires them.

## Runtime boundary

- Loads only runtime-enabled strategy profiles exposed by the strategy packages.
- Handles broker/API connectivity, dry-run checks, notifications, and deployment settings.
- Must keep credentials in GitHub Secrets, cloud secret stores, or the broker-specific secret system, never in Git.
- Should start with dry-run or paper mode before any live order path is enabled.

## Direct vs snapshot-backed profiles

Direct runtime profiles can usually run from market history or portfolio state. Snapshot-backed profiles need a current artifact bundle from the matching live-pool pipeline before this platform should execute them. The platform should not invent strategy eligibility; it should consume the status and artifacts published by the strategy and live-pool repositories.

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

## QSL compatibility status

- Added `qsl.toml` with `tier = "runtime-platform"`, `ring = 3`, and `compat.bundle = "2026.07.0"` for runtime compatibility tracking.
- Current dependency workflow is still `requirements.txt/requirements-lock.txt` + `pip`; migration to `pyproject.toml + uv.lock` is intentionally deferred due runtime entrypoint/dependency bootstrap compatibility risk.
- TODO: keep this repository under observation and schedule a low-risk migration once runtime bootstrap can retain existing scheduler/entrypoint behavior.

## Useful docs

- [`docs/binance_platform_rename_checklist.md`](docs/binance_platform_rename_checklist.md)
- [`docs/operator_runbook.md`](docs/operator_runbook.md)

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).

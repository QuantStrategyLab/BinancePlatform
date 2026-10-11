# BinancePlatform

BinancePlatform is QuantStrategyLab's execution runtime for Binance crypto trading: it runs runtime-enabled crypto strategies through Binance-facing workflows and self-hosted orchestration, handling broker/API connectivity, dry-run/live controls, and deployment. It is strictly an execution layer, not a research repository — strategy logic comes from `CryptoStrategies`, and live-pool eligibility plus validation artifacts come from `CryptoLivePoolPipelines` when a profile needs them. Within the larger QuantStrategyLab system it sits at the runtime-platform layer, consuming upstream strategy and pipeline artifacts rather than deciding strategy logic itself. It's meant for engineers operating or auditing this platform's deployment and runtime behavior, not for traders looking for ready-made strategies or investment signals.

[Chinese README](README.zh-CN.md)

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## Architecture role

- **Layer**: `runtime-platform`.
- **Responsibility**: Binance crypto execution runtime.
- **Owns**: broker/API connectivity, dry-run/live controls, deployment settings.
- **Consumes**: CryptoStrategies, CryptoLivePoolPipelines artifacts, QuantPlatformKit, QuantRuntimeSettings.
- **Must not**: own strategy research logic or publish live-pool membership.

## Runtime boundary

- Loads only runtime-enabled strategy profiles exposed by the strategy packages.
- Handles broker/API connectivity, dry-run checks, notifications, and deployment settings.
- Must keep credentials in GitHub Secrets, cloud secret stores, or the broker-specific secret system, never in Git.
- Should start with dry-run or paper mode before any live order path is enabled.

### Read-only Binance account-facts refresh

The `Refresh Binance Account Facts` workflow can dispatch the existing `Binance Account Facts` workflow once per day at 06:17 UTC. It is restricted to this repository's `main` branch and is disabled unless the repository variable `BINANCE_ACCOUNT_FACTS_REFRESH_ENABLED` is exactly `true`; an absent variable leaves it disabled. The dispatcher uses only its short-lived GitHub token to request the fixed `runtime-production` `direct_read` workflow input. It does not receive broker credentials, enable Runtime, or dispatch the trading workflow. The target workflow retains its own `BINANCE_ACCOUNT_FACTS_ENABLED` gate and protected `binance-runtime` environment.

QSL treats account-facts reports as stale after 36 hours. A daily refresh is intended to keep read-only balances current; a failed or skipped target run does not make old data current. The default configuration remains off; production activation is controlled by the dedicated repository gate and requires explicit authorization.

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
python -m pip install --upgrade pip uv
uv sync --frozen --extra test
uv run --no-sync python -m unittest discover -s tests -v
```

## QSL compatibility status

- Added `qsl.toml` with `tier = "runtime-platform"`, `ring = 3`, and `compat.bundle = "2026.07.0"` for runtime compatibility tracking.
- Dependency workflow is now `pyproject.toml + uv.lock`.
- CI, watchdog, and self-hosted runtime bootstrap all install from `uv.lock`.

## Useful docs

- [`docs/binance_platform_rename_checklist.md`](docs/binance_platform_rename_checklist.md)
- [`docs/operator_runbook.md`](docs/operator_runbook.md)

For a forward Earn accounting failure, the disabled `main` workflow has one
read-only `accounting_migration_action=earn-forward-diagnose` mode. It samples
from the current ledger checkpoint and reports bounded, redacted diagnostics;
it never clears an owner, writes accounting state, or grants execution.

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## License

See [LICENSE](LICENSE).

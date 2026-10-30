# BinancePlatform

Execution runtime that runs crypto strategies against Binance with dry-run and live controls.

English | [简体中文](README.zh-CN.md)

## Status

Experimental. Use at your own risk. This project is not financial advice and
does not guarantee any trading result. Live trading can lose money; you are
responsible for your own configuration and orders. Dry-run is the default.

## What this is

BinancePlatform is the QuantStrategyLab execution runtime for Binance crypto
trading. It runs runtime-enabled crypto strategies through a live cycle,
handling Binance connectivity, dry-run and live gating, state and
reconciliation, notifications, and deployment. Strategy logic comes from
`CryptoStrategies`, and live-pool eligibility and validation artifacts come from
`CryptoLivePoolPipelines`. It is aimed at engineers operating or auditing the
platform.

## What this is not

It does not contain strategy research or signal generation. It does not decide
which strategies are eligible to run; it loads profiles that other repositories
publish. It does not run at all without an explicit runtime target and
credentials.

## Quick start

Requires Python 3.11 or newer.

```bash
python -m pip install --upgrade pip uv
uv sync --frozen --extra test
uv run --no-sync python -m unittest discover -s tests -v
```

The commands below all go through `uv run --no-sync` so they use the interpreter
and dependencies installed by `uv sync` without re-resolving them.

Replay one fixed-input cycle from local fixtures without touching the exchange
(pass `--help` to list options such as `--fixtures-dir` and `--output`):

```bash
uv run --no-sync python run_cycle_replay.py
```

Run the live cycle (dry-run by default) once a runtime target and credentials
are set:

```bash
uv run --no-sync python main.py
```

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BINANCE_API_KEY` | Yes | none | Binance API key. |
| `BINANCE_API_SECRET` | Yes | none | Binance API secret. |
| `STRATEGY_PROFILE` | No | `crypto_live_pool_rotation` | Runtime strategy profile to load. |
| `BINANCE_DRY_RUN` | No | `true` | Keep the cycle in dry-run mode. |
| `RUNTIME_TARGET_JSON` | No | none | Runtime target descriptor aligned with `BINANCE_DRY_RUN`. |
| `ACCOUNT_SELECTOR` | No | `default` | Which configured account to run. |
| `GCP_PROJECT_ID` | No | none | Google Cloud project for state and reports. |
| `HEALTH_PORT` | No | `8080` | Port for the health endpoint. |
| `NOTIFY_LANG` | No | `en` | Language used for notifications. |

Credentials are read from the environment only and are never stored in the
repository.

## Runtime daily diagnostics

The daily publisher reports `status=unavailable`, `reason=binding_missing` and
exit code 1 when the runtime target is valid but its account-facts binding is
missing or blank. This failure happens before archive reads or report delivery;
it does not expose the binding or establish cloud authentication. Other failures
retain the generic unavailable status, and successful or preflight output is
unchanged.

## Repository layout

```
main.py                 live cycle entrypoint
application/            cycle, execution, and reconciliation services
entrypoints/            CLI entrypoint helpers
strategy/               strategy loading and registry glue
notifications/          notification adapters
infra/                  infrastructure helpers
scripts/                operator scripts
docs/                   runbooks and contracts
tests/                  unit and contract tests
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI assistants and maintainers also follow [AGENTS.md](AGENTS.md).

## Security

See [SECURITY.md](SECURITY.md). Do not open a public issue for a vulnerability.
Never commit credentials or account material.

## License

MIT. See [LICENSE](LICENSE).

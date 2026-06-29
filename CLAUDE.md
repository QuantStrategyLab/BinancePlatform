# BinancePlatform

Crypto execution platform for QuantStrategyLab, running on self-hosted Oracle VPS orchestration with GitHub Actions scheduling.

## Key Differences from Cloud Run Platforms

- **Scheduling**: GitHub Actions `schedule` cron (NOT Cloud Scheduler)
- **Runtime**: Self-hosted runner on Oracle VPS
- **Market**: 24/7 crypto — no "market close" concept
- **Strategies**: CryptoStrategies pip package
- **Snapshots**: CryptoLivePoolPipelines

## Workflows

| Workflow | Schedule | Purpose |
|---|---|---|
| `main.yml` | On-demand / scheduled | Strategy execution |
| `runtime-heartbeat.yml` | `35 * * * *` | Runtime health monitoring |
| `runner-scheduler-diagnostics.yml` | Manual | Scheduler config inspection |
| `ci.yml` | Push/PR | CI tests |

## Monitoring

- Heartbeat checks if main workflow ran within lookback window
- Telegram alerts on heartbeat failure
- Configurable via `vars.RUNTIME_HEARTBEAT_*` and `secrets.TG_TOKEN`

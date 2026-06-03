# BinancePlatform Rename Checklist


## 中文摘要

- 用途：本文档围绕 `BinancePlatform Rename Checklist`，用于理解 `BinancePlatform` 的配置、运行、部署、研究或验收边界。
- 主要覆盖：`Current status`、`Why this rename is different from Cloud Run repos`、`Confirmed impact points`、`External runtime dependencies`、`Repo-internal references`。
- 阅读顺序：先确认边界、输入输出和权限要求，再执行文档里的命令、CI、dry-run、发布或切换步骤。
- 风险提示：涉及实盘、密钥、权限、Cloud Run、交易所或券商 API 的变更，必须先在测试环境或 dry-run 验证；不要只凭示例直接修改生产。
- 英文正文保留更完整的命令、字段名和配置键；如果摘要和正文不一致，以正文中的实际命令和配置为准。
_Last reviewed: 2026-03-30_

This checklist records the completed runtime repository rename from `BinanceQuant` to `BinancePlatform`.

## Current status

- Current GitHub repo: `QuantStrategyLab/BinancePlatform`
- Target GitHub repo: `QuantStrategyLab/BinancePlatform`
- Current runtime model: Oracle Cloud / VPS hosted self-hosted runner
- Current runtime trigger: external scheduler calling GitHub `workflow_dispatch`
- Current strategy domain: `crypto`
- Current live profile: `crypto_live_pool_rotation`

## Why this rename is different from Cloud Run repos

This repo is not deployed by Cloud Run or Cloud Build triggers.
The production runtime depends on:

1. a self-hosted GitHub Actions runner
2. an external scheduler that calls the GitHub Actions dispatch API
3. GCP only as backend state / credentials support

So the main rename risk is not GCP. The main risk is breaking the external dispatch path.

## Confirmed impact points

### External runtime dependencies

These were the external dependencies that had to be checked before renaming:

1. Oracle/VPS cron or scheduler job that calls:
   - `POST /repos/QuantStrategyLab/BinanceQuant/actions/workflows/main.yml/dispatches`
2. Any shell script, systemd unit, or deployment script that hardcodes:
   - `QuantStrategyLab/BinanceQuant`
   - `https://github.com/QuantStrategyLab/BinanceQuant`
3. Any local checkout path assumptions like:
   - `/path/to/BinanceQuant`
4. Any runner registration / documentation that explicitly names `BinanceQuant`

### Repo-internal references

These can be updated in the repo during the rename change:

1. README / README.zh-CN titles and examples
2. operator runbook wording
3. module docstrings that still say `BinanceQuant`
4. helper examples using `cd /path/to/BinanceQuant`
5. fallback metadata strings such as:
   - `trend_pool_support.py` -> `source_project`

## Recommended execution order

1. Confirm the external scheduler script location on Oracle/VPS.
2. Update the external dispatch target from:
   - `QuantStrategyLab/BinanceQuant`
   to:
   - `QuantStrategyLab/BinancePlatform`
3. Rename the GitHub repository.
4. Update the local workspace folder name.
5. Update repo-internal docs / docstrings / examples.
6. Trigger one manual runtime run.
7. Confirm:
   - self-hosted runner still picks up the job
   - Firestore state still updates
   - runtime log push to `logs` branch still works

## Minimum validation after rename

1. `gh repo view QuantStrategyLab/BinancePlatform`
2. manual `workflow_dispatch` for `main.yml`
3. self-hosted runner starts the job
4. runtime finishes successfully
5. Firestore updates the expected strategy documents
6. `logs` branch receives the hourly execution report

## Things that do not need to be changed in this phase

- GCP project id `binancequant`
- Firestore database name / location
- strategy domain `crypto`
- current profile `crypto_live_pool_rotation`

## Blocking condition

This rename is now completed. The external dispatch caller and VPS checkout were updated to `QuantStrategyLab/BinancePlatform`, and manual `workflow_dispatch` was re-verified after the rename.

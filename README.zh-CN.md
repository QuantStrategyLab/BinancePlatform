# BinancePlatform

[English README](README.md)

> ⚠️ 投资有风险，不构成投资建议，仅供学习交流用途。

## 这个项目做什么

BinancePlatform 是 QuantStrategyLab 体系中的**执行平台**。面向 Binance 执行 QuantStrategyLab 加密货币策略，负责外部策略加载、运行时适配、交易执行编排和 CI 运维。

## 适合谁使用

- 希望阅读、复现或扩展 QuantStrategyLab 相关模块的工程师和研究人员。
- 在阅读详细 runbook 或 workflow 前，需要先理解项目入口的运维人员。
- 在启用自动化前，需要确认项目职责、安全边界和证据要求的 reviewer。

## 当前状态

偏生产运行的执行平台代码；启用真实下单前必须先跑 dry-run 或 paper 流程。

## 仓库结构

- `application/`, `entrypoints/`, `infra/`, `reporting/`：Python 包代码。
- `tests/`：单元测试和契约测试。
- `docs/`：详细设计说明、运行手册和证据文档。
- `.github/workflows/`：CI、定时任务和部署 workflow。
- `scripts/`：运维脚本和本地辅助工具。

## 快速开始

从全新 clone 开始：

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

如果命令需要凭据，请先阅读相关 workflow 或 runbook，并把密钥配置在 Git 之外。

## 部署和运行

在 GitHub Actions 中配置 Binance 凭据、运行参数、通知和策略来源。先手工触发 dry-run，检查日志和产物，再在确认凭据与风控后启用定时 workflow。

建议先手工运行或 dry-run。只有在日志、产物、权限和回滚步骤都检查过之后，才启用定时任务或 live 执行。

## 策略表现与证据边界

本仓库不负责策略排名。收益、回撤和 live 资格必须来自策略仓库及其快照/回测产物，平台只能执行已经通过门禁的策略。

README 不应该承诺固定收益或过期指标。实际使用前，请重新运行对应测试、回测或流水线任务。

## 安全注意事项

- 不要把 API key、券商凭据、OAuth token、Cookie 或账户标识提交到 Git。
- 新策略或平台变更在 live 前必须先跑 dry-run 或 paper 流程。
- 启用定时任务前，需要人工检查生成的订单、产物和日志。

## 参与贡献

请保持改动小、可复现，并用最小必要测试覆盖。涉及策略的改动，需要附上验证行为的证据产物或命令。

## 许可证

如仓库包含 [LICENSE](LICENSE)，请以该文件为准。

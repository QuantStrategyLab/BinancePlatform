# BinancePlatform

在 Binance 上运行加密策略、并带 dry-run 与 live 控制的执行运行时。

[English](README.md) | 简体中文

## 状态

实验性项目，使用风险自负。本项目不构成投资建议，也不保证任何交易结果。实盘交易可能亏损；你需要对自己的配置和订单负责。默认运行为 dry-run。

## 简介

BinancePlatform 是 QuantStrategyLab 面向 Binance 加密交易的执行运行时。它通过 live cycle 运行已启用的加密策略，负责 Binance 连接、dry-run 与 live 门控、状态与对账、通知以及部署。策略逻辑来自 `CryptoStrategies`，live-pool 资格与验证产物来自 `CryptoLivePoolPipelines`。它面向运维或审查该平台的工程师。

## 不适用范围

它不包含策略研究或信号生成。它不决定哪些策略可以运行，只加载其他仓库发布的 profile。没有明确的 runtime target 和凭据时它不会运行。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python -m pip install --upgrade pip uv
uv sync --frozen --extra test
uv run --no-sync python -m unittest discover -s tests -v
```

下面的命令都通过 `uv run --no-sync` 运行，因此会使用 `uv sync` 安装的解释器和依赖，不会重新解析它们。

在不接触交易所的情况下，用本地 fixtures 回放一个固定输入的周期（传入 `--help` 可查看 `--fixtures-dir`、`--output` 等选项）：

```bash
uv run --no-sync python run_cycle_replay.py
```

设置好 runtime target 和凭据后运行 live cycle（默认 dry-run）：

```bash
uv run --no-sync python main.py
```

## 配置

| 变量 | 必填 | 默认值 | 用途 |
|---|---|---|---|
| `BINANCE_API_KEY` | 是 | 无 | Binance API key。 |
| `BINANCE_API_SECRET` | 是 | 无 | Binance API secret。 |
| `STRATEGY_PROFILE` | 否 | `crypto_live_pool_rotation` | 要加载的运行时策略 profile。 |
| `BINANCE_DRY_RUN` | 否 | `true` | 保持周期运行在 dry-run 模式。 |
| `RUNTIME_TARGET_JSON` | 否 | 无 | 与 `BINANCE_DRY_RUN` 一致的 runtime target 描述。 |
| `ACCOUNT_SELECTOR` | 否 | `default` | 要运行的已配置账户。 |
| `GCP_PROJECT_ID` | 否 | 无 | 存放状态与报告的 Google Cloud 项目。 |
| `HEALTH_PORT` | 否 | `8080` | 健康检查端点端口。 |
| `NOTIFY_LANG` | 否 | `en` | 通知使用的语言。 |

凭据只从环境变量读取，绝不写入仓库。

## 仓库结构

```
main.py                 live cycle 入口
application/            周期、执行与对账服务
entrypoints/            CLI 入口辅助
strategy/               策略加载与注册表胶水代码
notifications/          通知适配器
infra/                  基础设施辅助
scripts/                运维脚本
docs/                   运行手册与契约
tests/                  单元与契约测试
```

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 安全

见 [SECURITY.md](SECURITY.md)。安全漏洞不要开公开 issue，也不要把凭据或账户信息提交到仓库。

## 许可证

MIT，见 [LICENSE](LICENSE)。

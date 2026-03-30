# BinancePlatform

语言: [English](README.md) | 简体中文

这是一个运行在 Binance 现货上的自动化量化项目，核心由两部分组成：

- BTC 核心仓定投与分档止盈
- 山寨币趋势轮动层

项目使用估值指标（`AHR999`、`Z-Score`）和趋势闸门（`MA200`、均线斜率），并兼容 Binance Flexible Earn、USDT 现货缓冲、BNB 手续费燃料仓、Telegram 通知和 Firestore 状态存储。

**趋势池来源：** 优先消费上游 `CryptoLeaderRotation` 发布的月度 live pool。本仓库会先校验上游 payload 的新鲜度和契约字段，再决定是否采用；同时会把成功接受过的上游 payload 作为 last known good 保存在状态中；只有前面几层都不可用时才会退化到本地静态 fallback。

当前 `crypto_leader_rotation` 的纯策略模块来自 `CryptoStrategies`。

完整策略说明现在放在 [`CryptoStrategies`](https://github.com/QuantStrategyLab/CryptoStrategies#crypto_leader_rotation)。下面这些章节主要保留下游执行侧的约束、运行时行为和运维说明。

**工作区假设：** 本地 replay、monitor 和 review 工具默认假设上游仓库位于 `../CryptoLeaderRotation`。如果目录不同，可以通过参数或环境变量覆盖。

**Python 版本：** 推荐 `Python 3.11`。CI 固定在 `3.11`，本地辅助命令会优先使用 `python3.11`，没有时回退到 `python3`。

## 仓库形态

- `main.py` 是 live 交易编排入口。
- `strategy_core.py` 放共享的纯策略逻辑。
- `research/` 放研究、审计和历史分析工具。
- 各种 `run_*` 脚本用于本地固定输入回放和维护辅助。
- `tests/fixtures/` 放固定输入，用于 dry-run replay 回归测试。

## 文件说明

- **main.py** — live 主脚本，按小时运行。
- **strategy_core.py** — live 和 research 共用的纯策略计算。
- **runtime_support.py** — live / dry-run 共用的运行时和执行报告辅助。
- **live_services.py** — Firestore 状态和 Telegram 通知适配层。
- **QuantPlatformKit.binance** — 复用 Binance 客户端初始化、余额辅助、行情快照和下单数量格式化。
- **trend_pool_support.py** — 上游趋势池 payload 解析、校验和 fallback 辅助。
- **trade_state_support.py** — 持仓状态标准化、退役仓位跟踪、重复动作保护。
- **research/backtest.py** — 研究和审计用回测，不参与 live 小时执行。
- **run_cycle_replay.py** — 用固定 fixtures 执行单轮 dry-run。
- **scripts/run_monthly_report_bundle.py** — 月度聚合脚本：将每小时执行 JSON 汇总为审阅包和 Markdown。
- **requirements.txt** — 人工维护的顶层依赖。
- **requirements-lock.txt** — CI / deploy 优先使用的锁定依赖版本。

`reports/` 下的本地生成物默认不提交。`tests/fixtures/` 会提交，因为它们属于可复现回归测试的一部分。

## 执行日志与月度审阅

每次小时运行后，完整的执行报告 JSON 会推送到 orphan `logs` 分支，路径为 `hourly/{YYYY-MM}/{YYYY-MM-DDTHHMM}.json`。

每月 1 日（UTC 00:00），**月度执行报告**工作流会聚合上月所有小时日志，创建结构化审阅包，并开启一个带 `monthly-review` 标签的 GitHub Issue。

**AI 月度审阅**工作流在该 Issue 标签触发时，发布双语（English + 中文）分析，涵盖交易执行质量、熔断器事件、降级模式事件、收益摘要、上游池变更影响、错误模式和理财缓冲效率。

### 工作流

| 工作流 | 文件 | 触发方式 | 运行器 |
|--------|------|---------|--------|
| 运行时 | `main.yml` | `workflow_dispatch` | self-hosted |
| CI | `ci.yml` | 推送到 main | ubuntu-latest |
| 月度报告 | `monthly_report.yml` | 每月 1 日 + 手动 | ubuntu-latest |
| AI 审阅 | `ai_review.yml` | issue 标签 `monthly-review` | ubuntu-latest |

### 所需 Secrets

| Secret | 使用者 |
|--------|--------|
| `BINANCE_API_KEY` | 运行时 |
| `BINANCE_API_SECRET` | 运行时 |
| `TG_TOKEN` | 运行时 |
| `GCP_SA_KEY` | 运行时 |
| `ANTHROPIC_API_KEY` | AI 审阅 |

## 策略概览

- **BTC 核心层：** 依据估值做定投（`AHR999`）和分档止盈（`Z-Score` 对动态阈值），目标仓位随总权益增长而提高。
- **趋势层：** 月度刷新候选池（优先上游 live pool，其次内部稳定质量排序 fallback），从中选出相对 BTC 最强的 Top 2，并按逆波动率分配权重。只有在 BTC 闸门开启时才允许趋势层工作。

系统按小时运行，但信号逻辑本身仍然是日频趋势和风险管理，不是高频交易。

## BTC 核心层

**指标：** `MA200`、`MA200 slope`、`AHR999`、`Z-Score`、动态 `Z-Score` 止盈阈值。

**逻辑：**

- `AHR999` 越低，定投力度越强
- 估值中性时维持正常节奏
- `Z-Score` 高于阈值时分档止盈
- `Z-Score` 越高，卖出比例越大

**目标仓位：**

`btc_target_ratio = 0.14 + 0.16 * ln(1 + total_equity / 10000)`，并有上限控制。组合越大，BTC 核心仓占比越高，趋势层占比越低。

**定投基准金额：** 每日 base order 随总权益增长。

## 趋势轮动层

**候选池来源：** 优先使用上游 live pool。读取顺序为：

1. 新鲜的上游 Firestore payload
2. Firestore 状态中记录的 last known good 上游 payload
3. 通过校验的本地 upstream 文件 fallback
4. 静态 `TREND_UNIVERSE` 紧急 fallback

**官方输入池：** 上游发布 5 币 live pool，本仓库把这 5 个币视为月度官方输入集。

**观察面板：** 本仓库也可能展示本地 stable-quality 排名，仅用于观察和诊断，不等于上游官方月池，也不是最终执行目标。

**最终执行目标：** live 趋势层只对最终 Top 2 轮动结果执行，或者在没有合格候选时进入防守状态。

**因子：**

- `SMA20 / SMA60 / SMA200`
- `20 / 60 / 120` 日收益
- `20` 日波动率
- `ATR14`
- `30 / 90 / 180` 日均成交额
- 趋势持续性
- 相对 BTC 强弱
- 风险调整后的动量

**持仓规则：** 从月池里选相对 BTC 最强的 Top 2，按逆波动率分配。

**开仓条件：**

- BTC 闸门开启
- 价格在 `SMA20 / SMA60 / SMA200` 之上
- 相对 BTC 分数为正
- 绝对动量为正

**出场条件：**

- 跌破 `SMA60`
- 触发 `ATR` 跟踪止损
- 被轮动出 Top 2

## 风险控制

- **BTC 闸门：** 只有 `BTC price > MA200` 且 `MA200 slope > 0` 时，趋势层才允许工作。
- **趋势熔断：** 如果趋势层当日收益低于阈值，则清空趋势层，BTC 核心仓保持不变。
- **外部 USDT 划转：** 手动充值或提现 USDT 时，会重置当日收益基准，不会把这类资金流当成策略亏损或熔断信号。
- **BNB 燃料仓：** 自动补仓用于手续费，不参与趋势轮动。

## Earn 兼容

- 下单前先检查现货余额，不够时从 Flexible Earn 赎回
- 维护 USDT 现货缓冲水位，多余则自动申购，短缺则自动赎回
- Flexible Earn 的自动申购和赎回只是在现货与理财之间搬动 USDT；因为运行时统计的是总 USDT，所以这类动作不会触发熔断，也不会触发外部资金流重置。

## 状态存储（Firestore）

会保存：

- 趋势持仓和最高价
- 熔断状态
- DCA 最近买卖日期
- 月池标识和池内币种
- 已从月池移除但仍持有的退役仓位

## 上游趋势池契约

**默认来源：** `CryptoLeaderRotation` 的月度输出。

读取顺序：

1. Firestore `strategy` / `CRYPTO_LEADER_ROTATION_LIVE_POOL`
2. 状态里保存的 last known good 上游 payload
3. 本地 `live_pool_legacy.json` 或 `live_pool.json`
4. 静态 `TREND_UNIVERSE`

**稳定字段：**

- `as_of_date`
- `version`
- `mode`
- `pool_size`
- `symbols`
- `symbol_map`
- `source_project`

**降级模式规则：**

- 上游 payload 必须有非空币种列表、可解析的 `as_of_date`、可接受的 `mode`
- 新鲜度由 `TREND_POOL_MAX_AGE_DAYS` 和 `as_of_date` 控制
- 如果 fresh upstream 过期或格式错误，不会把弱 fallback 当成等价替代
- 进入 degraded mode 后，默认暂停新的趋势买入，除非显式设置 `TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED=1`
- 已退役币种会保留在状态中直到真正卖出

## 环境变量

必需：

| 变量 | 说明 |
|---|---|
| `BINANCE_API_KEY` | Binance API Key |
| `BINANCE_API_SECRET` | Binance API Secret |
| `TG_TOKEN` | Telegram Bot Token |
| `GLOBAL_TELEGRAM_CHAT_ID` | 这个服务使用的 Telegram Chat ID。 |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP 服务账号 JSON 路径 |

如果你在多个 quant 仓库之间保留一层共享配置，通常只建议共享 `GLOBAL_TELEGRAM_CHAT_ID` 和 `NOTIFY_LANG`。`TG_TOKEN`、Binance API Key、GCP 凭据这些仍然应该由这个仓库自己管理。

可选：

| 变量 | 说明 |
|---|---|
| `BTC_STATUS_REPORT_INTERVAL_HOURS` | BTC 状态报告间隔，默认 `24` |
| `TREND_POOL_FILE` | 本地 `live_pool_legacy.json` 路径 |
| `TREND_POOL_FIRESTORE_COLLECTION` | 趋势池 Firestore collection，默认 `strategy` |
| `TREND_POOL_FIRESTORE_DOCUMENT` | 趋势池 Firestore document，默认 `CRYPTO_LEADER_ROTATION_LIVE_POOL` |
| `TREND_POOL_MAX_AGE_DAYS` | 上游 `as_of_date` 允许的最大天数，默认 `45` |
| `TREND_POOL_ACCEPTABLE_MODES` | 可接受的上游 mode，默认 `core_major` |
| `TREND_POOL_EXPECTED_SIZE` | 上游 live pool 期望数量，默认 `5` |
| `TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED` | degraded mode 下是否允许趋势新开仓，默认 `false` |
| `NOTIFY_LANG` | 日志和通知语言: `en`（英文，默认）或 `zh`（中文） |

## 通知格式

运行日志和 Telegram 通知共用同一个语言开关。设置 `NOTIFY_LANG=zh` 切换为中文，不设置则默认英文。

**策略心跳:**
```
💓 【策略心跳】
🕐 UTC 时间: 2026-03-24 00:00
━━━━━━━━━━━━━━━━━━
💰 总净值: $12,500.00
📈 趋势层持仓: $3,200.00 (1.25%)
₿ BTC 价格: $87,000.00
━━━━━━━━━━━━━━━━━━
AHR999: 0.850
Z-Score: 1.20 / 阈值 3.00
🎯 BTC 目标配比: 28.5%
🚦 BTC 闸门: 开启
━━━━━━━━━━━━━━━━━━
💡 建议: BTC 估值中性，跟随系统节奏即可。
```

**交易通知:**
```
✅ 【趋势买入】 ETHUSDT
价格: $3,450.00
预算: $800.00
轮动权重: 60%
相对BTC得分: 0.85

📉 【趋势卖出】 SOLUSDT
原因: ATR trailing stop ($142.50)
价格: $138.20

🛡️ 【BTC 定投买入】 BTC
AHR999: 0.45
目标配比: 28.5%
数量: 0.00125 BTC
```

## 部署

本仓库默认运行在 **self-hosted GitHub Actions runner** 上，例如 VPS。workflow 会拉代码、安装依赖、从 secret 写入 `gcp-key.json`，然后运行 `main.py`。不是“手动下载后本地 cron”那种流程。

这个仓库通过 `QuantPlatformKit` 复用 Binance 客户端初始化、余额辅助、行情快照和下单数量格式化。runner 直接执行这个仓库，`QuantPlatformKit` 不单独部署。

### 1. Self-hosted runner

- 在仓库 `Settings -> Actions -> Runners` 中添加 runner
- 在机器上安装并注册 runner，保持其长期运行

### 2. Workflow 和调度

- [`.github/workflows/main.yml`](./.github/workflows/main.yml) 负责 checkout、写入 GCP key、准备 venv、执行 `main.py`
- `push` 只触发 job；真正执行策略的步骤默认只在 `workflow_dispatch` 或 `schedule` 下运行
- 如需定时运行，可在 workflow 中加入 `schedule`
- VPS 侧推荐的运行单元名：`binance-quant`
- Cloud Run / VPS 的统一部署规则和命名建议见 [`QuantPlatformKit/docs/deployment_model.md`](../QuantPlatformKit/docs/deployment_model.md)

### 3. Repository secrets

需要设置：

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `TG_TOKEN`
- `GLOBAL_TELEGRAM_CHAT_ID`
- `GCP_SA_KEY`
- `ANTHROPIC_API_KEY`

现在 runtime workflow 要求这两个仓库/组织 Variables 已经提供：

- `GLOBAL_TELEGRAM_CHAT_ID`
- `NOTIFY_LANG`
- `STRATEGY_PROFILE`（建议设为 `crypto_leader_rotation`）

也就是说，如果你在多个 quant 仓库之间保留一层很小的共享配置，这个仓库直接使用组织级 `GLOBAL_TELEGRAM_CHAT_ID` 和 `NOTIFY_LANG`。`TG_TOKEN`、Binance API key、`GCP_SA_KEY` 仍然应该留在这个仓库自己的 secrets 里。

`STRATEGY_PROFILE` 当前只支持 `crypto_leader_rotation`；对应的策略域是 `crypto`。

### 4. GCP / Firestore

- `GCP_SA_KEY` 对应的服务账号必须具备 Firestore 读写权限
- 如果出现 invalid grant 或 account not found，通常说明 key 已失效、服务账号被删掉、或者项目不匹配

## 本地运行

仅供本地测试：

```bash
cd /path/to/BinancePlatform
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export BINANCE_API_KEY=... BINANCE_API_SECRET=... TG_TOKEN=... GLOBAL_TELEGRAM_CHAT_ID=...
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/gcp-sa.json
python main.py
```

## 本地研究回测

```bash
python3 -m research.backtest
```

## 单轮 Dry-run Replay

```bash
python3 run_cycle_replay.py --run-id local-check
```

它会输出结构化 JSON，包含：

- 采用的上游池和最终候选
- 趋势买卖意图
- BTC DCA 意图
- Earn 申购/赎回意图
- 被执行与被抑制的 side effect 计数

## 测试

```bash
python3 -m unittest \
  tests.test_strategy_core \
  tests.test_cycle_replay_runtime \
  tests.test_trend_pool_loading \
  -v
```

仓库卫生建议：

- 忽略 `reports/`、`venv/`、`.venv/`、`gcp-key.json`、`.venv_requirements_hash` 等本地产物
- 保留 `tests/fixtures/`，确保 replay 回归测试可复现

上游 `CryptoLeaderRotation` 负责月度发布、shadow candidate 评估和月度研究报告；本仓库只保留交易执行与必要的固定输入回放工具。

## Telegram

Telegram 通知会覆盖：

- 趋势买入 / 卖出
- BTC DCA
- Earn 申赎
- 熔断
- 异常错误

另外还支持可选的 BTC 周期性状态报告，默认每天 UTC 00:00 发送一次，可通过 `BTC_STATUS_REPORT_INTERVAL_HOURS` 调整。

所有通知支持 `NOTIFY_LANG` 切换语言（`en` 默认英文，`zh` 中文）。参见 [通知格式](#通知格式) 示例。

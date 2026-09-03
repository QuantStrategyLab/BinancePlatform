# Unknown-outcome query-before-submit 契约

## 目的与范围

此契约防止订单提交调用的崩溃恢复或异常处理造成重复 submit。仅约束订单调用前后的 durable `order_submission` 状态；不改变 receipt reducer、`quoteOrderQty`、资金/持仓、重试/退避、策略、provider、replay、broker、部署、Scheduler、QPK 或依赖。

## 状态表

| durable 状态 | 允许动作 | 进入条件 | 退出条件 |
| --- | --- | --- | --- |
| `RESERVED` | 唯一允许 submit 的状态 | 默认状态，或已验证 terminal 后为下一逻辑订单重置 | 在任何 client 调用前，`SUBMISSION_UNKNOWN` durable 写入成功 |
| `SUBMISSION_UNKNOWN` | 仅 `get_order` query/reconcile；绝不 submit | 已成功 durable 写入 identity digest 与 symbol | 仅收到并持久化 verified broker terminal response 后进入 `TERMINAL` |
| `TERMINAL` | 不沿用上一笔订单 submit | verified broker `FILLED`、`CANCELED`、`EXPIRED` 或 `REJECTED` 已 durable 持久化 | 为下一逻辑订单 durable 重置为 `RESERVED` |

`SUBMISSION_UNKNOWN` 只保存 `state`、`identity_sha256` 和 `symbol`；不得保存原始订单 ID、数量、价格或策略日期。broker query 使用由 digest 派生的 client order ID，不把原始 ID 写入状态记录。

## 异常分类与 fail-closed 规则

| 分类 | 条件 | 行为 |
| --- | --- | --- |
| pre-send 本地失败 | 在 `SUBMISSION_UNKNOWN` durable 写入**之前**已证明请求未发出，例如 symbol/identity 校验失败或 UNKNOWN 写入失败 | 不调用 client；可保持 `RESERVED` |
| post-UNKNOWN client 异常 | `SUBMISSION_UNKNOWN` 已 durable 写入后，任意 client 调用异常，包括 HTTP、transport、provider 特定或未分类异常 | 保留 `SUBMISSION_UNKNOWN`；只 query/reconcile；不得写 `TERMINAL`、不得重试 submit |
| query 未找到、query 异常或非终态 | query 无结果、不可解析、失败，或返回 `NEW`/`PARTIALLY_FILLED` 等非 terminal | fail-closed；保留 `SUBMISSION_UNKNOWN`，本次失败，后续仍只能 query/reconcile |
| verified terminal | broker query 或直接响应为 `FILLED`、`CANCELED`、`EXPIRED`、`REJECTED` | 仅此时允许 durable 写入 `TERMINAL`；terminal 写入失败也保留 `SUBMISSION_UNKNOWN` |

任何崩溃恢复都先读取 durable 状态。若为 `SUBMISSION_UNKNOWN`，恢复路径不依赖当前请求 payload，且只能 query/reconcile；没有 verified broker terminal response 时不得 submit 或写 `TERMINAL`。

## 兼容性与残余风险

缺少 `order_submission` 的旧状态按 `RESERVED` 处理。此契约依赖状态存储的单写入成功语义；状态写入不可用时在调用 client 前 fail-closed。它不处理并发 writer 或 broker 最终一致性延迟，后两者仍会保留 `SUBMISSION_UNKNOWN` 并阻止重发，直到获得可验证 terminal 响应。

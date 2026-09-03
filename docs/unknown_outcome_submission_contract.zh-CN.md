# Unknown-outcome query-before-submit 契约

## 目的与边界

本契约只保护一个不变量：订单提交结果未知时，进程崩溃或重启不得造成重复
submit。它不改变 receipt reducer、订单金额字段、资金/持仓、重试/退避、策略行为、
provider、部署或调度配置。

## Durable 状态表

| 当前状态 | 允许动作 | 转移 | 失败行为 |
| --- | --- | --- | --- |
| `RESERVED` | 唯一允许发起 submit 的状态 | 调用 client 前必须先 durable 写入 `SUBMISSION_UNKNOWN` | UNKNOWN 写入失败时不得调用 client；保持原状态并 fail-closed |
| `SUBMISSION_UNKNOWN` | 只允许按已冻结 identity 执行只读 `get_order` query/reconcile | broker 返回 `FILLED`、`CANCELED`、`EXPIRED` 或 `REJECTED` 后 durable 写入 `TERMINAL` | 未找到、query 失败、非 terminal 状态或 terminal 写入失败时 fail-closed，并保留 UNKNOWN；绝不 submit |
| `TERMINAL` | 不可直接 submit | 新逻辑订单必须先 durable 转为 `RESERVED`，再按上述流程写 UNKNOWN | 任一写入失败均不得 submit |

## 写序与恢复规则

1. 首次提交先读取 durable guard；只有 `RESERVED` 可继续。
2. 将 logical identity 哈希化，并把 `state=SUBMISSION_UNKNOWN`、
   `identity_sha256`、`symbol` durable 写入现有 trade-state document。
3. 只有写入明确返回成功后，才调用 Binance client submit 方法。
4. 进程看到 UNKNOWN 时忽略新的 submit payload，只用
   `QSL_<identity_sha256 前 28 位>` 和已保存的 `symbol` 查询原提交。
5. query 未找到或无法证明 terminal 时保持 UNKNOWN。terminal 写入失败也保持 UNKNOWN，
   因而下次重启仍走 query，而不是重发。

## 数据最小化

`order_submission` 记录只允许以下形状：

- `RESERVED` / `TERMINAL`：仅 `state`；
- `SUBMISSION_UNKNOWN`：`state`、`identity_sha256`、`symbol`。

记录不得包含原始 exchange/client order ID、数量、价格、策略日期或 provider 响应。
不符合该形状的记录视为无效状态，必须在调用 client 前 fail-closed。

## 兼容性

- 既有缺少 `order_submission` 的状态按 `RESERVED` 读取；下一次订单尝试会写入新 guard。
- broker-facing `newClientOrderId` 使用可由 durable digest 重建的 `QSL_` 前缀 identity；
  strategy intent、symbol、side、数量和价格计算均不变。
- 本契约假定既有 runtime single-writer 约束；不新增并发调度、CAS、schema registry 或依赖。

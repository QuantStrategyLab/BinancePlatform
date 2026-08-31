# Binance 冻结实盘基线的只读对账

当 Binance target 处于 `RECONCILE_ONLY` 时，可在自托管运行器手动启动 Runtime
workflow，并选择 `reconcile_only=true`。它运行独立的
`scripts/reconcile_frozen_live_baseline.py`，不会进入 `main.py`、不会触发策略、下单、
撤单、资金划转、理财申赎或 Firestore 写入。

运行器只读读取签名账户响应、余额、全部挂单，以及由
`BINANCE_RECONCILIATION_SYMBOLS` 精确限制的近七日成交；该变量必须覆盖 BTC、当前策略的
全部趋势候选和 BNB 手续费资产，不能用“查询全历史”替代。它同时只读本地执行状态。账户
UID、余额、订单和成交永不写入日志、报告或 artifact。artifact 只保留
`binance_reconciliation_candidate.v1` 的摘要和稳定阻断码，保存 30 天。

若交易所未返回账户 UID、受管交易对未配置、任一读取失败、私有预期摘要不存在或任一摘要
不一致，运行成功完成为“候选被阻断”，而不是重试、降级或恢复交易。

该候选仍须满足共享恢复流程：两份时间分离的收据、独立复核、双审、账户持有人确认、确认后
重新采样，以及受限控制面精确 CAS `RECONCILE_ONLY -> ACTIVE_LKG`。本仓当前只提供只读
证据采集，不提供 CAS、订单权限或自动恢复。

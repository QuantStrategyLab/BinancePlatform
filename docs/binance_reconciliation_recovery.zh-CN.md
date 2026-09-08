# Binance 冻结实盘基线的只读对账

当 Binance target 处于 `RECONCILE_ONLY` 时，可在自托管运行器手动启动 Runtime
workflow，并选择 `reconcile_only=true`。默认使用 `--no-persist`，仅输出脱敏候选，
不生成 artifact、运行报告或通知；只有在人工明确选择
`reconcile_persist_candidate=true` 后才会保存短期 artifact。它运行独立的
`scripts/reconcile_frozen_live_baseline.py`，不会进入 `main.py`、不会触发策略、下单、
撤单、资金划转、理财申赎或 Firestore 写入。

运行器只读读取签名账户响应、余额、全部挂单，以及由
`BINANCE_RECONCILIATION_SYMBOLS` 精确限制的近七日成交；该变量必须覆盖 BTC、当前策略的
全部趋势候选和 BNB 手续费资产，不能用“查询全历史”替代。Binance Spot 的 `myTrades` 每次
时间范围最多 24 小时，因此系统按不重叠的日窗口查询并去重；任一窗口达到 API 的 1,000 条
上限时会失败关闭，而不会猜测分页而遗漏成交。它同时只读本地执行状态。账户
UID、余额、订单和成交永不写入日志、报告或 artifact。启用持久化时，artifact 只保留
`binance_reconciliation_candidate.v1` 的摘要和稳定阻断码，保存 7 天。

若交易所未返回账户 UID、受管交易对未配置、任一读取失败、私有预期摘要不存在或任一摘要
不一致，运行成功完成为“候选被阻断”，而不是重试、降级或恢复交易。

## 受限恢复控制器

显式选择 Runtime 的 `recovery_action=prepare`，同时 `reconcile_only=true`，可生成并发布恢复候选。
运行目标必须已停用，分支必须是 main，不能混用其他诊断或 artifact 模式。两项恢复凭据仅在对应
prepare / verify / activate 步骤消费；普通运行不取得这些凭据。控制台固定为既有 QSL 私有控制台，禁止重定向和自定义目标。

本次适配严格绑定 2026-09-03 的既有冻结收据（Runtime 33768005187 / artifact 9898377291）、
原六项预期摘要及已核实的 13 个受管交易对。读取完整挂单、近七日成交、未归一化的真实本地账本，
并读取从冻结采样时刻起最多七天的资金历史。账户、订单、成交、账本必须匹配；余额只允许
通过已验证的 BONUS 流水逐项回退后精确匹配原余额摘要。其他资金活动、未解释差额、读取不完整、
窗口超限或采样期间余额变化均阻断。原冻结摘要从不改写。

采用 QPK `c4f9b7599041406d4693083bf2eb4e85d6e10059` 的来源绑定候选 v2：至少一份完整、
新鲜、可验证来源的对账证据；模型复核属于可选建议，缺少时明确为 unavailable，不生成虚假审核票。
候选在既有 Firestore 的 `strategy/MULTI_ASSET_STATE__recovery` 保存脱敏证据和实际 main 运行来源，
控制台仅收到摘要。保存前事务检查账户执行锁不存在、账本摘要未变化及旧控制文档未变化。
候选来源还须在后续步骤由 GitHub API 核验为对应 main workflow 的成功运行。

账户持有人需在控制台对该候选实际确认。`recovery_action=verify` 或 `activate` 必须提供 prepare
返回的精确 `recovery_id`，读取控制台真实确认回执，再重新读取交易所。证据必须晚于确认且
全部摘要与该候选一致；候选和确认均受 30 分钟新鲜度约束。verify 不写入；activate 仅在单次
Firestore 事务中精确切换同一控制文档至 ACTIVE_LKG，同时保存五项预期摘要与确认绑定。
事务冲突或远端结果不明时停止，不自动重试、清除执行锁或修改交易账本。

运行侧仅在显式配置 `BINANCE_RECOVERY_CONTROL_ENABLED=true` 后读取该已提交控制记录。
完整绑定有效时，只替换已验证运行目标的连续性状态；其他身份、策略和风控字段不变。
`RUNTIME_TARGET_ENABLED` 仍是独立停用开关，activate 不会修改它。只有完成上述恢复并核验后，
操作员才可按授权恢复既有调度。所有恢复步骤均不调用 `main.py`，不下单、撤单或执行资金操作。

本次历史窗口是有意限定的；过期或配置变化时保持关闭，重新调查对应基线，不能扩大窗口或盲目重采
来接受未知账务变化。恢复步骤失败仅输出阶段及固定原因码；先只读诊断，不盲目重复可变远端操作。

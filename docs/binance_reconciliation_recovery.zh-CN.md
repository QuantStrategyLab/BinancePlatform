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


## 已批准新期初的范围外 Spot 资产

2026-09-12 确认：保留原策略管理范围，范围外 Spot 资产仅参加全账户只读核对，不交由策略买卖。
post-rebase 控制器在两次采集及确认后复采中显式采用 `observe_only`；来源摘要绑定该政策和
范围外非零资产数量。全部 Spot 余额继续参与账户摘要，任何锁定余额、挂单、采样变化、
确认后的余额变化或不完整历史仍会阻断。原受管资产数量须精确匹配批准期初；原账本和
archive 的冻结摘要不变，历史差额仍标为未解决。

实际运行加载策略池时，以账本已有 `last_balance_snapshot` 的资产集合限制 resolved 与
含旧持仓的 effective 趋势池，禁止新增资产，并核对交易对与 base asset 一致。
BTC、BNB、USDT 保留固定角色，不能混入趋势部分重复计值。检查在任何策略池元数据持久化前执行；
后续快照由同一范围生成，范围只能收缩，重新扩大需要另行明确授权和处理。本次不新增资产清单服务。

`diagnose` 仅验证材料；通过不表示已发布候选、已人工确认或已恢复交易。
首次流水 cursor 初始化仍暂缓，不能修改冻结账本以绕过恢复校验。


## 闲置理财资金链更正（2026-09-12）

PR249 将“理财单独管理”误解成策略只管理 Spot，已撤回该范围变化；策略保留原批准的
Spot + Flexible Earn 持有资产口径，闲置理财不等于资金退出策略。读取 Earn 失败或分页不完整
必须拒绝估值，不能回退为 Spot 并产生假的资金减少。

申购/赎回首先持久化未决状态，收到 purchaseId/redeemId 后保存原回执。只读查询匹配的
历史记录，核验产品、资产、金额、SPOT 账户及 SUCCESS/PAID 状态，并核对实际 Spot 余额变化；
未完成时保持未决，不重复 POST、不继续下单。无回执的未知结果仍需独立核查，不能猜测成功。
内部转移不能算策略盈利；自动申购设置不在本次更改范围内。
重启核验仅在既有账户 owner claim 成功后执行；遗留 owner 仍按既有人工恢复条件处理，
本改动不清锁、不自动抢占旧进程，不能宣称任意崩溃后均可自动恢复。

`audit` 对已批准新期初使用绑定归档中 opening_balance_observed_at，而非旧恢复日期，
两次校验归档绑定未变化。输出 BNB 的奖励数量匹配布尔值和计数，不输出余额、金额或私有明细。
数值匹配仅用于缩小根因，不授予账本修改或交易权限；历史 BNB 差额仍须获得完整解释。

依据：[Binance Flexible Earn FAQ](https://www.binance.com/en/support/faq/detail/3bd1a6eba20a445da1e94bf6cfa52e80)
与 [Simple Earn REST API](https://developers.binance.com/en/docs/catalog/investment-and-services-simple-earn/api/rest-api/flexible-locked)。
本轮自动测试使用模拟资金；代码与 CI 通过不等于真实申赎、账务恢复或策略激活。


### BNB 差额诊断的时间粒度与范围

官方 Flexible FAQ 明确余额每分钟累计收益，历史提供日记录。批准期初位于日内时，
不能将按记录时间筛选的日收益总额解释成该期初到当前快照之间的应计收益。
`delta_matches_*` 仅是数字比较；`realtime_reward_interval_alignment` 明确标注未验证。
这并不证明实际差额完全来自收益，也不授权放宽恢复检查。

仅当 audit 发现 BNB mismatch，额外各读一次官方 Wallet `asset/assetDividend`（BNB，最多500）
及 `asset/dribblet`（Spot，最多100）。窗口沿用已验证期初至当前，满页、数量不符、越界或读取异常
均标未验证；只输出计数、失败码和 BNB 变化方向，不输出金额或原始行。
这两个检查不接入 recovery 的准入谓词，不宣称覆盖全部资金路径；交易、账本与账户设置均不修改。
官方接口：https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/asset 。


## 当前时点新期初预览（2026-09-12）

`rebase-proposal` 现按原账本的受管资产范围采集当前 Spot + Flexible Earn，保存产品收益计数和
外部流水去重游标。采样前后没有挂单、采样窗口没有成交或外部流水变化，产品生命周期相同且
数量变化严格等于实时奖励计数增量时，可生成加密预览；正常分钟计息不会因快照不完全相同而被拒绝。
产品变化、计数回退、抵押、余额锁定、不可赎回、分页不完整或未解释增减仍拒绝。

该预览是跨 API 的受限观察，不是原子账户证明。旧档案和历史差额保留，历史奖励不被当作新期盈利，
不无限回溯历史。起点取实际采集时间，不能回填为当天零点。已有加密 artifact 路径保持不变。
`automatic_accounting_ready`、`recovery_ready`、`execution_authority_granted` 均为 false；
本功能不写账本、不迁移、不激活、不改变自动申购设置。旧的 apply 白名单没有改动。

`application/earn_accrual.py` 提供纯 Decimal 增量守恒核对，要求调用方逐资产给出已独立核实的非利息净变动；
不可将余额差额反推为已核实流水。该核对尚未接入日常自动记账和恢复消费者，不能将此工程预览称为
生产自动记账已完成。实际迁移仍需具体快照确认，并完成向前记账消费者和恢复校验后再验收。

余额响应中的范围外资产名称可为 Unicode，按原名去重，不添加到受管范围；所有返回行仍检查金额与锁定。
依据：[Binance REST API Unicode 示例](https://developers.binance.com/en/docs/products/spot/rest-api)。

## 向前收益记账消费者（2026-09-12）

仅账本已含 `earn_accrual_checkpoint` 时启用新路径；旧账本继续使用原路径，不自动迁移。
加载时保留 checkpoint 与 `earn_accounted_net_changes`。市场估值与记账使用同一次完整 Spot/Earn
数量采样；既有 owner-protected writer 一次保存下一检查点、流水游标、余额及清零后的已记成交净变动。
验证失败不推进；写入失败不更新内存检查点，也不自动重试。

数量变化按同产品实时计数增量、已持久化的完整成交净数量/手续费、已核实当日 USDT 入金核对。
成交在原 FILLED_ACCOUNTING_PENDING → TERMINAL 记账步骤累计原始 Decimal 数量，费用从对应资产扣除；
不通过余额差反推资金来源。中途资金核验可更新当前余额，但不单独推进收益检查点或流水游标，避免漏掉区间资金活动。
理财奖励已体现在权益中，不再加一次利润，也不记作外部本金。

未知/未完成订单、产品消失或更换、计数回退、新提币及未支持币种入金继续拒绝。内部申赎不算收益；
只有同产品的总数量守恒可直接通过，产品生命周期不连续时不能猜测新的累计计数起点。
这不是所有资金活动均可自动分类的声明。

新期初 proposal 同时包含全资产零 `earn_accounted_net_changes`。它仍不是可执行迁移候选；
9月11日旧 apply/recovery 根保持不变。新迁移需要具体快照的人工确认，随后基于真实迁移和完整归档
绑定恢复验证；本改动没有切换期初、解除熔断或授权实盘。

## 已批准的新期初一次迁移（2026-09-12）

用户已批准 Runtime `34690028846` 的完整方案。`prospective-rebase-apply` 仅接受其精确审批内容摘要，
通过临时受保护的 `BINANCE_APPROVED_PROSPECTIVE_OPENING` 环境输入在内存读取；不在仓库、日志或
明文工件保存账户内容。配置只对该显式停用迁移步骤提供，使用后删除；不传递本机解密私钥，
日常生产不依赖该临时配置或 Mac。

只写原批准快照字段，生效观察时间保留为批准快照时点，不用执行时价格改写批准估值。
执行前核对原账本、控制及版本、账户身份；快照须同日且不超过24小时。批准时点至当前须零挂单/成交、
流水去重记录不变，Spot+Earn增量只能由同产品实时收益计数解释。原有非零外部本金累计会阻断，
不能借新期初重置未批准的其他字段。

单次事务同时读取 owner/ledger/control/新archive，只有身份和版本均保持不变且archive不存在才执行：
create完整旧账本、旧控制、原审批材料及新账本摘要；update批准的会计字段和新归档标记。
新归档为 `MULTI_ASSET_STATE__before_rebase_34690028846`，旧归档及其链条保留。提交或读回不明即uncertain，
不重复提交。成功读回整本账、归档和未改变的控制后才报告迁移完成。旧rebase-apply常量和恢复路径未切换，
本操作不解除熔断、不改策略、不恢复调度或交易。

## 新期初只读核验（2026-09-12）

`recovery_action=diagnose` 在账本指向已批准的新归档时，核验真实迁移
`34690695663@a3ef5660e6d25fcfd5a7dedd10536a32eedde203` 与固定整账、归档和审批材料。
复用现有向前记账消费者，在内存核对两次 Spot/Earn 采样、收益计数和已核实资金流；
两次采样之间也必须守恒，不能用余额差猜测本金。检查自新期初以来受管交易范围内的成交与全账户挂单，
结束后读回账本、控制、归档保持不变且无 owner。历史未知差额继续保留。

此检查不写账、不生成候选、不发布控制台授权；原 prepare/verify/activate 根保持不变。
检查通过只证明当次新期初向前核算一致，不表示交易恢复或完整日常业务周期验收。

## 新期初正式恢复接线（2026-09-12）

`prepare/verify/activate` 与运行消费者现在识别 `prospective_rebase`。共享 QPK 与控制台协议不变：
候选仍绑定真实 Spot 余额、挂单、成交及完整账本；Flexible Earn 的收益增长由已批准 checkpoint
向前守恒单独证明，不把收益差额复制成期望余额，也不修改历史未知差额。最后 Spot 采样须与
通过守恒的 checkpoint 相符，并在读取订单后保持稳定。

准备时只允许原归档 control，或已严格验证来源、成功生产 run 且超过原30分钟时效的同类候选。
使用既有 owner/ledger/archive/previous 原子条件保存，向管理网站发布脱敏候选。未过期候选
不覆盖；显式重建会更换 recovery_id 和 candidate，旧确认不能复用。

verify/activate 先读取网站管理员对具体候选的确认，再重新采集并核对向前守恒，Spot/订单/账本
五项摘要必须与被确认候选一致。只有 activate 才可执行既有原子控制状态切换；运行端验证
候选、来源、确认与 transition plan 的绑定后消费 ACTIVE_LKG。

此流程不打开 RUNTIME_TARGET_ENABLED、不解除熔断、不改变策略或风险预算，也不下单。
实际激活和运行仍需明确授权；本地闭环测试不代替真实人工确认或日常业务周期验收。

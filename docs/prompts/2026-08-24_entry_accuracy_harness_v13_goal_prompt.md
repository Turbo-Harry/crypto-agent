# 入场准确率、Harness v13/v11 与主动提案 v5 目标 Prompt

> 状态：权威实施稿；仅 OKX 模拟盘 shadow，不是已验证模型或下单许可
> 日期：2026-08-24

## 当前事实基线

- 10 币与 Alt 独立 90 天重放的 A/B 费用后 EV 和 Brier skill 均为负，validated factor=0，
  `/models/entry` 为空；不得把“模型为空”当成需要绕过的故障。
- Harness v8/v6 已有 12 条自然 pending run：6 reject、5 abstain、1 schema error，仍未取得任何成熟
  4h 结果，`veto_enabled=false`。其中 GRASS long 把正新闻误报为冲突，DOGE/HOOD 又把普通波动冒充
  `extreme_market_event`；v9/v7 因此把新闻方向与显式严重事件资格提升为机器契约。完整重启后的首批
  两条自然 run 均一次完成、零修复：ADA short 合法使用正新闻冲突+9.12 bps 点差；HOOD short 虽不再
  冒充严重事件，却把方向完全一致的技术结构加 disorder 写成 `signal_inconsistency`。新 challenger
  冻结为 Prompt v10 + Tool Policy v8，只用带方向符号的冻结因子决定该风险族。v10 完整重启后的首轮
  自然 run 又显示布尔资格过粗：GRASS short 确有正 `trend_band_atr` 冲突，但模型把负的顺向动量写成
  “正动量冲突”。v11 因此逐字列出真正冲突因子并校验理由引用。其首批 7 条自然 run 中，具备两族
  资格的 BTC/LINK/LTC 3 条均首次完成；只有单族资格的 INJ/ETH/BNB/ENA 4 条却在一次修复后仍错误
  尝试 reject，全部 schema fail-closed。v12/v10 把精确合格风险族和 reject 证据地板前置，首条自然
  SOL 单族样本已首次完成并正确 abstain；静态审计随后发现旧“高风险+高信心必须 reject”校验与单族
  禁止 reject 冲突。当前 challenger 冻结为 Prompt v13 + Tool Policy v11，只在证据地板满足时强制
  reject；地板不满足时允许诚实保留高风险估计并 abstain。仍为 pending shadow，`veto_enabled=false`。
- 主动提案当前冻结 implementation 为 `agent-proposal-impl-v6-directional-vwap-slippage`；v4.2 四个自然
  批次仅 1 completed、3 schema error，最新错误被定位为双提案长 evidence 在 200-token 共享预算下没有
  形成完整 JSON。v5.0 在代码提交后、进程完整重启前被 config 热重载，产生 1 条“新身份+旧函数体”的
  schema error，已由 v5.1 身份隔离。完整重启后的首个 v5.1 自然批次已在 1585ms 内 completed，生成
  INJ long 与 ZRO short 两条有效 shadow 提案；两条均为 2:1、只有两个必要 evidence 锚且零执行权限。
- `signal-features-v5` 已随提交 `bdee018` 完整重启 paper：首批 8 条自然 A 候选的方向化逐档 VWAP 滑点
  为 0.52～15.18bps，普通一档可完成样本准确落在半价差，HBAR 的 150 USDT 跨档路径产生 15.18bps；
  旧深度利用率 proxy 的数千 bps 已退出当前身份。C v6 首批 2/2 提案也冻结 v5 schema、固定 2:1 且
  `execution_authority=0`。HBAR 的单族高摩擦样本两次重复同一 evidence ID 后被确定性校验失败关闭为
  `baseline_pass`；该 Trace 不计作 reject，也不因为格式完成率而修改风险门。
- 入场概率与极值模型按 long/short 分开训练；300/60/60 是每个拟训练方向的门，不是把双方向总数相加。
  研究链现已按完整 `config_identity` 隔离：本轮部署审计时 v5 为 A 9 条、B 7 条、C 5 条 pending，
  outcome 均为 0。旧 A 的 179 个 outcome 与旧 v7/v4 Harness 的 3 条成熟判断只保留
  审计，不再补当前训练、校准或 100/30 门。

## 目标

1. 对冻结的 15m 方向候选估计未来 4h 费用后亏损概率。
2. 只拦至少两个独立、方向正确且字段语义可机器验证的普通风险族，或一个可核验严重事件。
3. 用自然 paper 的 4h 路径证明相对量化基线的费用后增量，而不是用减少交易数冒充精准率。
4. 保持固定 2:1、单笔风险 1%、名义 150 USDT、组合 600 USDT 与交易所侧止损；Agent 只否决不放行。
5. 让 C 主动提案扩大可审计候选来源，但在独立概率模型与自然 shadow 全部门通过前永远保持零执行权限。

## 步骤

1. 冻结完整身份：精确 `strategy_version`、Prompt v13、DeepSeek 模型、Context、Schema、Retrieval、Tool Policy v11
   和价格口径任一变化都重新计样本，旧版本只保留审计。采样、因子试验、概率/极值训练、经验预测、
   校准、模型生命周期与 readiness 必须逐层使用同一 `config_identity`，不得只在接口展示层隔离。
2. 按方向解释动量和资金费：long 的负动量、short 的正动量才是逆向；正资金费不是 short 成本，
   负资金费不是 long 成本。
3. 消除流动性字段歧义：`depth` 是回踩位置质量，`book` 是方向对齐盘口失衡，
   `book_imbalance/depth_imbalance` 是买卖压力方向；它们都不是绝对可见深度，不得支持
   `liquidity_failure`。`expected_slippage_bps` 必须是 150 USDT 逐档成交 VWAP 相对 mid 的方向化价格
   冲击；深度不足必须缺失，禁止用名义额/可见深度利用率冒充价格 bps。
4. 固定流动性严重门：只有 `spread_bps≥8` 或 `expected_slippage_bps≥10` 才取得
   `liquidity_failure` 风险族资格。低于门槛仍可影响总体概率，但不算独立拒绝证据。
5. 机器校验概率/信心、方向、资金费、持仓冲突、流动性门、风险族数量和 evidence 锚；首次判断即收到
   与校验器同源的 `allowed_evidence_ids`，以及动量逆向、账户冲突、严重流动性和资金费成本四项冻结
   资格。v13/v11 还必须收到新闻方向冲突、显式严重事件和信号不一致三项资格：news_score/composite 按 [-1,+1]
   符号解释；没有冻结 `extreme_market_event=true` 时，高波动/regime/forecast 都不能冒充严重事件。
   `signal_inconsistency` 只接受 1H/4H 动量、`trend_band_atr` 或 `directional_index_spread` 中至少一项
   符号与候选方向相反；契约还必须列出具体冲突因子，理由不得把清单外的顺向因子写成冲突。
   disorder、波动水平、route 或缺模型本身都不能取得该资格。
   首次契约还必须列出精确 `qualified_ordinary_risk_families` 与
   `reject_evidence_floor_satisfied`。只有至少两个合格普通风险族或显式严重事件时才可 reject；false
   时必须按概率门选择 abstain/approve；即使风险概率与信心均不低于 0.70，也必须保留真实估计并
   abstain，不能人为压低信心。缺失字段只能降低信心，不能凑第二风险族。
   修复轮继续复用同一契约，重复 evidence ID 必须去重。最多修复一次，仍不合格或总耗时超过 4 秒就
   失败关闭并保持量化基线。
6. 只在 paper 自然 shadow 收集 A/B 分策略 run→outcome→evaluation→version 证据；live 永久 shadow，
   B 无自动执行权限。A/B 都必须在自然候选形成的同一轮冻结盘口、点差、预期滑点和可用订单流；交易所
   提供盘口时不得以空字段进入 Harness，取数失败必须显式保留缺失并继续 shadow，不能用默认值制造证据。
   依赖相邻快照的盘口、OI 或缓存状态必须按策略隔离，B 不得改写 A 的采样历史。
   无偏 Trace 可以在量化门前留样，但 Harness 100/30、Brier 和增量 EV 的晋升集合必须与真实消费点一致：
   A 只统计量化、2:1、active 入场模型和经验门全部通过的 `rule_decision=pass` 候选；legacy AI 本来就会
   拒绝的候选不得给 Harness 记功。B 只按自己的 shadow baseline 独立研究。接口必须同时报告全部成熟
   Trace、baseline eligible 和 excluded 数，不得删除排除样本或把结构候选总量冒充可执行增量样本。
7. 对 C v5 每个批次逐字冻结 `aligned_direction`、`eligible_candidates`、微观结构、input hash 和
   implementation identity；每条只回传 15m 与 microstructure 两个必要锚，模型只能选择确定性合格方向，
   JSON/Schema/证据/方向任一错误都失败关闭。
8. C 提案只写 `rule_decision=shadow`、`final_decision=rejected`、`execution_authority=0`，按固定 1R:2R
   结算完整 4h/1m 首触、MFE/MAE；与 A/B、旧 C 版本、历史回放严格隔离。
9. 每轮先做数据与身份审计，再做 purged walk-forward、概率校准和费用后 EV；结果不通过就记录停止裁决，
   不搜索新阈值、不打开 sealed holdout、不修改活体数据库。

## 验收标准

- 当前完整 v13/v11/策略配置身份下，每策略 baseline-eligible 自然成熟样本至少 100，合格 reject 至少
  30；Trace、概率和 evidence 覆盖 100%。量化基线已拒或 legacy AI 已拒的成熟 Trace 只作审计，不补门。
- 所有 `liquidity_failure` 都满足冻结点差或预期滑点门；方向失衡不得冒充绝对深度不足。
- 当前样本必须是 `signal-features-v5`；旧 v4 的深度利用率 proxy 不得与逐档 VWAP 语义混入同一验收。
- A/B/C 当前候选、outcome、因子试验、模型制品、校准和生命周期的 strategy identity 必须逐项相同；
  readiness 的 Harness 门必须等于当前 v13/v11/v5 完整版本，旧版本计数非零也不能补当前 100/30。
- A/B 分策略审计盘口、点差、预期滑点和订单流覆盖；FakeAdapter 提供盘口时端到端候选必须冻结非空
  `book_imbalance/spread_bps/expected_slippage_bps`，并验证 B 的盘口/OI 状态不会改写 A 的同名状态，
  同时保持 B 零订单、零执行权限。
- 总延迟不超过 4 秒；迟到、超时、Schema、网络或 Trace 错误均为零 Veto。
- Brier skill 不低于频率基线，风险概率标准差至少 0.03，校准不得系统性反向。
- saved loss 大于 missed profit 加模型成本，费用后增量 EV 单侧 95% 下界大于 0。
- reject 不得由单一币种、方向、regime 或月份贡献超过 80%；方向占比必须作为独立机器字段和硬门，
  不能用多个 symbol×direction×regime 组合稀释同一方向的集中度。
- C 当前完整身份在每个拟训练方向分别至少取得 300 条自然成熟提案，且该方向 TP-first/SL-first 各至少
  60；5 折中至少 4 折通过，Brier skill 至少 0.05，费用后 EV 单侧 95% 下界大于 0，且 DSR/PBO 与
  币种、regime、月份稳定性全部通过，才允许为该方向生成 shadow 入场模型。另一方向不得借样本晋升。
- C 模型生成后仍需至少 60 条独立 shadow 候选、30 条已关闭、30 条被模型选择的完整评估；实际费用后
  EV 继续为正、Brier 不恶化、最大回撤不超过门限，才可另提人工批准。本文不授予该批准。
- 当前协议的每个提案 input hash 必须从冻结 payload 逐字复算一致，2:1 几何严格成立，任何记录的
  `execution_authority` 非 0、或 Agent 恢复基线拒绝，均为立即失败。
- Agent 专项、全量自动发现测试、依赖图、参数、隔离和修复护栏全绿；只重启 paper，并验证健康、
  心跳、持仓衔接、对账和错误接口。

## 停止规则

- 样本不足继续 shadow，不把 long/short 合计冒充单方向 300/60/60，也不降低 Harness 100/30。
- 全量结构 Trace 达到 100/30、但 baseline-eligible 子集不足时仍继续 shadow；不得靠拦住本来不会下的
  候选取得增量 EV 或 Veto 权限。
- Brier、绝对费用后 EV、增量下界或分段稳定任一失败，停止晋升。
- A/B/C 的独立 90 天或自然 paper 费用后 EV 下界非正时，模型列表保持为空；不得用降低样本门、删亏损、
  换费用口径、增加同一行情的重叠候选或只报胜率来制造通过。
- 不得改活体数据库、伪造 outcome、混历史重放、直接标 active 或让 Harness 恢复基线拒绝。

## v13 冻结反例

v7/v4 首轮 A 自然 Trace 有 XRP/SOL/LTC 三个 reject。XRP 的点差/预期滑点为 3.389/5.372 bps，
SOL 为 1.063/1.739 bps，二者却因负盘口失衡或高 `depth` 分被描述为流动性失败；实际上这些字段
是方向压力或回踩质量，不是绝对深度。LTC 的预期滑点 18.152 bps 才符合严重执行摩擦。v8 只修正
Prompt 与确定性流动性语义，不改变模型、Context、0.70/0.70、100/30 或交易权限。首条 v8/v4 自然
run 暴露修复轮反复自造字段级 evidence_id；Tool Policy v5 只把校验器同源的合法锚白名单显式交给
修复轮。自然首批 v8/v5 又证明首次请求仍会浪费在猜锚和猜资格上，因此 Tool Policy v6 把同源锚与
四项冻结资格前置到第一次请求；它不改变 Prompt v8 的风险任务或任何交易权限。v8/v6 随后的自然
GRASS long 又把 `news_score=0.5714/composite=0.5157`、bull 11/bear 3 的正新闻写成
`news_direction_conflict`；DOGE/HOOD 把普通 `vol_expansion`/高波动写成 `extreme_market_event`，HOOD
还重复同一 market evidence ID。v9/v7 只把这三种可核验语义错误提升为机器门，不读取未来 outcome。
首批 v9/v7 自然 HOOD short 又显示，模型会把旧的严重事件误判替换成 `signal_inconsistency`：其
1H/4H 动量、EMA 趋势带、VWAP 距离都与 short 同向，理由实际只剩 disorder/高波动。v10/v8 因此把
四个有明确方向语义的冻结因子交给与 validator 同源的资格函数；不引入 outcome 阈值，也不把 regime
或治理 route 当成方向冲突。旧 v9/v7 Trace 保持原语义回放，不与新身份混计。
v10/v8 首轮自然 GRASS short 的冲突清单实际只有 `trend_band_atr`，但模型理由把数值为负的 1H/4H
动量写成“正动量”。v11/v9 在布尔资格之外提供精确因子清单，并对理由中 momentum、趋势带或 DMI
族的引用逐族复算；风险族整体合法不再掩盖具体理由错误。旧 v10/v8 仍保持原语义回放。
v11/v9 在 19:15 自然批次出现清晰分界：BTC/LINK/LTC 同时具备严重滑点与逆向动量两族，3/3 首次
完成；INJ 只有严重滑点，ETH/BNB 只有方向冲突，ENA 只有新闻冲突，4/4 却反复尝试 reject 并在一次
修复后失败。v12/v10 把 validator 已能复算的族清单和“是否满足 reject 地板”直接放进首次契约；
不改 0.70/0.70 或两个普通风险族门，只消除模型自行组合资格的错误。
v12/v10 首条自然 SOL long 只有 `signal_inconsistency` 一族，首次调用即 completed 并 abstain；但代码
审计发现，如果同类单族样本给出风险概率与信心都不低于 0.70，旧 validator 会要求 reject 或降低信心，
与地板 false 时禁止 reject 的机器契约冲突。v13/v11 只把这条强制改为“证据地板满足时才要求
reject”；地板 false 时允许高风险高信心 abstain，旧 v12 仍按冻结语义回放。

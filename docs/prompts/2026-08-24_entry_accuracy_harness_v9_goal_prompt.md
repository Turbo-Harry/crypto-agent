# 入场准确率、Harness v9/v7 与主动提案 v5 目标 Prompt

> 状态：权威实施稿；仅 OKX 模拟盘 shadow，不是已验证模型或下单许可
> 日期：2026-08-24

## 当前事实基线

- 10 币与 Alt 独立 90 天重放的 A/B 费用后 EV 和 Brier skill 均为负，validated factor=0，
  `/models/entry` 为空；不得把“模型为空”当成需要绕过的故障。
- Harness v8/v6 已有 12 条自然 pending run：6 reject、5 abstain、1 schema error，仍未取得任何成熟
  4h 结果，`veto_enabled=false`。其中 GRASS long 把正新闻误报为冲突，DOGE/HOOD 又把普通波动冒充
  `extreme_market_event`；新 challenger 因此冻结为 Prompt v9 + Tool Policy v7。完整重启后的首批两条
  自然 run 均一次完成、零修复：HOOD short 使用正新闻冲突+结构不一致，ADA short 使用正新闻冲突+
  9.12 bps 点差；两条都没有冒充严重事件，仍为 pending shadow，`veto_enabled=false`。
- 主动提案当前冻结 implementation 为 `agent-proposal-impl-v5.1-full-restart-boundary`；v4.2 四个自然
  批次仅 1 completed、3 schema error，最新错误被定位为双提案长 evidence 在 200-token 共享预算下没有
  形成完整 JSON。v5.0 在代码提交后、进程完整重启前被 config 热重载，产生 1 条“新身份+旧函数体”的
  schema error，已由 v5.1 身份隔离。完整重启后的首个 v5.1 自然批次已在 1585ms 内 completed，生成
  INJ long 与 ZRO short 两条有效 shadow 提案；两条均为 2:1、只有两个必要 evidence 锚且零执行权限。
- 入场概率与极值模型按 long/short 分开训练；300/60/60 是每个拟训练方向的门，不是把双方向总数相加。
  当前 A long 成熟 130（TP 41/SL 64），A short 成熟 40（TP 6/SL 31），均未达到训练门；C v5.1
  当前为 2 条 pending，不能计入成熟样本。

## 目标

1. 对冻结的 15m 方向候选估计未来 4h 费用后亏损概率。
2. 只拦至少两个独立、方向正确且字段语义可机器验证的普通风险族，或一个可核验严重事件。
3. 用自然 paper 的 4h 路径证明相对量化基线的费用后增量，而不是用减少交易数冒充精准率。
4. 保持固定 2:1、单笔风险 1%、名义 150 USDT、组合 600 USDT 与交易所侧止损；Agent 只否决不放行。
5. 让 C 主动提案扩大可审计候选来源，但在独立概率模型与自然 shadow 全部门通过前永远保持零执行权限。

## 步骤

1. 冻结完整身份：Prompt v9、DeepSeek 模型、Context、Schema、Retrieval、Tool Policy v7 和价格口径
   任一变化都重新计样本，旧版本只保留审计。
2. 按方向解释动量和资金费：long 的负动量、short 的正动量才是逆向；正资金费不是 short 成本，
   负资金费不是 long 成本。
3. 消除流动性字段歧义：`depth` 是回踩位置质量，`book` 是方向对齐盘口失衡，
   `book_imbalance/depth_imbalance` 是买卖压力方向；它们都不是绝对可见深度，不得支持
   `liquidity_failure`。
4. 固定流动性严重门：只有 `spread_bps≥8` 或 `expected_slippage_bps≥10` 才取得
   `liquidity_failure` 风险族资格。低于门槛仍可影响总体概率，但不算独立拒绝证据。
5. 机器校验概率/信心、方向、资金费、持仓冲突、流动性门、风险族数量和 evidence 锚；首次判断即收到
   与校验器同源的 `allowed_evidence_ids`，以及动量逆向、账户冲突、严重流动性和资金费成本四项冻结
   资格。v9/v7 还必须收到新闻方向冲突和显式严重事件两项资格：news_score/composite 按 [-1,+1]
   符号解释；没有冻结 `extreme_market_event=true` 时，高波动/regime/forecast 都不能冒充严重事件。
   修复轮继续复用同一契约，重复 evidence ID 必须去重。最多修复一次，仍不合格或总耗时超过 4 秒就
   失败关闭并保持量化基线。
6. 只在 paper 自然 shadow 收集 A/B 分策略 run→outcome→evaluation→version 证据；live 永久 shadow，
   B 无自动执行权限。
7. 对 C v5 每个批次逐字冻结 `aligned_direction`、`eligible_candidates`、微观结构、input hash 和
   implementation identity；每条只回传 15m 与 microstructure 两个必要锚，模型只能选择确定性合格方向，
   JSON/Schema/证据/方向任一错误都失败关闭。
8. C 提案只写 `rule_decision=shadow`、`final_decision=rejected`、`execution_authority=0`，按固定 1R:2R
   结算完整 4h/1m 首触、MFE/MAE；与 A/B、旧 C 版本、历史回放严格隔离。
9. 每轮先做数据与身份审计，再做 purged walk-forward、概率校准和费用后 EV；结果不通过就记录停止裁决，
   不搜索新阈值、不打开 sealed holdout、不修改活体数据库。

## 验收标准

- 当前完整 v9/v7 每策略自然成熟样本至少 100，合格 reject 至少 30；Trace、概率和 evidence 覆盖 100%。
- 所有 `liquidity_failure` 都满足冻结点差或预期滑点门；方向失衡不得冒充绝对深度不足。
- 总延迟不超过 4 秒；迟到、超时、Schema、网络或 Trace 错误均为零 Veto。
- Brier skill 不低于频率基线，风险概率标准差至少 0.03，校准不得系统性反向。
- saved loss 大于 missed profit 加模型成本，费用后增量 EV 单侧 95% 下界大于 0。
- reject 不得由单一币种、方向、regime 或月份贡献超过 80%。
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
- Brier、绝对费用后 EV、增量下界或分段稳定任一失败，停止晋升。
- A/B/C 的独立 90 天或自然 paper 费用后 EV 下界非正时，模型列表保持为空；不得用降低样本门、删亏损、
  换费用口径、增加同一行情的重叠候选或只报胜率来制造通过。
- 不得改活体数据库、伪造 outcome、混历史重放、直接标 active 或让 Harness 恢复基线拒绝。

## v9 冻结反例

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

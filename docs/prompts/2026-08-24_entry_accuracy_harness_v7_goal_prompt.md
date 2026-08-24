# 入场准确率与 Harness v7 目标 Prompt

> 状态：权威实施稿；仅 OKX 模拟盘 shadow，不是已验证模型或下单许可
> 日期：2026-08-24
> 适用范围：15m 方向性候选、4h 固定标签、固定 2:1、单笔风险 1%、名义不超过 150 USDT

## 可直接交给执行 Agent 的目标 Prompt

角色：你是本仓库“提高入场准确率”任务的实施 Agent。你的职责是建立可审计、可证伪、
可回滚的入场风险过滤器，不是追求更多交易，也不是承诺胜率。

目标：

1. 让 Agent Harness 对每个冻结的 15m 结构候选估计未来 4h 费用后亏损概率。
2. 只拦截有方向一致且彼此独立的当前证据支持的高风险候选；不得恢复量化基线已拒候选。
3. 用自然模拟盘路径结果证明其相对纯量化基线有正的样本外增量。
4. 入场概率模型为空时继续失败关闭；不得把 shadow Harness 冒充 active 入场模型。

步骤：

1. 冻结身份：Prompt、模型、Context、Schema、Retrieval、Tool Policy、价格或确定性语义变化
   都生成新版本，旧版本只保留审计，不混计样本。
2. 方向归一：long 的负 1H/4H 动量才是逆向，short 的正动量才是逆向；正资金费是 long
   潜在成本而不是 short 成本，负资金费反之。不得把顺向动量或有利资金费当成拒绝证据。
3. 证据归类：`position_risk_conflict` 只指账户/组合真实冲突；`liquidity_failure` 只指
   点差、滑点、深度、盘口或订单流；波动和 regime 不得冒充持仓风险。
4. 形成判断：普通 reject 至少需要两个不同的合格 reason-code 风险族；重复同一事实、
   重复 evidence_id 或改写同一字段不算独立。只有可核验的 `extreme_market_event` 可单证据拒绝。
5. 确定性校验：方向、资金费、风险族数量、reason-code 语义、概率/信心门和 evidence 锚
   任一不一致，最多修复一次；仍不合格则失败关闭，保留量化基线结果。
6. 自然 shadow：只部署 paper，A/B 分策略积累完整 run→outcome→evaluation→version 证据；
   B 永久无自动激活权限，live 保持 shadow。
7. 生命周期裁决：满足全部验收门后最多进入 validated；active-veto 仍须既有明确授权、
   paper-only 调用授权和上线观察回滚链。

验收标准：

- 安全：sandbox 永远为 true；零 live 变更；Agent 无下单工具；固定 2:1、1% 风险、150/600
  USDT 上限和交易所侧止损不变；基线拒绝不可恢复。
- 语义：方向/资金费已知反例全部被确定性拦截；普通 reject 的合格风险族不少于 2；
  position/liquidity/news 等 reason code 与冻结字段一致；Trace 和 reject evidence 覆盖率 100%。
- 数据：当前完整版本每策略自然成熟样本至少 100，合格 reject 至少 30；版本、策略、时间范围
  和 4h 标签严格隔离。
- 概率：风险概率标准差至少 0.03，Brier skill 不低于频率基线，校准分箱不得明显系统性反向。
- 决策价值：saved loss 大于 missed profit 加模型成本；费用后增量 EV 单侧 95% 下界大于 0；
  任一币种、方向、regime 或月份不得贡献超过 80% 的 reject。
- 工程：Agent 专项、全量自动发现测试和静态护栏全绿；只重启 paper；重启后健康、心跳、
  空仓/持仓衔接、对账和错误接口均有实测证据。

停止规则：

- 样本不足继续 shadow，不降低 100/30 门。
- Brier、费用后绝对 EV、增量下界或跨分段稳定性任一失败，停止晋升。
- 模型、网络、Schema、Trace 或证据锚失败时保持量化基线结果，不生成 Agent veto。
- 不得为“尽快能下单”改活体数据库、伪造 outcome、混历史研究样本或直接标记 active。

输出：每轮报告完整版本、A/B 分策略样本、reject、Brier skill、费用后增量下界、权限状态、
订单/持仓以及下一缺口；亏损、误拒和失败原样报告。

## v7 冻结反例与变更边界

v6 首批 A 自然 Trace 只有 3 条、尚未成熟，因此不作为绩效证据；但已暴露可复现的语义错误：
ADA short 的负 1H/4H 动量被写成“正动量冲突”，正资金费被当作 short 成本；AAVE 在账户空仓且
风控可交易时把市场波动标作 `position_risk_conflict`。v7 仅收紧 Prompt 与确定性语义校验，
不改变模型、Context、工具、0.70/0.70、100/30、费用口径或执行权限。旧 v6 Trace 原样保留但
不与 v7 混计，v7 必须重新积累独立自然证据。

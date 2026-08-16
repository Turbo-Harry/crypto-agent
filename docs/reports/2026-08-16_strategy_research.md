# 业界交易策略调研报告 —— 订单流 / 聪明钱 / 分场景选策略（2026-08-16）

> 定位：进化循环中「调研子agent A」的交付物，为设计方案 v0.2 的 L2/L3 路线图供弹。
> 格式遵守 evolution_loop_prompt.md：每条结论 = `来源URL | 现状差距（对照本项目代码）| 预期收益（定量优先）| 风险与成本 | 优先级`。
> 诚实声明：所有"预期收益"在通过 S1/S3 验证门前均为**未证实**；本报告只报有证据的结论。

---

## R1 订单流 / 微观结构（CVD、主动买卖比、爆仓瀑布）

| 项 | 内容 |
|---|---|
| 来源 | [CoinGlass：CVD 与背离信号完整指南](https://www.coinglass.com/zh-TW/learn/cvd-en)、[Alertachart Orderflow Toolbox](https://www.alertachart.com/help/en/orderflow-toolbox)、[The Kingfisher：清算地图 × CVD 确认](https://thekingfisher.io/blogs/liquidation_maps_cvd) |
| 现状差距 | **本项目已有数据层但零接线**：`data/fetch_orderflow.py`（币安镜像订单簿失衡 + taker buy ratio）、`data/fetch_open_interest.py`（Gate.io OI/多空比/多空爆仓量）均已实现且实测可用；但方向性信号（`scan_signal`）只消费 K 线指标，订单流数据从未进入决策。 |
| 预期收益 | 行业定位是**确认器/风险预警**而非独立信号：CVD 与价格背离、爆仓瀑布（清算级联）作为入场确认或"危险区"过滤器，预期收益=降低假信号率；无公开可复现的定量收益保证。 |
| 风险与成本 | 免费源质量（币安镜像非实时、Gate.io 限速）；伪相关过拟合风险高。接线成本低（fetcher 已存在，只需采集 + 影子验证）。 |
| 优先级 | **P1**——数据已备，先影子采集进 Phase 1 特征表，不进决策。 |

## R2 聪明钱（SMC / ICT）

| 项 | 内容 |
|---|---|
| 来源 | [BMMagazine：SMC 是基础概念的换皮？](https://bmmagazine.co.uk/business/smcs-rebranding-of-basic-concepts-are-traders-being-misled/)、[AInvest：SMC 并不追踪真正的聪明钱](https://www.ainvest.com/news/smart-money-concepts-don-track-smart-money-real-plumbing-dealer-gamma-order-blocks-2608/)、[InsiderFinance：edge 幻觉与幸存者偏差](https://wire.insiderfinance.io/the-illusion-of-edge-smc-survivorship-bias-and-market-reality-ae7873ef154d)、[Gate Learn：SMC/ICT 概念介绍](https://web.gate.it/learn/articles/smart-money-concepts-and-ict-trading/5026) |
| 现状差距 | 本项目无任何 SMC 概念；且 SMC 中可证伪的部分（流动性扫损、止损位被扫）已由设计方案的 MAE/MFE + post_exit_reverse（DEF-3）覆盖。 |
| 预期收益 | **低且不可量化**：多数批评文献认为 SMC 主体概念不可证伪或即经典技术分析的换皮，教学材料的收益多为幸存者偏差；唯一有实证基础的是"流动性区域/清算带"（与 R1 爆仓数据重合）。 |
| 风险与成本 | **高**：不可证伪概念直接违反本项目"防过拟合/诚实评估"哲学（AGENTS.md）。 |
| 优先级 | **P3**——只提取可证伪子集：清算带 → 止损位选择研究（并入 R1）；其余概念不引入。 |

## R3 分场景选策略（regime 门控的策略套件）

| 项 | 内容 |
|---|---|
| 来源 | [Regime-Aware Systematic Equities Trading Platform（研究级多策略 regime 分配）](https://github.com/shprite21/Regime-Aware-Systematic-Equities-Trading-Platform)、[MarketRegimeTrader（HMM regime 检测 + 自适应策略，含 walk-forward 验证）](https://github.com/0x596173736972/MarketRegimeTrader) |
| 现状差距 | 本项目只有单一「回踩确认」策略，无 regime 分支；设计 v0.1 的 T1.4 只计划采集 regime 标签，未规划策略套件。 |
| 预期收益 | 这正是"根据不同场景选择不同策略"的业界标准做法：单一策略在不同 regime 分化明显，套件 + regime 门控可平滑表现；但每个新增策略 = 新增试验次数 → 按 S3（minBTL）需要更多样本，与当前样本稀缺直接冲突。 |
| 风险与成本 | 每策略必须独立过 S1-S3 验证门；HMM 类复杂模型对小样本必然过拟合——本项目应选**轻量 regime 标签**（波动率分位 + 趋势斜率，T1.4 已设计），不用 HMM。 |
| 优先级 | **P2**——设计先行（套件候选清单 + 门控规则），实现等样本达标后。 |

## R4 开源机器人 / 社区策略

| 项 | 内容 |
|---|---|
| 来源 | [paulcpk/freqtrade-strategies-that-work](https://kkgithub.com/paulcpk/freqtrade-strategies-that-work)、[Freqtrade vs Hummingbot 2026](https://voiceofchain.com/academy/freqtrade-vs-hummingbot-2026)、[DEV 社区 freqtrade 教程（自称 67.9% 胜率）](https://dev.to/trendrider/freqtrade-tutorial-2026-how-i-set-up-a-crypto-bot-that-hits-679-win-rate-18i5) |
| 现状差距 | 本项目无社区策略引入；已有回测结论（evolution_loop_prompt 记载）"传统技术指标策略严谨回测全亏或过拟合"，与社区策略普遍缺乏样本外证据一致。 |
| 预期收益 | 借鉴**结构**而非信号：风控模块、出场管理、hyperopt 流程、regime 过滤器的工程组织方式；社区信号策略的胜率宣称普遍无样本外验证，不可直接采信。 |
| 风险与成本 | 高——直接搬运 = 引入未验证黑箱。 |
| 优先级 | **P3**——只抄结构，不抄信号。 |

---

## 综合结论（喂给设计方案 v0.2）

1. **"不仅仅看指标"的落地路径**：指标仍做"信号触发"，但增加两条独立信息源做确认/否决——(a) 订单流（CVD 背离、taker imbalance，数据层已备）；(b) 清算带/爆仓瀑布（Gate.io 数据已备）作为风险预警与止损位参考。全部先影子采集，过 S3 验证门后才进决策。
2. **聪明钱的诚实回答**：SMC 教学概念证据不足，不引入；真正的"聪明钱"代理变量 = 订单流 + OI/爆仓数据（R1），这正是机构资金的可见足迹。
3. **分场景策略套件**（R3）候选清单（每项独立过验证门）：
   - 策略 A：趋势回踩确认（现有，1h+4h MTF）
   - 策略 B：突破/动量确认（regime=趋势强时启用，需订单流确认）
   - 策略 C：清算瀑布反转（regime=极端波动时启用，依赖爆仓数据）
   - 门控 = 轻量 regime 标签（波动率分位 + 趋势斜率），非 HMM。
4. **样本现实**：套件只增不减风险——当前 1 笔平仓，任何策略都不满足 S2 的 30 笔门槛；全部候选先以"影子重放 + 采集"模式运行（见设计 v0.2 对 Q1 的答复）。

## 附：本次调研的存量事实核对

- `data/fetch_orderflow.py` 与 `data/fetch_open_interest.py` 存在且为免费无 key 源（币安镜像 / Gate.io），但生产信号链（`engines/directional_trader.py:scan_signal`）未引用。
- 调研结论将与设计文档 v0.2 的 Phase 1（特征采集）与 Phase 3/4（学习闸门/策略套件）合并实施。

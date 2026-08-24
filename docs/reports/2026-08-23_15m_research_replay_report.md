# 15m/4h OKX SWAP 历史重放与统计裁决报告

> 日期：2026-08-23
> 性质：研究证据，不是模拟盘成交记录，不授权模型晋升或扩大预算。
> 对应权威计划：`docs/plans/2026-08-23_entry_accuracy_factor_forecast_FINAL.md`

## 1. 研究问题与防伪边界

本轮回答三个问题：当前 15m 回踩候选在真实合约历史行情上是否具备成本后正期望；自动
因子挖掘是否找到满足样本外门槛的因子；bootstrap 首触概率和条件极值模型是否优于简单
经验基线。

防伪约束：

- 只接受 OKX `*-USDT-SWAP`，现货 K 不作合约代理。
- 行情输入库只读，候选/标签写 `/tmp` 独立研究库，拒绝 `crypto_agent.db` 与
  `crypto_agent_live.db`。
- 15m 已收线 K 决定候选；entry 使用该根收盘价代理下一 tick，并在 provenance 明示。
- 结果只使用随后连续 4h 的 1m OHLC；中间缺一分钟即保持 missing。
- 30 天扩展库另从 OKX `funding-rate-history` 回填每币 90 条已结算资金费；候选只读取
  信号时点之前最近两次结算值及当时横截面分位，未来结算严格排除。book、OI 和 L2 事件
  历史仍不可得并保持缺失；因此该研究库仍不能冒充六维完整 paper 平仓。
- 历史预测使用固定 seed 的 moving-block OHLC bootstrap，不混入数据库经验结果；经验
  概率接口另加 as-of 截止，防止重放偷看未来。
- 本报告无 Agent 调用，不能产生“有效 AI 判断”或 Agent 增量证据。

## 2. 数据与可复现入口

工具：`tools/replay_15m_research.py`。

数据范围：BTC、ETH、SOL、XRP、DOGE、LINK、ADA、AVAX、BNB、LTC 十个
USDT 永续合约；1m/15m 回溯 8 天，1H/4H 上下文回溯 30 天。公开 OKX
`history-candles` 共收到 131,839 行；40 条“标的×周期”序列全部完成，单序列覆盖率
99.44%～100%。覆盖率不是结算通行证，路径连续性仍逐候选验证。

```bash
PYTHONPATH=lib:. python3 tools/replay_15m_research.py \
  --market-db /tmp/crypto-agent-15m-swap-market.db \
  --symbols BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP,LINK-USDT-SWAP,ADA-USDT-SWAP,AVAX-USDT-SWAP,BNB-USDT-SWAP,LTC-USDT-SWAP \
  --backfill-market --days 8 --context-days 30 --threads 6

PYTHONPATH=lib:. python3 tools/replay_15m_research.py \
  --market-db /tmp/crypto-agent-15m-swap-market.db \
  --output-db /tmp/crypto-agent-15m-research.db \
  --symbols BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP,LINK-USDT-SWAP,ADA-USDT-SWAP,AVAX-USDT-SWAP,BNB-USDT-SWAP,LTC-USDT-SWAP \
  --apply
```

## 3. 候选与路径标签

共扫描 7,085 个已收线 15m 时点，形成 437 个去重结构候选；414 个得到完整 4h/1m
结果，23 个因分钟缺口保持 missing。

| 范围 | N | TP first | SL first | timeout | 毛 EV |
|---|---:|---:|---:|---:|---:|
| 全部 | 414 | 131 | 246 | 37 | +0.0791R |
| long | 271 | 96 | 148 | 27 | +0.2183R |
| short | 143 | 35 | 98 | 10 | -0.1848R |

按当前保守成本口径：单边 taker 0.05% + 单边滑点 0.05%，换算为每笔相对自身 ATR
止损距离的 R 成本。成本后：

- 全部净 EV = -0.7571R，净盈利比例 34.78%。
- long 净 EV = -0.4599R，净盈利比例 40.96%。
- short 净 EV = -1.3204R，净盈利比例 23.08%。

因此即便 long 的障碍前毛结果为正，当前 15m 入场距离与市价成本组合仍不能证明可交易的
正期望；short 更差。该结果直接触发“长期/样本外 EV 未转正不得扩大预算”的停止条件。

## 4. 自动因子挖掘裁决

首轮 41 个注册因子经过 purged 5 折 walk-forward、4h embargo、成本、DSR、PBO、
分方向/标的/regime/月稳定性与缺失门：

- validated：0。
- reject：17。
- reject_missing：1。
- insufficient_data：23（主要是历史微观结构、拥挤与 5m 数据不可得）。

最接近阈值的 `hour_cos` 样本外 t=2.729，仍低于硬门 t≥3.0；其余因子也没有同时通过
DSR/PBO/折一致性/成本门。入口概率模型与极值模型因“已验证特征=0”均返回
`insufficient_data`，没有写入模型制品。

因子试验新增 `trial_key/data_hash/evaluation_version` 身份。同一数据和评估算法连续运行两次，
`factor_trials` 仍为 41 行；只有新标签、新特征或评估版本变化才形成独立试验。

## 5. 首触概率校准

437 个候选均在信号时点生成确定性 bootstrap 预测，其中 414 个随后取得真实路径标签并进入
校准。与同一批样本的常数经验频率基线比较：

| 指标 | 模型 Brier | 常数基线 | Skill |
|---|---:|---:|---:|
| TP | 0.222817 | 0.216300 | -3.01% |
| SL | 0.243410 | 0.241126 | -0.95% |
| TP/SL/timeout 多分类 | 0.554442 | 0.538811 | -2.90% |

模型三项均劣于简单基线，不能晋升。这里的 `calibrated` 只表示样本量足以计算校准统计，不表示
预测已经校准良好或具有决策价值。

## 6. 最高/最低条件分位探索

为诊断模型上限，使用 11 个仅 OHLC/时段可得特征做不落制品的 exploratory walk-forward；
这些特征没有通过 T5，因此结果没有资格进入正式训练：

- long：271 条、4 折；相对经验分位 pinball 改善 -99.47%，最高/最低覆盖率
  69.61%/58.01%，不在 75%～85% 门内。
- short：143 条、3 折；pinball 改善 -344.14%，覆盖率 67.57%/71.62%，折数与覆盖率均不足。
- 分位交叉为 0，说明结构约束有效；但“无交叉”不等于预测有用。

正式 `train_extrema_model` 因已验证因子为 0 返回 `insufficient_data`，模型制品仍为 0。

## 7. 当前裁决与下一门槛

本轮完成了真实 SWAP 历史候选、路径、自动因子和概率/极值的证伪链，结论是“不晋升”：

1. 历史候选数已超过 300，TP/SL 类别数也超过约 60，但仅覆盖 8 天和 10 个标的，独立
   regime/月度证据仍弱。
2. 成本后基线 EV 显著为负；当前没有 validated 因子、accepted 模型或可用极值模型。
3. bootstrap 概率劣于常数基线；提高模型复杂度没有理论依据。
4. 模拟盘自然平仓仍为 0，六维完整平仓仍为 0，有结果有效 Agent 判断仍为 0。

因此计划的代码与历史证伪阶段已经完成，但“提高开仓准确率”的统计完成定义仍未满足。后续
只允许继续 paper 采集：至少 60 笔模拟平仓、30 笔六维完整平仓、100 条有效 Agent 结果且
至少 30 条 reject，并等待更长的跨月/跨 regime SWAP 样本。成本后 EV、Brier skill、pinball、
覆盖率或 Agent 增量未转正前，模型继续 shadow，预算不扩大。

## 8. 30 天扩展复核与固定 2:1 前置门

随后将同一套研究流程扩展到 10 个标的的 30 天 1m/15m 与 90 天 1H/4H 上下文。40/40 条
序列完成，市场库收到 487,728 行、插入 355,907 个去重 bar；1m/15m 单序列覆盖率约
99.958%～100%，1H/4H 约 99.815%～100%。研究库扫描 28,203 个 15m 时点，形成 1,712
个候选，其中 1,690 个具有连续 4h/1m 路径，22 个保持 missing。

固定止损 -1R、止盈 +2R 的结果：

- TP first 462、SL first 1,110、timeout 118，毛 EV=-0.078866R。
- 10 个标的各回填 90 条资金费，共 900 条；1,712 个候选全部取得 causal as-of 资金费。
- long 884 条，毛 EV=-0.028751R、净 EV=-0.896368R。
- short 806 条，毛 EV=-0.133831R、净 EV=-1.065521R。
- 全部净 EV=-0.977041R；成本包括双边 taker、滑点和保守不利资金费，潜在资金费收入不
  用来降低开仓门。固定 `moving-block-bootstrap-v1` 种子下，多分类 Brier skill=-2.6286%，
  仍劣于常数经验基线；`swap-15m-replay-v3` 只标识数据管线，不再改变随机种子。
- replay v3 的 61 个因子试验为 reject=44、reject_missing=2、insufficient=15、validated=0。
  1,712 个候选全部取得同一收线时点的 10 标的横截面，1,703 个取得连续 5m 聚合窗口；
  BTC beta/残差动量、横截面排名、市场宽度、相关性集中度、实现波动率和 vol-of-vol 因而
  从“无法评价”转为正式受检，但 7 项全部 reject，HAR-RV 以 10.24% 缺失率触发
  `reject_missing`。这证明数据可得，不证明因子有效。剩余不足项来自真实缺失的盘口、L2
  事件、OI 和 basis；long/short 虽分别有 884/806 条标签，仍因没有 validated 特征而不训练
  正式模型。

新增的 11 个行情候选全部可由当时已收线 OHLCV/5m 波动复算：布林带宽分位、%B、
squeeze release、ADX、DMI spread、Kaufman Efficiency Ratio、VWAP/ATR 距离、VWAP
穿越率、量能 z-score、RV/HAR 比与 squeeze×volume。没有一个通过完整门。最接近的
`vwap_crossing_rate` 为 4/5 折一致、净 spread +0.0911R，但 IC t=1.4465、DSR=0，且筛选后
各分段策略 EV 仍为负；这只能保留为观察假设。`rv_to_har_ratio` 与 HAR-RV 同因早段窗口
不足产生 10.24% 缺失，触发 `reject_missing`。

扩展样本覆盖 10 个标的、2 个自然月和 high/low/mid 三类波动 regime，结论仍为
`stop_no_promotion`。因此不搜索别的盈亏比来美化结果，而是保持 2:1，并将真实 OKX 模拟盘
改成开仓前严格预测门：只有已通过样本外和独立 shadow 验证的 active 概率模型，且该候选
成本后 EV 的单侧 95% 下界大于 0 才放行。当前没有合格模型，所以正确行为是空仓；所有被拒
候选仍在 4h 后结算，用于继续挖因子、训练和校准。该机制旨在提高条件胜率，但在取得新的
样本外证据前不能宣称胜率已经提高。

## 9. 下一策略假设的优先级

策略路由顺序修订为“先判断行情，再选择候选策略，再过固定 2:1 概率门”。行情识别只输出
`trend/range/vol_expansion/disorder` 的因果概率和置信度，禁止用单一硬标签直接开仓：趋势
回踩只在 trend 候选中评价，突破策略只在 trend/vol_expansion 候选中评价，区间反转必须作为
独立候选后才能在 range 中评价，disorder 或低置信度一律空仓。路由器先保持 shadow，每个
`regime × strategy` 组合分别使用相同成本版本、purged walk-forward、Brier 与净 EV 下界裁决；
不能因为总体均值转正而掩盖某个行情分段为负。

当前适合继续验证、但不适合直接启用的组合是：15m 趋势回踩保持 1H/4H 同向，先用候选
成本排除 ATR 止损过窄的交易，再要求拒绝影线/放量与连续 top-5 L2 订单流同向，最后由严格
2:1 首触模型作 meta-label。理论上，趋势过滤对应时间序列动量，拒绝影线与 OFI 对应局部
流动性吸收，成本门解决 15m 窄止损下交易摩擦被放大的问题；但理论只决定候选，不替代本
仓库的样本外门。

现有 `B_breakout` 放量突破策略继续保持 shadow。旧 paper 库的 235 条 1H hypothetical 记录
不追溯冒充 15m 证据；schema v27 起，新 B 已携带独立 `strategy_id` 进入与 A 相同的
`signal_samples/signal_outcomes`，使用同一 -1R/+2R 和连续 4h/1m 首触结算。训练、校准与
readiness 默认只统计 A，防止 B 扩大 A 的样本门；schema v28 又把因子试验、特征选择和
模型制品按策略隔离。任一方案净 EV 下界未转正前都不获得下单权限。

## 10. A/B 公平重放与行情路由裁决

`swap-15m-replay-v4` 在同一个 30 天市场库、相同已收线时点、相同 4h/1m 首触标签和相同
逐候选成本口径下同时生成 A/B。A 仍精确为 1,712 个候选、1,690 个结果，原结果没有因接入
B 或行情标签而漂移；B 形成 1,973 个候选、1,931 个结果。`intraday-factor-oos-v5` 将
`strategy_id` 纳入试验身份，A/B 的同名因子不再相互覆盖。

- A：TP/SL/timeout=462/1,110/118，毛 EV=-0.078866R，净 EV=-0.977041R，多分类
  Brier skill=-2.6286%，61 个因子 validated=0。
- B：TP/SL/timeout=673/1,177/81，毛 EV=+0.101312R，净 EV=-0.756528R，多分类
  Brier skill=-4.3575%，61 个因子 validated=0。long 净 EV=-0.654724R，short=-0.895635R。
- B 的路由命中样本 334 条，毛 EV=+0.117491R，但净 EV=-0.461477R；未命中 1,597 条净
  EV=-0.818236R。路由有相对改善，不等于成本后正期望。
- A/B 各自只取路由命中后共 385 条，合并净 EV=-0.513970R。分层没有任何一组获得执行权。

因此“先判断行情再选策略”只保留为 shadow 元策略；它已得到独立历史证伪能力，但当前证据
只支持继续采集，不支持启用 B、调整既定 2:1、扩大模拟预算或把历史样本冒充 paper 自然
平仓。美股/公司代币可以进入每日 SWAP 候选池，但必须沿用相同流动性、连续 K 线、成本、
策略隔离和 2:1 概率门，不能因为资产类别不同绕过验证。

## 11. 96-SWAP 确认行情短窗口扩展复核（2026-08-24）

为验证当前 v5 特征身份在更广合约横截面上的方向与成本结论，对活体行情库做 SQLite 在线
一致性快照，只读 `klines_v2`，从中选择 96 个至少覆盖一天 1m/15m/1H/4H 的 USDT-SWAP。
该快照只有一个自然月且无资金费历史，因此仅有证伪权；不替代上文 90 天/10 币时间覆盖，
也不计入自然 paper 或 Harness 成熟度。

- A：2,050 候选、1,284 个连续 4h 标签；TP/SL/timeout=320/825/139，毛 EV
  -0.1053R、费用后 -0.6130R。61 个因子 validated=0；首触风险先验虽以 77.81% 精度拦下一批
  亏损，但保留组仍 -0.5377R，SL Brier skill=-4.59%。
- B：2,380 候选、1,446 标签；TP/SL/timeout=467/923/56，毛 EV +0.0177R、费用后
  -0.5127R。long 毛 EV +0.1992R，扣成本后仍 -0.3475R；61 因子 validated=0。
- 被动入场已修为只读确认 v2：A/B 信号价限价的费用后下界分别 -0.7503R/-0.7124R；
  20bp 成本回收限价下界 -0.4483R/-0.2254R。降低摩擦仍未形成正绝对期望。
- B 的多分类 Brier skill 为 +0.96%，但 SL Brier skill=-0.26%、风险先验保留组下界
  -0.7193R；不能挑单项正指标晋升。策略 C 在同快照仅 1 个开发候选且路径不完整，留出集封存。

裁决继续为 `stop_no_promotion`：模型列表保持为空，Harness 仅 shadow，预算不扩大。该扩展横截面
与 90 天时间面得到相同否决方向，加强了“当前准确率不足”的证据，但不等于证明未来不可改善。

# 历史入场预训练执行报告

> 日期：2026-08-25  
> 性质：research-only 历史重放；不计入自然模拟盘成熟度，不授予下单权限。

## 目标与执行边界

使用当前 `klines_v2` 确认行情和当前模型代码，执行完整的“15m 历史候选 → 后续 4h/1m
首触标签 → 因子门 → Logistic/CatBoost + 独立温度校准”链路。输入为只读
`data/market.db`，输出写入独立 `/tmp/crypto-agent-15m-pretrain-20260825.db`；没有读取或修改
paper/live 运行库，也没有把历史结果并入自然模拟盘统计。

执行入口：

- `tools/replay_15m_research.py`：A_pullback 与 B_breakout 同场重放。
- `tools/evaluate_15m_research.py`：逐策略执行 61 个预注册因子门和模型训练入口。
- 模型代码：Logistic champion、浅层 CatBoost challenger、purged walk-forward、独立尾部
  温度校准与成本后 EV 下界。

## 数据证据

- 行情：110 个 OKX USDT-SWAP，35,780 个可扫描 15m 时点；仅覆盖 2026-08 单月短窗口。
- 候选：A 2,323 条、B 2,699 条，共 5,022 条。
- 连续 4h/1m 标签：A 1,399 条、B 1,543 条，共 2,942 条；其余 2,080 条因分钟路径不完整
  保持 missing，没有猜测或补值。
- A long：790 条，TP/SL/timeout=`188/502/100`；A short：609 条，`161/401/47`。
- B long：972 条，TP/SL/timeout=`365/596/11`；B short：571 条，`114/410/47`。

## 训练结果

- A：61 个因子为 reject 40、reject_missing 2、insufficient_data 19、validated 0。
- B：61 个因子为 reject 30、reject_missing 2、insufficient_data 29、validated 0。
- 四个“策略×方向”都已超过 300 样本且 TP/SL 类别数超过 60，但因 validated 特征为 0，
  `train_entry_model` 均返回 `insufficient_data`；入场模型制品实际行数为 0。
- 独立温度校准没有被伪造触发：它位于特征验证之后；没有合格基础模型时不允许仅拟合一个温度参数。

## 成本后裁决

- A：毛 EV `-0.111357R`，成本后 EV `-0.607779R`。
- B：毛 EV `-0.021023R`，成本后 EV `-0.537796R`。
- B long 虽有毛 EV `+0.140638R`，成本后仍为 `-0.391611R`，不能挑选毛收益绕过费用门。
- A/B 均为 `stop_no_promotion`：positive_cost_ev=false、validated_factor=false、
  calibration_pass=false、budget_expansion_allowed=false。

## 结论

历史预训练流水线已经实际执行，不依赖下单数据；当前失败点不是样本数量，而是没有任何预注册因子
通过样本外验证，且策略整体成本后期望为负。系统正确地没有生成“已验证/已激活”模型。后续若扩大
历史覆盖，必须使用更长、跨月、含资金费的独立行情重新执行同一门，不能在本次单月结果上继续调参
直到出现正数。

## 预注册机制组合追加检验

在查看组合结果前冻结五组：趋势质量、回踩确认、突破确认、市场共振、执行成本。每组按方向使用
相同的 Logistic/CatBoost 同折挑战、purged walk-forward、独立温度校准、precision 与成本后 EV
下界；只写 research-only 结果，不生成运行制品。

- A：趋势质量、回踩确认、突破确认、市场共振的 long/short 共 8 个评价全部
  `stop_no_promotion`；执行成本组因历史盘口与预期滑点 100% 缺失而保持
  `insufficient_group_data`。
- B：趋势质量、突破确认、市场共振的 long/short 共 6 个评价全部 `stop_no_promotion`；回踩确认
  在 B 快照中不可用，执行成本组同样缺失。
- 所有可评价组的 CatBoost 都未满足替换 Logistic 的完整门；不是只比较拟合内准确率后选复杂模型。
- 最接近的绝对结果是 B long 市场共振，成本后 OOS EV 单侧 95% 下界仍为 `-0.146207R`；
  B long 突破确认为 `-0.166691R`，B long 趋势质量为 `-0.181320R`。A 各组下界更低。

裁决仍为 `stop_no_promotion`。组合检验说明“单因子不显著但联合可能有用”的假设已被当前短窗口
直接检验，但没有产生正的绝对费用后证据；不得继续在同一数据上改成员、阈值或方向直到过门。

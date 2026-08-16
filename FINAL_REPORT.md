# 加密货币交易 Agent 优化工程 — 最终交付报告

> 工作方式：两个子agent并行（外部调研员 = 10 个带 URL 提案；首席质疑官 = 10 条 CR 质疑），
> 与主agent自查（B1-B10）三方交叉验证 → 合并去重 → 按收益/风险逐轮实施 → 每轮单测验证。

## 一、实施成果总览（27 项改动，全部验证通过）

### A. 致命级修复（系统此前"必然死亡"的路径）
| 修复 | 文件 |
|---|---|
| WebSocket 断线自动重连（监督线程）+ 应用层心跳 + stale 数据过滤 + 错误日志 | realtime_okx.py |
| 波动率改由现货价格流 15 分钟滚动高低点直接计算（价格流与K线等价；OKX 公共 WS 已下线 candle 频道，实测 60018） | realtime_okx.py |
| 事件触发后状态更新（此前同一事件每 60 秒重复触发） | trading_main.py |
| 开仓幂等（同币已有持仓跳过）+ 余额/持仓数/单币敞口闸门（fail-closed） | trading_main.py / trading_daemon.py |
| RiskManager 真实接线（此前定义了从未调用）：单日亏 1.5% 停手 / 回撤 20% 熔断强平 / 飞书告警 | 3 个常驻入口 |
| 做空全链路修复（direction 字段、按方向判止损止盈、盈亏取反、复盘距离） | trade_journal.py / directional_trader.py / review_engine.py |
| 复盘→经验→阈值记录链复活（deep_review 返回值误用，此前从未真正执行） | directional_trader.py |

### B. 高优先级（策略有效性）
| 修复 | 文件 |
|---|---|
| tick 级止损（2 秒监控替代 6 小时轮询）+ 交易所侧 reduceOnly 停损单（进程崩溃也生效） | directional_trader.py |
| 套利自动平仓：费率翻转持续 16h / 基差 >0.5% → 平双腿；台账持久化 | trading_main.py |
| 净年化闸门：毛年化扣往返费+14天摊销，毛 8% → 净 0.18% 拒开（负期望不碰） | scoring.py / config.py |
| squeeze trap 过滤：|年化|≥80% 评分 100→30（非单调化） | scoring.py |
| 对冲杠杆 1x（3x/2x 只抬爆仓风险） | trading_main.py / funding_arb.py |
| 负费率方向分叉（现货空腿需保证金账户→仅合约腿+告警） | trading_daemon.py / funding_arb.py |

### C. 执行正确性
| 修复 | 文件 |
|---|---|
| round(x, float) TypeError（ccxt precision 是 float）→ 统一 precision_decimals | execution.py（新建） |
| max(700,100) 恒 700 → 统一口径 + 最小下单量校验 | trading_agent.py / trading_daemon.py |
| 合约腿补 posSide | funding_arb.py |

### D. 自进化体系修复（五层各自的验证门）
| 层 | 验证门 | 状态 |
|---|---|---|
| 策略 | 严谨回测（无未来函数） | 已有 |
| **权重** | **WeightLearner + EvolutionGate 影子验证（新）** | ✅ 数据闭环接通 |
| 经验 | ±10 对称、40/60 分离、30 天衰减、60 天复活、两面化评分 | ✅ |
| 阈值 | 每桶≥8样本、连续2桶非负、[60,90]夹逼、方向闸、历史裁剪500 | ✅ |
| 因子 | 真 GA 算子 + 因果归一化 + walk-forward（≥2折+同号≥80%） | ✅ |

### E. 元优化层（"优化自进化的优化策略"）
- **evolution_gate.py（新建）**：统一进化纪律——影子验证（候选只记录不执行）→ 样本外超越现役才 promote → 上线观察期退化 rollback → 事件全落盘可审计。
- **threshold_learning 方向闸**：放松阈值必须由新放行段的正期望支撑。
- **weight_learning.py（新建）**：权重层从静态拍脑袋 → 数据闭环（套利平仓净盈亏喂入），demo 学会剔除噪声因子。

## 二、诚实的验证结论

1. **因子 walk-forward 实测**：v1 报告"遗传因子 IC 0.12-0.18"确认为**测试集选择的幸存者值**。修复 GA 空操作算子 + walk-forward 验证后，15 个因子**仅 1 个通过**（OOS 中位 IC +0.105，2 折同号），其余训练 IC 0.24 的因子样本外归零。通过者存 factor_top.json。
2. **净年化核算**：150 USDT 档的费率套利扣除 0.3% 往返成本后，毛年化 8% → 净 0.18%，低于 2% 下限自动拒开。费率套利是"慢策略"（≥14 天持有），日内短线靠方向性仓位。
3. **单测覆盖**：4 轮共 40+ 项离线单测（GA 算子、净年化闸门、做空盈亏、级联检测、验证门 promote/reject/rollback、阈值方向闸、经验衰减/复活等）+ 14 模块导入冒烟 + 真实数据干跑。

## 三、剩余中优先级提案（建议先回测再接入）
| 提案 | 内容 | 前置条件 |
|---|---|---|
| OP-4 | 量加权订单流 + books5 + CVD | 先回测 taker imbalance 的 5/15min IC |
| OP-5 | HAR 已实现波动率预测 | BTC/ETH 样本外验证 |
| OP-8 | 费率截面因子（percentile） | 扩币数到 20-30（当前 5 币分位无意义） |

## 四、部署运行手册
1. 本地常驻：`cd crypto-agent && python3 trading_main.py`（事件驱动主进程，7×24）
2. 方向性（日内短线）：`python3 directional_trader.py`（2 秒止损监控 + 6 小时信号扫描）
3. 观察指标：飞书告警（熔断/开仓/平仓/级联）、threshold_state.json、weight_state.json、evolution_gate 事件
4. 上线前最后一步：模拟盘连续 24h 观察（确认无仓位叠加、熔断触发正常、费率套利只开净年化达标仓）
5. 真实盘前必须做的：真盘深度重算滑点分档（config SLIPPAGE_*）、确认 XRP TradFi 免责声明已接受、资金费率套利用保证金账户（负费率腿）

## 五、哲学层面的收获（对应"宁可做对，也不做错"）
- 大多数"进化"提议应该被验证门**拒绝**——本轮 walk-forward 15 个候选因子刷掉 14 个就是证明。
- 风控闸门全部 fail-closed：查询异常、余额不足、熔断、幂等冲突，一律拒绝下单。
- 小仓位（150 USDT）+ 慢决策（事件驱动 + 30min 冷却）不变，但每次下单前有 6 道独立闸门。

# 四子agent进化循环 — 轮次报告

> 协调者：Master。四个子agent：A 调研（外部情报）/ B 设计（优化方案）/ C 质疑（方案审查）/ D 执行（代码优化大师）。
> 循环：A→B→C(裁定)→D(实施)→协调者复核→轮次报告，直到收敛（无值得实施的优化项）或 10 轮保险丝。
> 纪律：防过拟合（任何调参须样本外/影子验证）、诚实（区分样本内/外）、风控 fail-closed。

## 子agent 登记
- A 调研子agent: 16581a57-99c6-4a3c-b7a8-38e160e7cffa
- B 设计子agent: 890cd8cb-0188-4f18-bcd7-bb094427da56
- C 质疑子agent: 7e2f3f76-c70e-49de-83e6-7858572ccb9c
- D 执行子agent: cba9a675-55f9-43c1-a7fc-562bc61450b1

---

## 第 1 轮（进行中）

### A 调研产出
- ✅ R1 交付：`research_report_round2.md`，8 条新提案（NEW-1~8），Top3 = NEW-3/NEW-1/NEW-7。已转发 B 做第二批设计。
- ✅ R2 交付：`research_report_round3.md`，5 条（R3-1 attachAlgoOrds TP/SL 一体+OCO【高】、R3-2 OKX 子账户隔离【高，RES-3 根治方案】、R3-3 进程 watchdog+heartbeat【高】、R3-4 funding 结算时间对齐【中，补 R1-4】、R3-5 NautilusTrader 订单状态机【中】）。供 B 下一批设计（R2 轮）。

### C 预审产出
- ✅ 已交付：20 条残余问题（RES-1~RES-20）。致命 3 条：RES-1 套利平仓现货腿方向硬编码（rate<0 仓平仓时卖空翻倍）、RES-2 套利仓无交易所侧止损、RES-3 跨进程共享账户（杠杆互覆+持仓合并致误平）。高 7 条：RES-4 幽灵止损单/RES-5 阈值死代码/RES-6 回滚不生效/RES-7 经验采纳未追踪/RES-9 权重label同源自证/RES-10 幂等被穿透（journal 3个同日同价ETH残留）。中 10 条。已转发 B：首批设计必须覆盖 7 条致命+高，中等级选 ≤3 条。

### B 设计方案 + C 裁定
- B 消息队列：① 交付现有设计（中断后）→ ② 第二批设计（NEW-1~8 选≤3）→ ③ C 预审 20 条（须覆盖 7 条致命+高）
- B 当前状态：运行中（处理上述队列）
- 协调者预确认证据：
  ① directional_trader.py:200-205 挂 conditional 停损单但全项目无 cancel 调用（幽灵止损单）
  ② threshold_learner.record 仅 directional_trader.py:282，trading_main._close_hedge 未喂阈值
  ③ 杠杆三处冲突：trading_main.py:36 套利 1x vs directional_trader.py:32 方向 2-3x 同账户互相覆盖；
     trading_daemon.py:28 套利仍 2-3x（上轮 OP-3 降杠杆改漏此文件）
（待 B 产出 → C 裁定）

### 协调者独立复核（对 C 的致命指控）
- ✅ RES-1 实锤：trading_main.py:343 `_close_hedge` 现货腿恒 `create_market_sell_order`；rate<0 仓（现货空+合约多）平仓时再卖现货 → 空头翻倍（账户持有现货库存时该路径可真实触发）
- ✅ RES-6 实锤：evolution_gate._rollback 仅改 gate 内部状态；weight_learning.record→record_incumbent→_observe→_rollback 链路不回写 WeightLearner.weights
- ✅ RES-10 实锤：trade_journal.json 实查 3 笔 open（txn_001~003，ETH entry≈1885.5，间隔 45s/30min，无 direction 字段）——幂等只信 journal、未与交易所持仓对账的现场证据
- ✅ RES-5/15 与协调者此前预确认一致
- ⚠️ RES-13 修正：calendar 事件日期覆盖 2026-08~12，当前（2026-08-16）仍有效（CPI 8-19 临近）——"过期静默失效"是 2026-12 之后的前瞻风险，非当前活跃故障；fail-safe+更新机制仍有价值但非紧急

### D 实施 + 协调者复核
（待）

### 协调者接口预核验（支持 C 裁定）
- ✅ R1-1 取消链成立（ccxt okx.py 源码实锤）：`options['algoOrderTypes']` 含 'conditional' → `fetch_open_orders(sym, {"ordType":"conditional"})` 路由到 `privateGetTradeOrdersAlgoPending`；`cancel_orders(ids, sym, {"trigger": True})` 路由到 `privatePostTradeCancelAlgos`（按 algoId 取消）。B 对 `cancelAllOrders=False` 的判断也属实（okx.py:59 附近）。
- ✅ R1-4 接口依赖成立：OKX `has['fetchLedger']=True`（okx.py:119）；market 单返回 `avgPx` 被 ccxt 解析为 `order['average']`（okx.py:4000）。
- ✅ R1-5 前置条件已核：grep 确认无 cron/LaunchAgents/LaunchDaemons/*.sh/*.plist 调度引用 trading_daemon → 方案 A（mv .legacy）安全。
- ✅ R1-4 账单接口已核：ccxt okx fetch_ledger 将 params 透传给 OKX bills 端点（type 参数可传数字编码），C 的"type=8=funding"判断与 OKX bills 数字编码惯例一致；D 实现按 C 要求做"全量拉取+本地过滤"兜底即可，无论编码差异。

### 收敛台账 / 待办 backlog（收敛判定依据）
| # | 待办 | 状态 | 证据 |
|---|---|---|---|
| 1 | 幽灵止损单清理（平仓/强平后取消 conditional 单） | 已确认→B 设计中 | directional_trader.py:200-205，全项目无 cancel 调用 |
| 2 | 套利平仓喂 threshold_learner（台账存综合分快照） | 已确认→B 设计中 | threshold_learner.record 仅 directional_trader.py:282 |
| 3 | 杠杆三处冲突统一（套利全 1x；方向开仓前重设） | 已确认→B 设计中 | trading_main.py:36=1x / directional_trader.py:32=2-3x / trading_daemon.py:28=2-3x |
| 4 | 方向性止盈无交易所侧单（TP 仅本地 2 秒轮询，进程崩溃则 TP 失效） | 已确认待立项 | directional_trader.py 仅挂 conditional 止损单 |
| 5 | B8：经验验证未记录"本笔实际采纳哪些经验"（trusted 全量 validate=回声） | 待 C 预审确认 | directional_trader.py monitor 平仓段 |
| 6 | calendar 手写 6 事件过期静默失效 | 待 C 预审确认 | economic_calendar.py |
| 7 | factor_top.json（walk-forward 通过的 1 因子）未接入决策，需 paper 前瞻 90 天 | 待立项 | factor_top.json 存在但无消费方 |
| 8 | OP-4/5/8（订单流IC/HAR/费率截面）——先回测验证再立项 | 标记：待验证 | optimization_report.md |

### 轮次小结
✅ 第 1-3 轮循环完整收官：
- A 调研 3 轮 23 条提案（R3 显式收敛"无新方向"）
- B 设计 2 轮 18 条方案 → C 裁定 4 轮 → 修订 3 轮 → 定稿
- 实施 18 项 + 3 项驳回 + 3 份文档（subaccount_test_plan / tp_sandbox_verify / watchdog_launchd）
- 全量验证：16 文件语法 ✅ + 16 模块导入 ✅ + 30+ 单测 ✅
- 收敛达成，详见 CONVERGENCE_REPORT.md。遗留仅用户执行项：子账户沙盘实测、TP 沙盘验证。

## 协调者裁定记录（流程性错位修复）
- R2 终审出现与 R1 相同的流程性错位：B 修订在消息正文、C 只核验落盘旧文件 → 协调者对照双方文本逐条裁定：**B 修订正文已满足 C 全部 6 条最终要求** → 定稿 `optimization_plan_agentB_R2_FINAL.md`（权威实施稿）。R2-6 通过；R2-1/2/3/4 接受；R2-5 接受但实施前置=沙盘验证（代码默认关闭 FLAG_ENABLE_EXCHANGE_TP）。

---

## 第 2 轮预排（R1 实施完成后启动）

**B 设计输入（待办 backlog）**：
- 高：RES-6 EvolutionGate 回滚回写 WeightLearner.weights（已实锤）
- 高：RES-7 经验采纳追踪（开仓记录 adopted lesson ids，平仓只 validate 实际采纳）
- 高：RES-10 journal↔交易所持仓对账 + 幽灵 open 记录回收（现场 3 笔 txn_001~003）
- 高：R3-1 attachAlgoOrds 挂 TP/SL 一体+OCO（补止盈交易所侧；与 R1-1 畸型单沙盘验证联动）
- 高：R3-3 进程 watchdog + heartbeat + 崩溃自动重启
- 中：RES-15 trading_main.execute 复用 execution.qty_for_notional（口径统一）
- 中：RES-16 WeightLearner 候选生成与 IC 评估分训练/验证段
- 中：RES-17 deep_review 补 atr/signal_price 参数
- 中：R3-4 funding 结算时间对齐（补 R1-4）
- 中：R3-5 订单状态机（与 R3-1 合并评估）
- 前置：R1-13 OKX 子账户沙盘实测（D 交付测试文档后执行）→ 决定 R3-2 子账户隔离是否可行
- 遗留（B 已标放弃，R2 复核是否复活）：RES-8 衰减 last_update、RES-11 周期矛盾、RES-12 止盈交易所侧（已被 R3-1 覆盖）、RES-13 calendar fail-safe、RES-14 gate 双写一致性、RES-18 告警 stale、RES-19 evolver journal 副本、RES-20 基差 REST 兜底

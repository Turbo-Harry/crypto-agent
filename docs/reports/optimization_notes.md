# 优化审计笔记（主agent自查，待与子agent报告合并）

> 用途：主agent在等待调研员/质疑官报告期间自查发现的候选问题。
> 合并后按收益/风险排序实施。每个条目：证据 → 危害 → 修法。

## 自查发现（代码审计）

### ✅ B1. realtime_okx.py — WebSocket 无自动重连【已实施 R1】
- 证据：`start()` 用 `run_forever` 无 `reconnect` 逻辑；`_on_close` 只把 `_running=False`，线程退出后不再订阅。
- 危害：任何断线（OKX 24h 强断/网络抖动）→ 实时数据永久冻结 → 决策系统瞎/停。
- 修法：`_on_close` 里延迟重连（指数退避 max 60s），或外层监督线程检测 `_running==False` 重启。重连后 `_on_open` 自动重订阅（现有订阅逻辑可复用）。加最后数据时间戳 `last_msg_ts`，主循环检测 stale > 90s 视为死链并强制重连。

### ✅ B2. realtime_okx.py — vol_15m 重启/断线后归零 + 重复K线【已实施 R1】
- 证据：`candles_1m` 只维护最近15根内存态；断线重连后从0重新积累；candle1m 对进行中K线会推送多条同 ts 数据，`append` 未去重。
- 危害：重连后 5-15 分钟内 vol_15m 失真（偏小→分数40"无动能"），可能错误放行或拦截决策。
- 修法：按 `ts` 去重（新 ts 才 append，同 ts 更新最后一条）；冷启动时用 REST 拉最近15根1m K线预热（ccxt `fetch_ohlcv(limit=15)`），避免窗口期失真。

### ✅ B3. trading_main.py — check_signal_event 事件触发时状态不更新 → 重复触发【已实施 R1】
- 证据：`check_signal_event` 三个 return 事件分支都提前 return，`self.signal_state[base]` 只在无事件时更新。
- 危害：价格异动后 prev 停留在旧值，下一分钟再比较旧值→新值可能再次触发同一事件（事件风暴）。
- 修法：任何分支都先更新 signal_state 再 return 事件名。

### ✅ B4. trading_main.py — execute() 无持仓/余额/仓位上限检查【已实施 R1】
- 证据：`execute` 直接下单，不查现有持仓数量、USDT 余额、单币名义敞口上限。
- 危害：曾真实发生"保证金不足(51008)、USDT 耗尽"事故；重复信号可在同一币无限堆积仓位，爆仓路径敞口无上限。
- 修法：下单前查 `fetch_balance`（USDT free ≥ 2×150 才开）+ 单币名义敞口上限（如 ≤600 USDT/币、总敞口 ≤2000）+ `fetch_positions` 计数上限（MAX_HOLDINGS=4）。任何一项超限 → 拒绝并告警，绝不静默继续。

### ✅ B5. directional_trader.py — monitor() 空头出场逻辑完全错误【已实施 R1】
- 证据：`if price <= t["stop_loss"] and t["take_profit"] > t["entry_price"]` 与 `elif price >= t["take_profit"]` 都按多头写；空头 `tp < entry`，`price >= tp` 几乎永远为真 → 开空后立刻被"止盈"平掉，或止损永不触发。
- 危害：所有空头仓位管理失效，方向做错也无人管。
- 修法：journal 里存 `direction` 字段，按方向写 stop/tp 判断：多头 `price<=stop or price>=tp`；空头 `price>=stop or price<=tp`。同时修复 log_entry 记录方向。

### B6. directional_trader.py — 两个经验库并存（ScoredExperience vs ExperienceBank）
- 证据：`directional_trader` 用 `self.exp_bank = ScoredExperience()`，但 `self.evolver.decide` 内部用 `ExperienceBank`（review_engine 的旧库）；`decide` 返回的 `stop_adj`/`size_factor` 无人消费（死代码）。
- 危害：两套经验体系各记各的，自进化闭环是断的；"放宽止损 +0.2 ATR"从未生效。
- 修法：统一到 ScoredExperience；decide 的 stop_adj/size_factor 在 open_position 实际应用（stop = entry - (1+stop_adj)*ATR，qty *= size_factor）。

### B7. threshold_learning.py — calibrate 的统计缺陷（高）
- 证据：① 取"第一个 avg_pnl>=0 的桶"为盈亏平衡，桶均值噪声大（1-2 样本）→ 阈值可能被单个幸运桶拉低到 40-60；② 无下限 clamp（只有 min(90, ...)）；③ 决策记录无限增长无衰减，老 regime 的样本永久占权重。
- 危害：阈值被噪声拉低 → 系统放行大量实际亏损的分数段 → 亏钱循环。
- 修法：① 要求桶样本数 ≥ min_bucket_samples(如8) 且单调性检查（从高到低 avg_pnl 应不降，否则不采纳）；② clamp 到 [65, 85]；③ 记录只保留最近 N=500 条或时间衰减权重；④ 只在决策总分分布覆盖的分数段内插值。

### B8. experience_scoring.py — trusted 经验被每笔无关交易的结果验证（中高）
- 证据：directional_trader.monitor 对每笔平仓，把**所有** trusted 经验 validate(该笔 pnl)。经验是否被该笔交易实际采纳没有被记录。
- 危害：经验分数追踪的是"系统整体表现"而非"该经验本身对错"，好经验可能被无关亏损误杀、坏经验被无关盈利洗白。
- 修法：开仓时记录 adopted_lesson_ids 到 trade journal；平仓只 validate 本笔实际采纳的经验。未采纳的经验不参与验证。

### B9. trading_main.py — 费率套利开仓漏了 posSide / 现货空腿不可行
- 证据：`execute` 的 `rate>0` 分支合约空用了 posSide，但 `rate<0` 分支现货卖空没有借币/保证金逻辑；`funding_arb.py open_hedge` 合约腿完全没传 posSide。
- 危害：负费率方向（现货空+合约多）在现货账户不可执行（除非 margin 模式），实际会报错或卖出现有持仓；funding_arb CLI 开仓 51000 报错。
- 修法：funding_arb.py 补 `params={"posSide": "short"}`；trading_main 负费率分支改为仅合约多+检查现货余额，无货则告警跳过并说明需要 margin 账户。

### B10. 目标数学校验（5% 单笔 + 2:1 盈亏比）
- 计算：2:1 RR 下，单笔 +5%/-2.5%（5% 与 2:1 隐含止损 -2.5%）。手续费+滑点单边约 0.1-0.15%，两腿 ≈0.3% 成本 → 实际 RR ≈ (5-0.3):(2.5+0.3)=4.7:2.8≈1.68:1。盈亏平衡胜率 = 2.8/(4.7+2.8) ≈ 37%。含杠杆 3x 时合约腿手续费 3 倍计入保证金 → 略差。结论：目标可达到但需要 ≥40% 胜率，历史上技术策略回测胜率不达标 → 靠资金费率套利（delta 中性不靠胜率）保底 + 方向仓小仓位。
- 行动：在 config 增加 FEE_BPS 真实核算，持仓收益计算按净额；方向仓仓位公式已含手续费预算。

## 待办（等子agent报告后合并）
- 调研员报告 → 外部方案（预计：HAR/GARCH 波动率预测、订单流不平衡、清算级联、freqtrade 风控模块、walk-forward 验证）
- 质疑官报告 → 更多攻击面
- 合并去重 → 优先级排序 → 逐条实施 → 验证（py_compile + 冒烟测试 + 有数据时 mini backtest）

---

# 合并实施记录（子agent报告 × 主agent自查 交叉验证后）

## R1 已实施（验证：语法12文件 ✅ + 离线单测13项 ✅ + 导入冒烟 ✅）

| 编号 | 内容 | 对应子agent条目 |
|---|---|---|
| ✅ | realtime_okx：监督线程自动重连(30s检查/120s僵死)、K线按ts去重、REST预热15根、应用层ping 20s、字段级ts + get(max_age) stale过滤、错误计数日志 | CR-5 / OP-2 |
| ✅ | trading_main：check_signal_event 先更新状态防重复触发；_risk_guard（熔断+幂等+余额300+持仓≤4+单币≤600，fail-closed）；净年化闸门（净<2%拒开）；gather_signals 用 max_age=60 + vol None | CR-1 / CR-4 |
| ✅ | RiskManager 接线 trading_main / directional_trader / trading_daemon（净值喂入+熔断+恢复通知） | CR-2 / OP-1 |
| ✅ | trade_journal：direction 字段、score 字段、空头盈亏取反；directional_trader monitor 按方向判止盈止损；deep_review 空头距离修正；record 用真实分80、决策用自适应阈值 | CR-3 / CR-6 |
| ✅ | threshold_learning：阈值夹逼[60,90]、每桶≥8样本、连续2桶非负才认、历史上限500条 | CR-6 / B7 |
| ✅ | execution.py 新建：precision_decimals + qty_for_notional（修 round(x,float) TypeError + 最小下单量校验） | CR-9 / OP-9 |
| ✅ | trading_daemon/trading_agent/funding_arb：数量换算统一 execution；负费率方向分叉（现货空腿无保证金账户→仅合约腿+告警）；funding_arb 补 posSide；trading_agent 修恒700 | CR-4 / CR-9 / B9 |
| ✅ | scoring：net_funding_annual（往返0.3%按14天摊销）；score_volatility(None)=45 | CR-4 / CR-5 |
| ✅ | config：ARB_ROUNDTRIP_COST=0.003 / ARB_MIN_HOLD_DAYS=14 / ARB_MIN_NET_ANNUAL=0.02 | CR-4 |

## R2 待实施（下轮优先级）

1. ~~**OP-1 剩余**：directional_trader 止损从"6小时轮询"→ WebSocket tick级监控线程 + 交易所侧 reduceOnly 停损单（进程崩溃也有止损）~~ **✅ 已实施本回合**：WS 接入 + 2秒 monitor + 交易所侧条件停损单 + 熔断强平（只平本策略持仓，不动套利对冲仓）+ 开仓幂等/余额守卫。单测发现并修复存量 bug：monitor 里 deep_review 返回值误用（复盘→经验→阈值记录链此前从未真正执行）。
2. ~~**OP-3**：套利自动平仓（费率连续2周期翻转→平对冲仓）、基差跟踪（perp−spot）、score_funding_rate 非单调化（|年化|>80% 降分=squeeze trap）、对冲杠杆降 1x~~ **✅ 已实施本回合**：arb_positions.json 台账 + manage_arb_positions（基差>0.5% 平仓 / 翻转持续16h 平仓 / 仓位消失清理）+ WS 双 ticker 订阅算基差 + 费率评分非单调 + 所有套利入口杠杆 1x。7 项单测通过。
3. ~~**OP-7 / CR-10**：factor_evolution 修复 GA 空操作算子、全样本标准化泄漏、walk-forward 多折验证~~ **✅ 已实施**：真子树交叉（20/20 改结构 vs v1 的 0/20）、真变异（14/20）、vol 因果扩张均值、walk-forward 3 折 + 提升标准（≥2折+同号≥80%+中位|OOS IC|≥0.03）。**诚实结果：15 因子仅 1 个通过**（OOS 中位 IC +0.105，2折）；v1"0.12-0.18"确认为测试集选择的幸存者值。通过者存 factor_top.json。
4. **CR-7**：~~score_oi 空头分支、calendar 降权 0.10~~ **✅ 已实施**；权重 IC 化校准 + 因子相关性去冗余 待 R3 数据回测。
5. **CR-8**：~~经验评分对称化(±10)、discard 复活(60天)、时间衰减(30天半衰期)、决策分两面化~~ **✅ 已实施并单测通过**；B6（两套经验库统一、stop_adj/size_factor 生效）待 R3。
6. **OP-6**：清算级联代理指标（ΔOI+价格+费率共振）加入事件检测 → R3
7. **验证项**：模拟盘 24h 连续跑（观察不叠加）、人为制造连亏验证熔断触发 → R3

## ✅ 元优化层（用户新要求："优化自进化的优化策略"）
- **evolution_gate.py 新建**：EvolutionGate 影子验证门——候选只影子记录不执行 → ≥N样本且超越现役才 promote → 上线观察期退化即 rollback → 事件全落盘可审计。单测 promote/reject/rollback 全过。
- **threshold_learning 方向闸**：放松阈值必须由新放行段 [new,old) 正期望支撑，否则拒绝。单测：噪声放松被拒 / 真放松放行。
- 五层进化验证门现状：策略(回测) / 权重(WeightLearner+EvolutionGate ✅R3) / 经验(±10+衰减+复活) / 阈值(桶统计+方向闸+夹逼) / 因子(walk-forward 门)。进化规则可评估可替换=元目标。

## ✅ R3 已实施
- **weight_learning.py 新建 + trading_main 接线**：权重层数据闭环——套利台账记录子分数+入场费率，平仓时净盈亏（费率收入−往返成本）喂给 WeightLearner；候选权重（剔除贡献为负的因子）经 EvolutionGate 以分数-盈亏 IC 为门指标验证。demo 学会剔除噪声因子（b→0, a→1.0）并通过验证门。decide() 改用 wl.weights。
- **OP-6 清算级联检测**：run 循环每分钟采样 Gate.io OI（10分钟窗口），check_signal_event 新增级联事件（价格≥3% + OI≤-2% 共振 → "暂停追单"），可覆盖普通异动事件、不重复触发。
- **B6 经验库统一**：_ExpAdapter 让 evolver.decide 走 ScoredExperience（此前两套库并存）；stop_adj/size_factor 真实生效（止损±0.2ATR、连亏半仓）。
- 12 项单测 + 14 模块导入冒烟全过。

## 待办（R4 收尾）
- 最终集成验证（模拟盘 --once 观察不叠加/不开负期望仓）
- 剩余中优先级提案评估（OP-4 量加权订单流 / OP-5 HAR 波动率 / OP-8 费率截面因子）——建议先回测验证再接入
- 部署建议（本地 nohup 常驻 vs 腾讯云服务器）

## ✅ R4 已完成（收尾验证）
- **真实数据干跑（未下任何单）**：价格/费率/恐惧贪婪/OI/评分/阈值/风控闸门全链路正常；综合分 44-57 全部"观望"（当前市场无达标机会，符合"宁缺毋滥"）；幂等闸门正确识别真实持仓（BTC 1 个/ETH 2 个）。
- **干跑抓出并修复 4 个真实环境 bug**：
  1. OKX 公共 WS 已下线全部 candle 频道（实测 candle1m/1D、现货/合约 instId 均 60018；tickers 对照成功）
  2. OKX 心跳是纯文本 "ping"（JSON {"op":"ping"} 报 60012）→ 改发 "ping"，收到 "pong" 忽略
  3. REST 预热未打 vol_ts → 被 stale 过滤器误杀 → 已补时间戳
  4. **用户洞察：价格流与 K 线等价** → 波动率改由现货价格流 15 分钟滚动高低点直接计算（deque 窗口，≥5 分钟跨度才更新，>900s 样本修剪），砍掉 candle 订阅和 REST 轮询线程；REST 仅在冷启动预热一次。单测+实盘复验通过。
- 清理全部测试遗留状态文件（保留 factor_top.json 等真实产出）
- docs/reports/final_report.md 交付报告完成

## ✅ R1 定稿方案（Agent B/C 收敛）— D 实施记录（批次1）

> 批次1 范围：仅 R1-10（协调者限定）。R1-5 / R1-12最小止血 / R1-1 下批次实施；R1-2/3/4/6/11/12账本 下批次。

### ✅ R1-10 套利现货腿平仓方向（RES-1）
- **改动文件**：`trading_main.py`（execute / _close_hedge）、新增 `test_r1_10_close_hedge.py`
- **内容**：
  1. execute() 台账新增 `spot_side` 字段：rate>0→"long"、rate<0→"short"（平仓反向用）。
  2. execute() 下单 try 内加 `spot_ok`/`perp_ok` 标记，任一腿失败 → 反手平已成交腿（孤儿补偿），告警后 return False。
  3. _close_hedge() 现货腿改为按 `spot_side` 反向平（long→卖、short→买）；旧台账无该字段按 `entry_sign` 兜底推导；显式 `spot_side=None`（单腿）跳过现货腿；平仓前对账 `min(amount, abs(held))`，方向相反/为 0 不硬平。
- **验证**：
  - `py_compile trading_main.py` ✅
  - `test_r1_10_close_hedge.py` 3 项离线单测 ✅（rate<0→现货买回补 / entry_sign 兜底 / spot_side=None 跳过）
  - `import trading_main` 冒烟 ✅（仅 urllib3 OpenSSL 无害告警）
- **遗留风险**：
  1. 对账用 `fetch_balance()`（默认 swap 账户）读现货持有量，未显式传 `type="spot"`——OKX 统一账户下可读但语义不严格；现货空腿（rate<0）在现金账户本不可开（R1-11 负费率整体拒绝后 moot）。
  2. 孤儿补偿用整 `amount` 反手平，未按实际成交回填（市价单近似）；上线前沙盘验证。
  3. 本批未实施 R1-10 之外的其余项（R1-5/12/1/2/3/4/6/11/12账本）。

## R1 定稿方案实施记录（协调者兜底部分 — D 批次1停滞期间由协调者实施并单测）
- ✅ R1-5：trading_daemon.py → trading_daemon.py.legacy（调度引用已核无，mv 安全）
- ✅ R1-12 最小止血：directional_trader monitor/_liquidate_all 平仓量改 t["size"] + reduceOnly=True（不再全额平合并持仓）
- ✅ R1-1 幽灵止损单清理：_cancel_stop_orders（原生 orders-algo-pending 全量取 + cancel_orders trigger=True）+ 三处挂接（monitor 平仓后/_liquidate_all 强平后/open_position 开仓前）+ 畸形单沙盘验证标注
- 验证：py_compile 3 文件 + 8 项离线单测全过（含 D 的 test_r1_10_close_hedge.py）

## R1-2 实施记录（协调者兜底）
- ✅ execute() 台账加 composite_score + weights_version 快照；run_once/run 两处补传 total（修 run_once 漏传 scores）。
- ✅ _close_hedge() 平仓后有快照才喂 threshold_learner.record（旧台账无快照直接跳过，不重算）；weight_learner 照喂。
- 验证：py_compile + 2 项离线单测（有快照 record 1 次取快照值 / 无快照跳过 threshold、weight 照喂）全过。

## R1 定稿方案 D 实施记录（批次2）

> 批次2 范围：协调者收窄为仅 R1-2。其余 R1-3/4/6/11/12账本 后续分批。

### ✅ R1-2 套利平仓喂阈值学习（D 复核 + 落盘单测）
- **改动文件**：`trading_main.py`（execute / run_once / run / _close_hedge）、新增 `test_r1_2_threshold_feed.py`
- **内容**（与协调者兜底一致，D 逐行复核无冲突）：
  1. execute() 签名 `execute(self, base, sig, scores=None, composite_score=None)`；台账 append 加 `composite_score` + `weights_version` 快照。
  2. run_once() / run() 两处 `execute(..., scores=scores, composite_score=total)`（修 run_once 漏传 scores）。
  3. _close_hedge() `if ok:` 块内、weight_learner 之后加：`score = rec.get("composite_score")`；`if score is not None: threshold_learner.record(float(score), float(net_pnl))`（旧台账无快照直接跳过，不重算不打标）。
- **验证（D 复核实测）**：
  - `py_compile trading_main.py` ✅
  - `test_r1_2_threshold_feed.py` 2 项 ✅（有快照 → threshold.record 1 次且取快照值 75.0 + pnl 为 float / 无快照 → threshold 不 record、weight 照喂 1 次）
  - `import trading_main` 冒烟 ✅（仅 urllib3 OpenSSL 无害告警）
- **遗留风险**：net_pnl 仍为估算值（`abs(entry_rate)*3*days - 0.003`），真实盈亏待 R1-4 落地；旧台账（无 composite_score）不参与阈值学习，属预期（不重算）。

### ✅ R1-3 状态文件拆分 + 原子写（D 复核 + 落盘单测）
- **改动文件**：`threshold_learning.py`（_save 原子写）、`weight_learning.py`（_save 原子写）、`directional_trader.py`（learner path + 注释）、`trading_main.py`（learner path）；新增 `test_r1_3_atomic_write.py`
- **内容**（代码已由协调者写入，D 逐行复核无冲突）：
  1. 方向侧 `ThresholdLearner(path="threshold_state_dir.json")`；套利侧 `ThresholdLearner(path="threshold_state_arb.json")`——两进程不再共用 `threshold_state.json` 互相覆盖。
  2. `threshold_learning._save()` / `weight_learning._save()` 改原子写：写 `path+".tmp"` → `os.replace(tmp, path)`（崩溃不留半截 JSON）。
  3. 方向侧注释写明：方向信号分恒 SIGNAL_SCORE=80 单点 → calibrate 单桶 no-op → 阈值保持初始 70 固定，自适应由套利侧负责。
- **验证（D 复核实测）**：
  - `py_compile threshold_learning.py weight_learning.py directional_trader.py trading_main.py` ✅（4 文件）
  - `test_r1_3_atomic_write.py` 3 项 ✅（threshold 原子写无 .tmp / weight 原子写无 .tmp / 两 learner 路径互异）
  - `import threshold_learning, weight_learning, directional_trader, trading_main` 冒烟 ✅
- **遗留风险**：
  1. 简报 R1-3 的「跨进程锁（threshold_state.lock）」本批未实施（协调者单点指令只含拆分+原子写+注释 3 项）。因状态文件已拆分、每文件仅单一写进程，锁为防御性兜底，待后续如需补。
  2. 旧 `threshold_state.json`（未拆前的共享文件）不再被读取，属废弃文件，可后续清理。

## R1 定稿方案实施记录（R1-3/4/6/11/12 账本 — 协调者兜底，D 复核模式）
- ✅ R1-3 状态文件拆分+原子写：threshold_state_arb/dir.json 拆分；_save 原子写（.tmp→os.replace）；方向阈值恒70注释。D 复核+单测 test_r1_3_atomic_write.py。
- ✅ R1-4 真实已实现盈亏：_fill_price（fetch_order 回填）、_fetch_funding_received（bills type="8"）、spot/perp/funding/fees 真实核算、pnl_estimated 打标；threshold/weight learner 支持估算样本跳过校准/贡献评估。3 项单测。
- ✅ R1-6 杠杆幂等收窄：同 symbol 同 posSide 才拒（opposite side 放行）。
- ✅ R1-11 禁裸单腿：funding_arb 负费率整体拒绝。
- ✅ R1-12 所有权账本：position_ownership.py（claim/release/总敞口600/flock+原子写）；directional_trader 开仓 claim、失败回滚、平仓/强平 release。3 项单测。
- ✅ R1-13 子账户测试文档 docs/ops/subaccount_test_plan.md。
- 全量验证：15 文件语法 + 全量导入冒烟 + 11 项落盘确认 ✅

## 单写者政策（D 提议采纳）
- 协调者兜底写入与 D 并行曾产生文件写竞争 → 政策：同一文件同一时段只允许一个写作者（协调者或 D），另一方只复核/验证。

## R2 实施记录（D — 批次：R2-6 + R2-1）

### ✅ R2-6 止损复盘参数（atr_value/signal_price 接线）【接受·零风险】
- **改动文件**：`trade_journal.py`（log_entry 加字段）、`directional_trader.py`（open_position / monitor 传参）；新增 `test_r2_6_stop_tight.py`
- **内容**：
  1. `trade_journal.log_entry` 加 `atr_value=None, signal_price=None` 字段并落盘。
  2. `directional_trader.open_position` 的 log_entry 调用传 `signal_price=sig["entry"], atr_value=sig["atr"]`。
  3. `monitor` 平仓后改为 `deep_review(closed, atr_value=t.get("atr_value"), signal_price=t.get("signal_price"))`。
- **验证**：`py_compile` 4 文件 ✅；`test_r2_6_stop_tight.py` 3 项 ✅（止损<1×ATR 亏损单产出"止损太紧"教训 / 旧记录 None 不崩 / journal 存字段+默认 None）；导入冒烟 ✅
- **遗留**：无（零风险项；adopted_lesson_ids 字段已由 R2-3 提前写入 log_entry，本批未动其语义）。

### ✅ R2-1 验证门回滚回写基线【接受】
- **改动文件**：`evolution_gate.py`（on_rollback 回调）、`weight_learning.py`（rollback_to_base 重写 + 绑定）；新增 `test_r2_1_rollback.py`
- **内容**：
  1. `EvolutionGate.__init__` 加 `on_rollback=None` 参数并保存；`_rollback()` 中先 `if self.on_rollback: self.on_rollback()` 再 `_save()`（回调先于持久化，避免 gate 状态与真实权重不一致）。
  2. `weight_learning.rollback_to_base`：`weights=base_weights`、`version+=1`、`rolled_back_at=time.time()`、`_save()`、print 审计行。
  3. `WeightLearner.__init__` 构造 gate 时传 `on_rollback=self.rollback_to_base`；`rolled_back_at` 落盘/载入（不归 0）。
- **验证**：`py_compile` 4 文件 ✅；`test_r2_1_rollback.py` 1 项 ✅（gate._rollback 触发回滚 → weights==base、version 自增、rolled_back_at 记录、weight_state 落盘基线）；导入冒烟 ✅
- **遗留**：R2-3/2/4/5 待后续分批；本批未改。

## R2 实施记录（六条全部落地 — 协调者实施 + D 复核模式）
- ✅ R2-1 EvolutionGate on_rollback 回调（先于 _save）+ rollback_to_base（version+=1 + rolled_back_at）。单测：回滚→weights==base、version 自增。
- ✅ R2-2 WeightLearner 时间切分：record 加 ts；legacy（无 ts）打标排除；train=前70% 生成候选、valid=后30% 算 IC 喂 gate；估算样本不参与。单测：legacy 排除、wait 门槛。
- ✅ R2-3 经验采纳追踪：_ExpAdapter.relevant 只返 trusted（带 id）；decide 恒初始化 adopted_lesson_ids 并按触发分支收集；log_entry 存字段；monitor 只 validate 本笔采纳。单测 4 项。
- ✅ R2-4 watchdog.py（PID 文件 + 心跳 stale/missing 判定 + 去抖3次 + os.kill(pid) 精确 kill + 飞书告警）；两进程 run() 写 .pid 与 heartbeat；docs/ops/watchdog_launchd.md 模板文档。
- ✅ R2-5 _place_tp（attachAlgoOrds 首选 + 原生降级 + tp_missing 打标）；FLAG_ENABLE_EXCHANGE_TP=False 默认关闭；docs/ops/tp_sandbox_verify.md 验证清单。
- ✅ R2-6 deep_review 补 atr_value/signal_price（log_entry 字段 + open_position 传参 + monitor 传参）。单测：止损太紧教训产出/对照无。
- 全量验证：16 文件语法 + 16 模块导入冒烟 + 各方案累计 10+ 单测全过。

## 收敛状态（第 2 轮设计→实施闭环完成）
- 待办 backlog 剩余：RES-15（execute 复用 execution.py，R1 已改同文件、可安全立项）、R1-13 子账户实测（文档已交付，等用户执行）、R2-5 沙盘验证（等用户执行）、RES-8/11/12/13/14/18/19/20（已标放弃）。

## 沙盘实测记录（协调者只读+受控验证，畸形单问题定论）
- 实测1（只读）：orders-algo-pending 必须带 ordType 参数（不带 → 51000）→ R1-1 取消函数已修（枚举 6 类）。
- 实测2（只读）：全 5 币 0 个挂起条件单，而 ETH 仍有 1.22 多仓 → ccxt 旧写法（type=market+ordType=conditional+triggerPrice）挂单从未真正生效，交易所侧止损是幻觉。
- 实测3（受控挂单）：原生构造 triggerPx → 50015 拒绝；slTriggerPx 结构 → 挂单成功、pending 可见（字段全对）、枚举取消 → 0 残留 ✅
- 代码修正：directional_trader SL 用 slTriggerPx 原生结构；_place_tp 降级用 tpTriggerPx 结构。
- 结论：docs/ops/tp_sandbox_verify.md 的验证门槛现在已可满足——TP 可安全开启（FLAG_ENABLE_EXCHANGE_TP），但 attachAlgoOrds 首选路径未实测（本测走的是原生降级路径）。

## R3 收尾批次实施记录（RES-18/20/13 — 协调者实施）
- ✅ RES-18：check_alerts 与 check_signal_event 均改 get(base, max_age=60)（stale 剔除）；funding=None 防护（不再 TypeError 吞异常）；decision_cool 改为"决策后无论开仓与否都置位"（修非交易事件每分钟重复 notify 轰炸）。
- ✅ RES-20：manage_arb_positions 基差 swap_price 缺失 → fetch_ticker REST 兜底；双源都失败 → 告警一次（宁告警不平仓误判），基差退出不再静默失效。
- ✅ RES-13：economic_calendar.calendar_expired()（全过期/空清单检测）；score_calendar 过期→60 分（不再恒 100）+ 30 天冷却飞书告警。
- 验证：py_compile 17 文件 + 4 项离线单测（过期检测/分数降级/None 防护/兜底落盘）全过。

## 沙盘实测记录（畸形单问题定论，含在上一节）

## 策略调整（用户决定）：停用资金费率套利对冲
- 用户判断"对冲不靠谱"→ 新增 config.ENABLE_FUNDING_ARB = False 总开关。
- trading_main.execute 开仓路径直接拦截；check_signal_event 的"费率年化突破"事件（唯一用途=触发套利决策）停用时跳过；费率翻转/价格异动/清算级联告警保留（对方向仓仍有参考价值）。
- 套利代码完整保留，置 True 即恢复。
- 附带影响：跨进程账户串扰问题（杠杆互顶/持仓合并）大幅缓解——单策略运行时不冲突；子账户隔离方案不再必要（组合方案 R1-6+R1-12 已足够）。

## 2026-08-16 复盘链路审计 + 设计立项（协调者，本会话记录）

### 审计发现（全部写入 docs/plans/2026-08-16_self_evolution_design.md §1，编号 DEF-1~9）
- **DEF-1** 熔断强平路径（_liquidate_all）不走复盘链 → 复盘覆盖率 0/1（唯一平仓 txn_004 review 为空）。
- **DEF-2** unverified→trusted 死锁：决策只读 trusted、验证只验证"本笔采纳"，新教训永远无法晋升。
- **DEF-3** post_exit_reverse 死参数：活体调用从不传参，插针维度永不触发。
- **DEF-4** 信号分恒 80 单点 → 方向侧阈值学习被设计性禁用（R1-3）；thresholds 表被临时路径 key 污染（1 行，样本 1 条）。
- **DEF-5** evolution_gate 死代码（全仓库零引用）；**DEF-6** weight_learning 仅接线停用中的套利引擎。
- **DEF-7** analyst 教训 symbol='*'，决策按具体 symbol 过滤，永不匹配。
- **DEF-8** scan_decisions 同秒矛盾行（18:58:54 open+reject、阈值 85 来源不明）→ 疑测试/非生产进程写共享库，待 Phase 0 溯源。
- **DEF-9** 策略级复盘维度缺失：MFE/MAE、R 分布、regime、信号连续分。

### 本轮其他动作
- git：audit-fixes rebase 到 main（快进，无冲突），两分支同点 1fb7e41；工作区干净。
- 建立自进化设计方案 v0.1（含业界标准 S1-S5 与验收表、Phase 0-5 路线图、质疑轮 Q1-Q8）。
- 结论：样本 1 笔平仓前系统定位=「采集观察期」，复盘维度需补策略级指标才可支撑参数改进。

## 2026-08-16 Phase 0 实施记录（复盘链断点修复 + 套利移除，协调者实施）

### 设计先行
- v0.2 设计文档落盘（回答 v0.1 质疑 Q1/Q3/Q6：影子重放集只有证伪权 / 一致性初筛+独立验证两级机制 / 权责矩阵 §7）+ 新质疑轮 Q9-Q14。
- 业界策略调研报告落盘（订单流 CVD/聪明钱 SMC 批评/分场景策略套件/开源机器人，全部附来源）。

### 断点修复（全过零回归约束：基线 6 绿保持、3 红闭环、新增回归测试）
- ✅ T0.1 DEF-1：_liquidate_all 两处（现货/合约）log_exit 后补调 _post_close_review → 复盘覆盖率从此 100%。
- ✅ T0.2 DEF-2：死锁打破——deep_review 每条教训带 implies 归因方向；_post_close_review 做一致性初筛（candidate/dubious）；ScoredExperience 支持状态参数 + candidates()/dubious() + _sync_status 在 3 次验证前保留状态；_ExpAdapter.candidates()；decide() 候选低权重参考（只写理由+采纳追踪，不改参数）。评分只来自后续独立交易 → 无循环论证。
- ✅ T0.3 DEF-3：_post_close_review 采集止损出场后反转（现价 vs 止损位）并传参 post_exit_reverse → 插针教训触发。
- ✅ T0.4 DEF-8：DirectionalTrader.__init__ 加 db_path；_log_scan_decision 落库隔离；test_decision_loop/_make_trader 与 test_service_api 全隔离（journal/exp_bank/ledger/threshold/scan_decisions）；溯源结论：18:58:54 矛盾行与 thresholds 临时 key 均来自测试进程写共享库（test_decision_loop threshold_gate 曾 initial_threshold=85 + scan_signals）。
- ✅ 新回归测试 tests/test_phase0_review.py 14 项全绿。

### 套利引擎移除（用户决定"不需要"，T0.6/DEF-10）
- 归档 legacy/：engines/{trading_main,trading_agent,funding_arb}.py、decision/{weight_learning,scoring}.py、tests/{test_r1_10_close_hedge,test_r1_2_threshold_feed,test_r2_1_rollback}.py。
- service/worker.py 不再托管套利线程；app.py 下线 /arb/status；models.py 移除 ArbStatusOut 与 HealthOut 套利字段；config.py 删除 ENABLE_FUNDING_ARB/ARB_MIN_*；watchdog HEARTBEATS 移除 arb 项；main.py/worker.py 文档同步。
- test_r1_3 重写为 SQLite 版（threshold 落库 + 库隔离，6 项全绿）；test_service_api 移除套利断言（24 项全绿）。
- 活体重启（launchctl kickstart -k gui/$(id -u)/com.crypto.agent）：新 PID 起、/health ok（心跳 0.7s）、3 笔 ETH 持仓衔接、仓位快照续写、heartbeat_arb.txt/trading_main.pid 已清理。启动对账两条"无台账持仓"告警为重启前既存对冲仓（非回归）。

### 既有发现（未改，防越界）
- code_graph --check 报 1 处 pre-existing：directional_trader.pid 被 engines 与 service 两层写入（本次未引入，建议后续单独处理）。

### DEF-8 收尾（2026-08-16 晚）：test_service_api 漏隔离被捕获并修复
- 现象：生产 scan_decisions 在测试运行时刻新增 BTC 行（19:23:06、19:26:59 等），复查全表共 33 条 BTC 行。
- 根因：test_service_api.main() 构造 ServiceTrader 未传 db_path（Phase0 编辑该文件时只删了套利引用，漏了隔离）；其 trader.tick() 触发 scan_signals → 写共享生产库。生产引擎当日候选池为 AEON/LINK（日志可证），从未扫描 BTC → 全部 BTC 行均为测试痕迹（含 test_threshold_gate 的"阈值85/70 成对"签名行）。
- 修复：① ServiceTrader.__init__ 增加 db_path 透传（worker.py，生产默认 None 行为不变）；② test_service_api 传隔离路径；③ 清理生产表 33 条 BTC 测试行（DELETE WHERE base='BTC'，理由与证据如上）。
- 验证：test_service_api 24/24 绿；重跑前后生产 scan_decisions 计数不变（24）；py_compile OK。活体进程无需重启（db_path 默认 None 与现运行行为一致，下次自然重启生效）。
- 预防：任何构造 DirectionalTrader/ServiceTrader 的测试必须全对象隔离清单核对（journal/ledger/threshold/exp_bank/scan_decisions 五项）；改动测试文件时重跑该文件并核对生产库行数不变。

### 2026-08-16 晚 收敛性核查 + 生产库污染哨兵（用户追问"问题是否在收敛"）
- 核查发现：thresholds 表仍残留 3 条测试临时路径 key（T0.4 清理任务只清了 scan_decisions，漏了 thresholds——执行遗漏，非新缺陷）。
- 修复：DELETE 临时 key；新增 tests/test_production_guard.py 哨兵——把 DEF-8 类污染的探测器固化为测试（thresholds 临时 key / scan_decisions 测试标的 / lessons 测试符号，3 项签名断言），此后任何测试漏隔离会在全量套件中被当场抓住。
- 收敛性数据（重启后 19:26 起）：engine_errors 0 条；scan_decisions 生产写入只剩 AEON/LINK（真实候选池）；全量 95 项绿。
- 结论：系统级缺陷到达率已归零，但"收敛"未到可宣布标准——详见设计文档与下方缺陷到达分类表。

## 2026-08-16 晚 收敛机制落地（用户追问"什么机制才能保证问题收敛"）
- 穷尽体检发现并修复：DEF-11（重启后账本不补账→敞口闸门漏计 230 USDT；restore 聚合+覆盖语义，回归测试 17 项绿，活体账本已对齐 230/230）；采集守护挂起 2h（重启，market.db 恢复 1 分钟级写入）；H7 误报修正（改读 /status.risk_halted）。
- 机制落地：M1 tools/health_check.py（H1-H9 不变量，launchd 每 5 分钟 + 飞书告警去重）；M2 test_production_guard.py；M3 tools/test_isolation_lint.py（首跑抓 2 处漏隔离）；M4 全量回归 98 项绿；M5 缺陷台账（设计文档 §10）。
- 当前体检 9/9 全绿；收敛判定标准见设计文档 §10.3。

## 2026-08-16 晚 因子挖掘完善（用户:"整个目标prompt,然后完善因子挖掘"）
- 目标 prompt 落盘: docs/prompts/2026-08-16_factor_mining_goal_prompt.md（自包含主提示词:原理/业界标准/现状差距/范围/阶段/验收/红线）。
- 新增 factors/factor_gate.py 验证门: walk-forward 折内 IC→t 值(Harvey-Liu-Zhu t≥3.0 promote/≥2.0 watch)；成本扣除(净价差<0→reject_on_cost)；去冗余(|corr|>0.7→redundant)；经济逻辑必填(无→hypothesis_only,GP 产物永不自证)；每次检验入 factor_trials 试验日志(storage SCHEMA 新增表)。
- factor_mining.py 4 个因子接入验证门,真实数据裁决: 恐惧贪婪/恐惧贪婪变化/动量7天/均线偏离50 全部 reject(最强 |t| 0.37——与仓库"传统技术指标因子无效"历史结论一致)。
- 离线单测 tests/test_factor_gate.py 6 项全绿(随机拒/单调过/高成本拒/冗余拒/无逻辑降级/日志落库);全量回归 104 项绿。
- Deflated Sharpe/PBO 留接口钩子(试验日志字段齐备),诚实标注未实现(见 prompt §4)。
- 影子政策: 任何因子 promote+人工批准前不得进决策(prompt §6 红线 1);factor_top.json 保持无消费方。

## 2026-08-16 晚 Phase 1 特征采集实施（用户启动）
- 新增 storage trade_features 表 + engines/feature_collector.py（入场/离场特征采集，影子模式、离线安全、失败记账不阻断）。
- 接线：scan_signal 影子连续分(0-100,拒绝K线34%+回踩深度33%+趋势离散度33%)与轻量 regime(波动率分位+趋势斜率+4h 离散度,不用 HMM)；open_position 两路径采集入场特征；_post_close_review 采集离场特征（MFE/MAE 由 1m K 线高低点算 R 计、滑点、持仓时长、反转）。
- 修复真实缺口：TradeJournal.log_exit 此前不落 exit_time（持仓时长/MFE 依赖），已补。
- deep_review 双轨输出（metrics 数值伴文字教训）。
- 订单流特征接入（币安镜像订单簿/主动买占比 + Gate.io OI/爆仓量），仅生产 OKX 适配器启用；失败→null+features_missing 记账。
- 验收：test_phase1_features.py 16 项全绿；全量回归 120 项全绿（零回归）；schema 文档 docs/architecture/trade_features_schema.md。
- 影子政策：signal_score 等特征只记录不进决策，消费方须过 S1-S3 验证（schema 文档 §三）。

## 2026-08-16 晚 全部不依赖样本的工程一次性落地（用户:"所有都一次性做完,样本可以等"）
- Phase 2 评估引擎: tools/strategy_report.py（SQN≥30 笔才报/PF/回撤/MAE-MFE 分布/缺失率,样本不足时诚实标注）+ 体检 H10。
- Phase 3 学习闸门基建: 阈值层喂影子分(FLAG_USE_SHADOW_SCORE_GATE=False 门控默认关, A3 通过后人工开启); experiments 试验注册表 + decision/experiments.py(propose/judge, DSR 概率≥0.95+PBO<0.3+样本≥30); factors/overfit_guard.py(Deflated Sharpe Acklam 逆正态 + CSCV-PBO, 8 项单测含 n_trials=1 边界)。
- Phase 4: lessons 加 regime 列(SCHEMA+ALTER 迁移) + 结构化匹配; analyst 的 symbol='*' 教训进入决策; tools/replay_signals.py 决策重放集(影子,只给证伪权,结果落 data/replay.db)。
- Phase 5: data-dashboard 新增「闭环健康」页签(/api/strategy-report + /api/closed-loop)。
- 收尾: M6 变异注入自证(test_mutation_selfcheck.py 4 项,注入三类污染必被抓+合法行不误报); M7 完成声明=证据包(AGENTS.md §12); pid/心跳写入统一走 execution/pidfile.py(code_graph --check 从 1 处违规 → 0)。
- 验证: 全量回归 12 文件 132 项全绿; code_graph 零违规; 策略体检工具实测输出"样本不足"诚实标注; 重放集后台运行。
- 设计文档 v0.3(§11): Q9/Q10/Q13 答复 + 各阶段状态表 + "等样本"清单。

## 2026-08-16 晚 采集加速调整（用户指示:"策略可以激进点,为了采集数据"）
- 频率/范围放宽 6 项（全部登记 experiments 表 collect_boost_*）: 冷却 180→60min; 每日额度×2(DEFAULT 2→4); MTF 共振过滤临时关闭(1h 单周期可入场,tf4h_spread 特征已记录→把预设变成可事后检验的假设,可回滚 True); 信号分门槛 80→75; 回退池 5→10 主流且扫描池=watchlist∪回退池; 每日扫描流动性门槛 500万→200万。
- 【风险红线不变】: 单笔 1% / 名义≤150 / 总敞口≤600 / 交易所侧止损 / 熔断——全部未动。
- 目的: 加速 30 笔平仓样本积累(当前 1 笔); 每笔仍全特征采集(SQN/期望值/R 分布等样本到位即可检验)。
- 回滚方式: config.py 注释已标明各项原值; experiments 表 change_id=collect_boost_* 一行一项,逐一可逆。
- 验证: 全量回归 12 文件 132 项全绿; 活体重启后体检 10/10。

### 2026-08-16 深夜 采集加速上线后的连锁修复（watchdog 误杀 + 哨兵签名演进）
- watchdog 误杀慢启动（加速后首轮 17 币扫描数分钟,心跳停更>30s → SIGTERM 崩溃循环）: 超时 30→120s + screen_daily 前/扫描循环每币刷新心跳。验证: uptime 139.7s 稳定、首轮扫描完成、心跳 1.4s。
- 哨兵签名演进: 加速后 BTC 进生产扫描池,H3"测试专用标的"签名误报——scan_decisions 标的检查退役（由 test_phase0_review T0.4 每次运行的隔离断言接管）;保留 thresholds 临时 key + lessons 测试 source_trade 模式(^[a-z]\d+$|^fake_)两类无歧义签名;变异自证与 H3 同步更新。
- 采集加速实际生效: 每日扫描候选 2→8 个(MIN_VOL 500万→200万),扫描池 = 8 候选 + 9 回退 = 17 币。

## 2026-08-16 深夜 门槛降到 50 + 策略参数统一维护（用户指示）
- 门槛三件套联动调整: SIGNAL_SCORE 50 / DECIDE_MIN_SCORE 50 / THRESHOLD_INITIAL 45（不联动会"全部信号被拒"——已在 config 注释写明约束关系）。
- 参数统一维护落地: config.py 新增「策略参数统一维护」区块，9 个参数收拢（信号分/决策门槛/阈值初始/拒绝K线比/止损止盈ATR倍数/单笔风险/单笔名义/总敞口）；engines/decision/execution 各模块改为只引用 config、删除私藏副本；PositionLedger 总敞口默认值也从 config 读。
- 测试对齐: 各测试改用 config 常量（门槛用例 49/50、阈值用例 85/45）。
- 验证: 全量回归 12 文件 130 项全绿。

## 2026-08-16 深夜 参数集中化规则落地（用户规则:新增参数只能在 config.py 加）
- config.py 统一维护区扩到 30+ 参数: 信号/门槛/阈值三件套、风险红线三件套、回退池/杠杆表/特性开关、每日扫描 6 参数、经验库衰减/复活、日度分析 6 参数、试验注册表 3 门槛、LEGACY_CT_VAL 面值表。
- 各模块(engines/decision/execution)只保留 config 引用别名;搬运时修正一处自引入错误(DOGE 面值 1.0 误写 0.001,已还原)。
- 机器执行: tools/params_lint.py(扫描策略层模块级字面量赋值) + tests/test_params_centralization.py(进全量套件);AGENTS.md §13 规则。lint 首跑抓出 LEGACY_CT_VAL 漏网。
- 验证: 全量回归 13 文件 131 项全绿;lint 0 违规。

## 2026-08-16 深夜 激进第二档（用户指示:模拟盘,激进为主,降低参数加大交易概率）
- 门槛三件套: 40/40/35; 拒绝K线比 1.5→1.0; 冷却 60→30min; 每日额度×2(最高16笔/币); 流动性 100万; 趋势偏离 0.3%; ATR 甜区 0.3%-8%; 候选池 8→12。
- 全部经 experiments 登记(aggressive_v2_* 4 项); 风险红线(1%/150/600/交易所侧止损)未动。
- 验证: 全量回归 131 项全绿(见套件); params_lint 0 违规。

## 2026-08-16 深夜 策略 B（突破/动量确认）影子模式上线（用户 OK）
- 背景: 用户问"现在行情真的会产生信号吗"——实测当前横盘行情 10 主流 8 空头排列,A 信号稀缺;重放集用激进参数 9 笔/期望 -0.746R(证伪权提示门槛再降质量转负)。
- 决策: 不再压榨 A 的门槛,新增互补信号源 B(突破/动量确认,调研 R3 策略套件候选),影子模式。
- 实现: engines/strategy_b.py(放量突破前 N 高/低点+阳/阴线,1×/2×ATR 风险框架,影子分 0-100) + storage shadow_signals 表 + 扫描循环接入(只记录/绝不下单/kline_ts 去重) + config 参数 STRATEGY_B_SHADOW_ENABLED/BREAKOUT_LOOKBACK/BREAKOUT_VOL_RATIO。
- 测试: test_strategy_b.py 10 项全绿(触发/不触发/去重/引擎级零下单)。
- 验证: 全量回归 141 项全绿(见套件);影子政策红线: 验证门+人工批准前永不转正。

## 2026-08-16 深夜 XRP 空头信号→沙盘拒单事件（用户问"现在没有开空信号吗"）
- 事实: 23:16:56 XRP 触发真实空头信号(空头趋势+反弹拒绝,决策 open)→ 下单被 OKX 沙盘拒(code=1 All operations failed,反查未确认成交)→ 系统 fail-closed 放弃,无仓位无飞书。
- 排查: XRP-USDT-SWAP 规格正常(ct_val=100, lot 0.01 张, min 0.01 张)、余额正常、仓位计算 1 张≈100 USDT 在 150 红线内 → 疑沙盘环境对该合约的特定拒单/瞬时故障。
- 观察项: 若后续其他币也出现同错→系统性;仅 XRP→合约特定。scan_decisions+日志+账本三重记录完整(信号→决策→执行失败全链路可追溯)。
- 行情佐证: 7/8 空头币现价高点距 EMA20 仅 0.06%-0.30%,空头反弹拒绝形态随时可能再触发;策略 B 影子同时盯跌破前低的空头突破。

## 2026-08-16 深夜 下单失败结构化日志（用户问"有没有下单失败的日志"）
- 现状缺口: 此前下单失败只有 stdout 文本(❌ 开仓失败...),不可查询/告警/看板展示。
- 落地: storage order_failures 表(ts/base/inst_id/side/qty/stage/error);引擎 13 个失败点接线(开仓/平仓/止损挂单/TP 挂单/预检拒绝——含"名义不足最小张数"等信号未成单原因);体检 H11(近 24h 失败 ≤5 告警);看板闭环健康页新增下单失败面板。
- 测试: test_strategy_b 新增失败落库用例(15 项全绿)。
- 说明: XRP 23:16 的拒单早于本日志上线,不在表内(未来失败全量记录)。

## 2026-08-17 凌晨 dsh-alert-inject 推送插件落地（用户方案"异常推入本 session"）
- 成果: 监控失败 → POST 127.0.0.1:3080/alert-inject → 转发事件 → 客户端注入当前会话,端到端实测通过(beacon 全链 injected-ok)。
- 实现: 工作区 dsh-alert-inject 插件(host 路由+客户端 bundle);dsh-api-remotes 白名单加 alert/injected;装进 web profile + cordis.patch.yml;dsh web 移交 launchd(com.dsh.web KeepAlive)托管。
- 调试踩坑: sessions.sessionOf(ctx) 吃 context 非 id,正确 API sessions.binding(id).session;客户端 bundle 热更新、host 改动需重启;首次事件未达是浏览器未刷新加载新 bundle。
- crypto-agent 侧: alert_diag.diagnose_and_alert 失败时同时 POST 注入(飞书之外第二通道)。
- 遗留(可接受): 插件内调试埋点保留(beacon+probe,probe 仅显式触发,生产无害)。

## 2026-08-17 凌晨 未触发信号复盘落地（用户建议:"没触发也要复盘为什么没触发"）
- 价值: 复盘维度补齐"机会成本/未触发归因"缺口——每轮 no_signal 记录四环节画像(趋势/触线/影线/量能)+瓶颈识别+近失标记,回答"信号断在哪一环"。
- 实现: storage signal_profiles 表; strategy_b.profile_from_klines(复用策略 A 同款条件,零额外 API——用策略 B 已取的 kl_b);扫描循环 no_signal 时落库; tools/no_signal_report.py 聚合(瓶颈分布/近失/分币画像/结论建议);看板闭环健康页新增面板。
- 测试: test_strategy_b 增 3 项(横盘→trend/下跌未触线→touch/落库隔离),20 项全绿;全量回归见套件。
- 设计意义: 瓶颈分布是策略改进的直接证据(如"80% 卡趋势"→补突破策略;"近失>20%"→门槛微调即可提频,经 experiments 留痕)。

## 2026-08-17 凌晨 止损监控提速（用户建议:"下单后秒级感知价格,提高止损速度"）
- 原状: WS 仅订阅 5 币(worker 硬编码,池外 15 币下单后监控走 REST 查价);监控节拍 2s。
- 提速三件套: ① OKXRealtime.subscribe() 动态订阅——open_position 成功后立即把该币接入 WS 秒级推送(幂等/重连自愈/线程安全);② worker WS 覆盖全回退池(config.SYMBOLS 10 币);③ 监控节拍 2s→1s,交易所持仓 REST 快照 2s 节流(避免 REST 频率翻倍),快照未刷新拍只做价格判定、平仓执行等下一拍(≤1s)。
- 延迟链现状: 价格变动 → WS 秒级推送 → 1s 节拍内判定 → 市价平仓(REST ~百毫秒) + 交易所侧 slTriggerPx 原生止损兜底(进程崩溃也生效)。
- 测试: test_phase0_review 增 2 项(订阅去重/持仓节流不误平),23 项全绿;全量回归见套件。

## 2026-08-17 凌晨 统一异常中心（用户要求:所有异常统一输出到一个接口,报警链统一打到本 session）
- 落地: storage anomalies 表(唯一事实源: source/severity/title/detail/status,30min 同源同题去重); tools/anomalies.py 唯一登记入口。
- 生产者全部收编: 体检失败(health)/下单失败逐笔(order_failure)/引擎异常(engine_error)/风控熔断(risk,critical)——不再各写各表;alerts 信箱退役。
- 统一接口: 交易服务 GET /anomalies + 看板 /api/anomalies + 闭环健康页「统一异常中心」面板(置顶)。
- 报警链统一: alert_diag 从 anomalies.list_new() 组装统一格式消息 → 飞书 + 注入本 session。
- 测试: anomalies 登记/去重/resolve 3 项,test_strategy_b 24 项全绿;全量回归见套件。

## 2026-08-20 凌晨 阈值进化门接线 + 证据时间衰减（用户拍板"都做吧"，DEF-5 闭环）
- 背景: EvolutionGate 自审计 CR-8/CR-10 落成后一直是死代码(DEF-5)——阈值校准
  (threshold_learning.calibrate)满 30 样本直接改阈值,无影子验证、无观察期、无回滚。
  经验库分数有衰减但 evidence_strength 聚合无衰减——老教训证据权重永久满额。
- 落地(阈值门): ThresholdLearner 增 gated 模式(record 只记录)+propose(只算不改)
  +apply_threshold(门晋升/回滚唯一写入口,不夹逼——回滚基线 35 低于学习器下限 60,
  夹逼会偷偷抬高回滚值);引擎 _threshold_gate_step 接线: 每笔真实平仓→现役样本
  +候选反事实影子样本(候选拒绝的交易记 0)→满 GATE_MIN_SHADOW(30) 且优势
  ≥GATE_MIN_EDGE(0.001) 晋升→观察期(GATE_OBSERVE_BATCH=10)退化自动回滚
  THRESHOLD_INITIAL。诚实声明: 反事实只对收紧方向有证据力,放松阈值机器不可自动过门。
- 落地(证据衰减): _evidence_weight = clamp(good-bad,0,CAP) × 0.5^(天数/
  EVIDENCE_HALFLIFE_DAYS=30),evidence_strength 与 rollup_lessons 同口径(防两套数学)。
  借鉴 FinMem 分层记忆衰减思想,但保留本仓可审计的 SQLite 教训表,不引 LLM 依赖。
- 其他: EvolutionGate._save 改原子写(.tmp+os.replace);候选支持 meta(机器可读参数);
  ThresholdLearner/gate 状态文件随 db_path 隔离(T0.4 同口径)。
- 参数新增(config 参数区): EVIDENCE_HALFLIFE_DAYS / GATE_MIN_SHADOW / GATE_MIN_EDGE /
  GATE_OBSERVE_BATCH。
- 测试: 新增 tests/test_gate_wiring.py 14 项(衰减半衰期数学/gated 不自动改/提案→影子
  →晋升/退化→回滚基线/状态文件隔离);修正 test_exchange_layers 过时断言(08-19
  FLAG_ENABLE_EXCHANGE_TP 后开仓挂 2 张条件单,旧断言写死 1 张,见 pitfalls);
  全量回归 15 文件 191 项全绿 0 失败;params_lint/code_graph/fix_guard 全过。

## 2026-08-20 凌晨 交易 API 层加固 + 引擎上帝类按功能拆分（用户指示"顺手修掉,再把上帝类拆了,按功能维护"）
- 加固(exchange/transport.py 两处小瑕疵):
  ① 限速器加锁——监控线程(1s)与扫描线程共用同一适配器实例,_last_ts 读写无锁
  会让两线程同时通过限速检查;threading.Lock 包住比较+更新。
  ② 重试升级——固定 1 次/1s → 指数退避 MAX_ATTEMPTS=3(1s→2s),覆盖 429 与
  网络抖动(SSL EOF/握手超时);POST 重试安全依旧由 clOrdId 幂等键保证。
  (常量属传输层结构参数,exchange 层不在 params_lint 扫描域,留模块头常量。)
- 拆分(engines/,1383 行上帝类 → 核心壳 391 行 + 四个功能块):
  signal_scan.py(295) SignalScanMixin — scan_signal/scan_signals/_trade_budget/
    _is_auto_untradable/_long_scan_progress/_log_scan_decision/_build_trade_conditions;
  position_mgmt.py(364) PositionMixin — open_position/_recover_order/_place_tp/
    _cancel_stop_orders/_log_order_failure;
  risk_monitor.py(254) RiskMonitorMixin — monitor/_liquidate_all/_pos_gone/
    _log_51169_throttled/_log_risk_event;
  review_pipeline.py(160) ReviewMixin — _post_close_review/_threshold_gate_step;
  directional_trader.py 保留 入口/notify/connect/instance_lock/_ExpAdapter/
    __init__/_reconcile_startup/行情辅助/_dir_cn/run_once/tick/run。
- 拆分纪律(为什么选 Mixin 而非组合对象): 类名/方法名/self 状态一个不动——
  测试(dt.open_position 等直呼)、ServiceTrader 子类 override、fix_guard 字符串
  护栏、活体进程全部无感;方法体逐行搬移(脚本按行段搬,非手抄),行为零变化。
- 顺手修复: monitor 现货平仓异常分支引用未定义变量 qty(潜伏 NameError,见 pitfalls);
  fix_guard G1/G5/G7/G10/G12 护栏路径随代码搬家同步更新。
- 证据: 全量回归 15 文件 191 项全绿 0 失败;params_lint/code_graph --check/
  fix_guard 12 条全过;MRO 装配自检 26 个方法无缺失。

## 2026-08-20 凌晨 只做合约 + 美股合约接入（用户拍板"我们不做现货,只做合约"）
- 背景: 用户问"为什么没有美股合约交易"。OKX 生产实测 19 个美股/公司代币永续
  (NVDA/TSLA/AAPL/MSTR/…/ANTHROPIC/OPENAI),24h 额全部 ≥100 万;沙盘有其中 9 个。
  阻塞链共四层,全部修复:
  ① daily_scan 把 SWAP volCcy24h(币本位)当 USDT 比较 → ANTHROPIC(实际 168 万)
    每天被阶段1误杀(watchlist 历史 0 行);修复 = volCcy24h × last。
  ② 旧美股池走 X 前缀现货清单 → 与"只做合约"冲突;_stock_pool 收敛为
    config.STOCK_SWAP_TOKENS 合约口径(沙盘实测 9 个)。
  ③ 引擎数量对齐 floor(qty, ctVal) 过粗 → ctVal=1 币(≈180-500 USDT/张)的合约
    在 150 名义上限下 floor 到 0,9 个里 6 个被拒;修复 = floor(qty, lotSz×ctVal),
    9/9 全部可交易(核算表见当日 session,ANTHROPIC 0.829 币/149.9U 等)。
  ④ 沙盘元数据 lotSz(0.001) 与真实撮合粒度(0.01)不一致 → 51121;okx_adapter
    自愈: 粗化 ×10 重试(≤3 次)+有效粒度按 instId 缓存,条件单/平仓沿用。
- 政策落地: config.SWAP_ONLY=True(参数区,用户拍板)——开仓层硬闸门,无合约
  场所一律拒绝(reject_spot_only 记决策日志);现货路径代码保留不可达(可逆)。
  顺手修复 XRP 被 startswith("X") 误标 is_stock 的看账污染。
  ⑤ 候选池两处收尾: watchlist 同日重扫改"先 DELETE 当日再写"(旧候选残留);
    screen_daily 增只做合约预过滤(69→51,X 系现货代币曾凭现货成交额从加密
    观察池混入占 4/12 席;清单获取失败 fail-open,开仓层闸门兜底)。
- 沙盘实测证据: ANTHROPIC 合约全链路 开多→挂止损(slTriggerPx)→pending→撤单
  →reduceOnly 平仓 全程 sCode=0 无残留;0.831 币经 51121 自愈成功、0.83 直过。
- 活体验证: 重启后候选池 12 个 = CRCL(美股合约,全场最高分 0.79)+TSLA 首次
  入选,X 系现货全部清出;KAITO 持仓/台账衔接,心跳正常。
- 测试: test_exchange_layers 新增 51121 自愈 5 项(桩传输层),全量 15 文件
  196 项全绿 0 失败;params_lint/code_graph/fix_guard 全过。
- 已知边界更新: AAPL/MSTR/COIN/META/AMZN/INTC/SNDK/SOXL/LITE/AMD 生产有合约
  但沙盘暂缺(XIAOMI 同类),沙盘补上后扩 STOCK_SWAP_TOKENS 即可。

## 2026-08-20 凌晨 沙盘不可交易币预过滤（用户拍板清遗留：BICO/WLD/ZEC/HYPE 占候选名额）
- 落地: `untradable_bases()` = DEMO_UNTRADABLE ∪ untradable_symbols；
  screen_daily 阶段1 前剔除(连 K 线都不拉)；回退池同步过滤；scan_signals
  第二道跳过旧池残留。  开仓层 reject_untradable 闸门保留。
  另: 公开接口也打 x-simulated-trading(此前只打签名请求)——instruments/tickers
  与沙盘账户一致,INTC/SOXL 等生产有、沙盘无的合约不再凭生产流动性占席。
- 测试: test_daily_scan_drops_untradable(BICO 静态 + ZEC 动态,BTC/SOL 仍入选)。
- 活体: 重扫后候选 12 = ETH/SOL/BTC/HBAR/AI16Z/XRP/AAOI/CRWV/CRCL/LINK/AAVE/MET,
  黑名单与沙盘缺口(BICO/ZEC/HYPE/WLD/INTC/SOXL/MSTR)残留 0;KAITO 持仓衔接。

## 2026-08-17 凌晨 未触发归因反哺决策系统（用户问:归因如何反哺决策）
- 落地 tools/no_signal_report.py: generate_feedback() 把画像分布转成四条反哺规则提案——R1 影线门槛微调候选(近失≥20%+主瓶颈wick)/R2 策略B转正评估启动(trend≥60%)/R3 纪律性等待显式抑制调参(touch≥70%)/R4 量能观察(vol≥40%)。
- 反哺纪律: 提案只进 experiments 注册表(proposed),永不自动生效——验证门(S1-S3)+人工放行(防过拟合红线)。
- 当前实测: 主瓶颈 touch 84% → R3 触发("等回踩是纪律,抑制调参冲动")——归因反哺的第一课是"什么都不改"。
- 测试: 4 项规则触发/抑制断言,test_strategy_b 29 项全绿。

## 2026-08-20 凌晨 TradeJournal ID 防撞号 + 写库增量化
- 背景: log_entry 按内存 `len(self.trades)+1` 生成 `txn_001` 式主键,两进程同开或重启后内存与库不同步会撞号;
  `_save()` 对全表逐笔 INSERT OR REPLACE(每笔独立连接+commit),注释声称增量 UPDATE 但未实现——撞号即静默覆盖丢台账。
- 落地(execution/trade_journal.py,调用方签名零改动,不改 storage/db.py):
  ① 新 ID = `txn_{int(time.time())}_{secrets.token_hex(2)}`,旧 txn_001 不迁移(lessons.source_trade /
    trade_features.trade_id 继续引用)。
  ② log_entry 纯 INSERT(主键冲突抛错不覆盖);log_exit 只 UPDATE status/exit_price/exit_reason/exit_time/pnl;
    save_review 只 UPDATE review/review_ts;review 只 UPDATE review(lessons 仍只内存追加,kv.legacy_journal_lessons 不动)。
  ③ `_save()` 全量重写保留,仅 JSON 迁移 + `_backfill_notional` 回填可走(position_mgmt TP 打标仍兼容调用)。
- 测试适配: test_service_api save_review 不再写死 txn_001,改读 journal.trades[0]["id"]。
- 验证: 临时库双 TradeJournal 各 log_entry 一笔 → ID 不同且 trades=2;log_exit 一笔另一笔未改;
  手工插入 txn_001 后 _load + log_exit 正常。py_compile 通过;test_exchange_layers 24/0、
  test_service_api 24/0;journal 相关 test_r2_6 3/0、test_p0_fixes 13/0、test_phase0_review 33/0、
  test_decision_loop 14/0、test_phase1_features 16/0。storage/db.py 与活体进程/crypto_agent.db 未动。

## 2026-08-20 凌晨 SQLite 事务原语 tx() + watchlist/快照原子写
- 背景: storage/db.py 只有 q/q1/x 三个原语,x() 每条写独立短连接+自动 commit;
  没有跨多条语句的事务。daily_scan 重建当日 watchlist 先 DELETE 再逐条 INSERT,
  中途崩溃会留下空的/半截候选池,当天开仓决策全部读这份残缺数据。
- 落地:
  ① storage/db.py 新增 tx(db_path=None) contextmanager: yield 已设 WAL/
    busy_timeout/synchronous=NORMAL/row_factory 的连接;正常退出 commit,
    异常 rollback 后重抛,finally close。docstring 写清何时用 x、何时用 tx。
  ② _connect 补 PRAGMA synchronous=NORMAL(WAL 官方推荐;checkpoint 仍 fsync,
    已提交事务崩溃不丢,写入更快)。短连接/WAL/busy_timeout=5000 设计不变。
  ③ engines/daily_scan.py watchlist 重建: DELETE+全部 INSERT 包进一个 tx()。
  ④ service/worker.py 与 engines/signal_scan.py 的 position_snapshots 是
    "一轮快照多行"(同一时刻持仓全集),一并包进 tx();单行写路径未动。
- 未改: execution/trade_journal.py(单写者,另一协作者刚改过);活体进程未重启;
  工作目录 crypto_agent.db 未直接修改。
- 测试: py_compile 改动文件通过;临时库验证 commit 路径/异常 rollback 原子性/
  PRAGMA synchronous=1(NORMAL) 三项全 PASS;test_exchange_layers 24/0、
  test_service_api 24/0;相关 test_r1_3_atomic_write 6/0、test_production_guard 2/0
  (tests/ 下无 db/storage/scan 文件名匹配的专用测试);code_graph --check 无反向依赖。

## 2026-08-20 凌晨 OKX REST 收敛到 exchange 层（用户：api 都没有收敛到交易层）
- 背景: 四层架构已落地，但交易路径仍旁路：daily_scan urllib 打 tickers/instruments、
  K 线走 data/fetch_okx（history-candles + 24h 文件缓存，扫描可能用到隔日 K）、
  realtime_okx REST 预热裸 URL、deploy_guard 穿透 transport、引擎直 import make_cl_ord_id。
- 落地:
  ① ExchangeAdapter 增 fetch_tickers(venue) / new_cl_ord_id()；TickerInfo.vol_usdt_24h
    在适配层归一（SWAP = volCcy24h × last，SPOT 原样）——ANTHROPIC 成交额坑不再能在
    策略层重踩。
  ② daily_scan 只吃适配器；SWAP_ONLY 观察池直接用合约 ticker 排名（不再用现货池再滤）；
    回退主流池 instId 改为 -USDT-SWAP；引擎/HTTP 注入同一 exchange 实例；db_path 隔离。
  ③ WS REST 预热改为调用方注入 fetch_candles（data 不 import exchange，守分层）。
  ④ position_mgmt 改 exchange.new_cl_ord_id()；deploy_guard 改 cancel_algos；
    health_check H13 走 OKXTransport.public(/public/time)。
  ⑤ 交易四层（engines/service/decision/execution）静态禁止 okx.com 与 /api/v5/。
- 故意保留: data/fetch_okx.py 的 history-candles 分页（研究/回测，非交易路径）；
  data/collect.py / tools/okx_pg_ingest.py 采集脚本（data 层不得反向 import exchange）。
- 活体: 未重启（纯路径收敛，下单语义不变）；重启后验证心跳/持仓衔接后再宣称恢复。
- 证据: test_exchange_layers 31/0（含 ticker 归一 3、daily_scan 离线回退 3、交易路径无 URL 1）；
  test_service_api 24/0；test_p0_fixes 13/0；test_phase0_review 33/0；test_decision_loop 14/0；
  params_lint / code_graph --check / fix_guard 12 条全过。

## 2026-08-20 凌晨 SQLite 索引对齐 + 流水保留 90 天 + 迁移版本化
- 背景: SCHEMA 索引与真实查询不对齐(trades 按 entry_time 查/排,anomalies 去重按
  source+status,shadow_signals 按 base+strategy+kline_ts);流水表只增不删会无限膨胀;
  `_add_missing_columns` 靠 PRAGMA table_info 逐列探测,后续列会越积越多。
- 落地(storage/db.py; 不改 execution/trade_journal.py; 不写活体 crypto_agent.db):
  ① 索引: 新增 idx_trades_entry_time(trades.entry_time)、
    idx_anom_source_status(anomalies.source,status)、
    idx_shadow_base_strategy_kline(shadow_signals.base,strategy,kline_ts);
    DROP 旧 idx_anom_status(CREATE INDEX IF NOT EXISTS 不会删旧名;SCHEMA 每次
    init_db 都 DROP INDEX IF EXISTS,迁移 v2 同样 DROP——防止 version 已升到
    最新后活体旧进程把旧索引建回来、迁移不再跑)。
  ② 保留: config.DB_RETENTION_DAYS=90。prune_old_rows 用 tx() 清流水表
    (scan_decisions/position_snapshots/signal_profiles/engine_errors/
    shadow_signals/order_failures/analyses kind=daily) 以及已 resolved 的
    alerts/anomalies;status='new' 即使过期也留。永不碰 trades/lessons/
    lesson_rollups/trade_features/experiments/factor_trials/thresholds/
    watchlist/ownership/untradable_symbols/kv。清理后 PRAGMA optimize(不做 VACUUM)。
    engines/daily_scan.screen_daily 扫完候选后调用一次并打印结果。
  ③ 迁移: PRAGMA user_version。MIGRATIONS v1=lessons.regime/conditions 补列
    (探测后再 ALTER,duplicate column 容错);v2=索引替换。全新库 SCHEMA 建满后
    直接 user_version=SCHEMA_VERSION;老库(version=0 表齐全)按序跑迁移。
- 查过但不加的索引: lessons(category,content) 表小且主路径全表扫描;
  experiments.change_id 试验行极少;anomalies(source,title,ts) 与用户指定的
  (source,status) 重叠且表小;engine_errors.error LIKE 不适合建索引(已有 ts);
  risk_events 已有 idx_risk_ts、未列入清理清单;watchlist/kv/thresholds/ownership
  走主键;alerts 已退役无查询调用方。
- 活体: 未手工 DDL/DELETE。TestClient 回归曾让 HTTP init_db() 碰到活体库
  (user_version 已被升到 2,新索引已在,旧 idx_anom_status 被旧进程建回)。
  SCHEMA 现每次 DROP 旧索引;下次服务重启(或下一次 init_db)会清掉旧名。
  未重启活体进程。
- 证据: py_compile 通过; params_lint 通过; code_graph --check 无违规;
  test_exchange_layers 31/0、test_service_api 24/0、test_params_centralization 1/0;
  相关 test_strategy_b 29/0、test_r1_3_atomic_write 6/0、test_production_guard 2/0。
  临时库: 全新库 user_version=2 且新索引在/旧 idx_anom_status 不在;
  老库 version=0+旧索引 → 迁移后 version=2、旧索引已删、lessons 补列幂等;
  version 已是 2 但仍带旧索引 → init_db 后旧索引被 SCHEMA DROP 清掉;
  prune 过期流水各删 1、new anomaly 保留、trades/lessons/kv 行数不变。

## 2026-08-20 飞书通知改 interactive 卡片（用户：md 无法正常渲染）
- 背景: 飞书 `--text` 不渲染 Markdown；`--markdown` 包装成 post 也不解析
  星号（alert_diag 2026-08-17 已实证）。交易/看门狗通知仍走 `--text`，开仓
  一行用 `|` 拼接，AI 诊断的 `**` 在主通道会原样显示。
- 落地:
  ① `decision/notify.py` 唯一出口：GitHub MD → lark_md 子集（标题变加粗、
    去掉围栏/表格），发 interactive 卡片；失败则 `--text` + 剥标记。
  ② `engines/directional_trader.py` / `tools/watchdog.py` / `tools/alert_diag.py`
    不再各自拼 CLI。
  ③ 开仓/平仓/每日看账/候选池文案改为「首行标题 + 换行字段」，关键数字加粗。
- 未重启活体进程（下次服务起来后新格式才发出）。
- 证据: tests/test_notify.py（卡片结构/清洗/CLI 参数，不真发飞书）。

## 2026-08-20 开仓日志与台账笔数对齐
- 背景: 看账「开仓 159」vs 台账 24 笔。扫描在下单前就把 decision=open 落下;
  ALLO 当天 51001 失败仍算开仓。历史按日: 08-17 意图 49/成交 4, 08-19 59/8。
- 落地:
  ① `scan_signals` 等 `open_position` 返回 tid 才记 open。
  ② 失败路径记 `open_failed`(下单失败/熔断/幂等/余额/账本)或既有 `reject_*`。
  ③ 看账改「成交 N 笔（已平 M）」,不再把扫描意图叫开仓。
- 未改活体库历史行(旧 open 仍是意图,7 天窗口内 JSON 字段 scan_opens 仍含旧数)。
- 未重启活体进程。
- 证据: tests/test_decision_loop.py `test_open_logged_only_on_fill`
  (成交 open=台账; 失败 0 open + open_failed)。

## 2026-08-20 扫描尺子自进化（影线比：提案→影子→验证门→人工批准）
- 背景: 扫描层规则写死；未触发画像只会出建议、不改尺子。用户要求扫描也能
  自进化，但必须先影子验证、证明更好，再由人批准改一根尺子。
- 落地（只动 REJECT_WICK_RATIO）:
  ① 近窗 signal_profiles 触发 R1（近失≥20% 且主瓶颈=wick）→ experiments
     kind=scan_wick 提案（候选=现役×0.9，下限 0.8）。
  ② 现役没信号、候选影线比会出信号 → shadow_signals(A_wick) 只记账、不下单。
  ③ 随后 24 根 1H 按止盈/止损路径结算（同根两边都打按止损，影子不美化）。
  ④ 满 30 笔且均盈>GATE_MIN_EDGE 且 DSR 概率≥0.95 → accepted；**仍不改尺子**。
  ⑤ POST /scan/evolve/approve 才写 kv 覆盖；config 基线不变；rollback 恢复。
- 红线: 机器不得自动放宽扫描门槛；风控 1%/150/600/交易所止损未动。
- 测试: tests/test_scan_evolve.py；test_service_api 增 /scan/evolve 观测与
  未过门批准 409。

## 2026-08-20 看账总盈亏改实际 USDT
- 背景: 用户要求「总盈利写实际 usdt」。原先飞书每日看账写 `总盈亏 +x.xx%`，
  是把各笔价格变动比例加总，名义不同的两笔同 +2% 会被写成 +4%，不是账户真赚了多少钱。
- 落地:
  ① `execution/trade_journal.py` 增加 `realized_pnl_usdt` / `total_realized_pnl_usdt`
     （pnl 比例 × notional_usdt）。
  ② 每日看账飞书文案改「总盈亏 **±N.NN USDT**」；报告 JSON 增加 total_pnl_usdt。
  ③ `/journal` 增加 `total_pnl_usdt` 与单笔 `pnl_usdt`。
  ④ 平仓通知复用同一换算，避免两套公式。
- 未重启活体进程（下次看账/服务起来后新格式才发出）。
- 证据: tests/test_decision_loop.py「已实现盈亏按实际 USDT」；
  test_service_api `/journal` 单笔与合计 USDT。

## 2026-08-23 AI 友好仓库第二轮完善（入口契约 + 漂移守卫）
- 体检发现: `llms.txt` 有 1 条已归档模块失效链接；AI 友好文档仍写不存在的
  dependency_graph 命令；AGENTS/README 裸启动命令会落到代码的 live 默认模式；
  docs 索引漏 `AGENT_NOTES.md` 与 watchdog 手册；扫描节奏/行情后端说明已漂移。
- 协作契约: 重写 `docs/architecture/ai_friendly_repo.md`，补 60 秒接手路径、事实优先级、
  高风险冲突 fail-closed 规则、按任务路由和证据矩阵；AGENTS 增最短路径与 agent claim 协议。
- 安全入口: README/AGENTS 启动示例显式 `CRYPTO_AGENT_MODE=paper`；代码能力与 AI 操作授权
  分离。本轮不改交易模式、策略参数、活体配置，不启动/重启任何服务。
- 机器执行: 新增纯标准库 `tools/ai_repo_check.py`，检查根入口、根目录散装 Markdown、
  本地链接存在且不越界、llms 关键入口、docs 全索引、AGENTS 关键操作护栏；新增
  `tests/test_ai_repo_check.py` 以失效链接/孤儿文档变异自证；CI 接线并纳入 27 脚本全套件。
- 文档收敛: 移除 llms 的 `engines/trading_main.py` 失效入口，补 ccxt_adapter、协作协议、
  AI 守卫；docs/README 索引数修正为 31（不含自身），补齐两篇漏项。
- 验证: AI 自检通过；新测试 6/6（失效链接/孤儿文档/散装文档/AGENTS 护栏/链接越界）；
  py_compile 2 文件通过（PYCACHE 隔离到 /tmp）；
  code_graph 无违规；params_lint 0 违规；test_isolation_lint 全通过；fix_guard 21/21；
  全量 27 个离线测试脚本在独立 `/tmp` 数据库/事件文件中运行，累计 382 项通过、0 失败。

## 2026-08-23 通知、事件隔离、分层与 CI P1 收敛
- 通知判断：真实输出由 `config.TRADE_NOTIFY_ADAPTERS` 集中配置，覆盖 `okx` 与
  `okx-ccxt`；FakeAdapter 保持静音，不触碰飞书或事件文件。
- 事件分层：JSONL 实现从 `service` 下沉 `execution`，引擎只调用注入的
  `_log_event`；测试 `db_path` 自动派生独立事件文件，CI 额外为每个脚本设置独立
  `CRYPTO_AGENT_DB` 与 `CRYPTO_AGENT_EVENTS_FILE`。
- 测试修复：通知重试覆盖瞬时恢复、持续失败和纯文本兜底；统计写入、等待、CLI
  均替换；隔离 lint 改 AST，正确识别关键字与位置参数。
- CI：原 26 个脚本全部接入；同期新增 AI 仓库守卫后按 27 个全量运行，并包含
  compile、参数集中化、代码图分层、测试隔离和修复护栏。依赖补
  `ccxt>=4.5,<5`，验证项目 `lib` 中 ccxt 4.5.64 与 `ccxt.pro` 均可导入。
- 安全边界：未改 sandbox、风险参数、下单/止损逻辑，未启动或重启活体进程。

## 2026-08-23 开仓准确率计划·批次 A（候选数据可信）
- T0：在 `config.py` 固化策略版本、特征 schema、24H/1m 标签口径和结算周期；新链路不改变 1%/150/600、交易所侧止损或 HTTP 权限。
- T1：新增 `signal_samples` 与幂等采样器。结构信号先留样，再过额度、冷却、分数、经验、AI 和执行；同币/方向/1H K/含配置哈希版本只允许一个候选，决策轨迹保留 rule/AI/final/reason/trade_id。
- T2：新增 `signal_outcomes`、纯函数首触结算器、worker 定时 sweep 和默认 dry-run 的回填工具。原生 OKX 与 CCXT 适配器均提供区间 K 线分页；覆盖不足保持 pending，禁止用当前价伪造。
- 验证：新增 `test_signal_sampling.py` 9/0、`test_signal_outcomes.py` 15/0；改动文件 py_compile 通过；params_lint、code_graph、test_isolation_lint 全绿。未联网、未启动/重启实例、未写生产库。

## 2026-08-23 开仓准确率计划·批次 B（预测语义可信）
- T3：`decision.forecast` 拆为 regime moving-block 路径、完整 terminal distribution 和独立 first-passage；空单障碍改为 stop 在上/TP 在下，历史概率按样本量收缩，校准只接受 `signal_outcomes` 的真实路径标签。
- T7：新增 `decision.extrema_forecast`，提供 U_H/D_H 的 q10/q50/q90、方向+regime 经验基线、L2 线性分位模型、pinball/coverage/adaptive conformal 工具；分位交叉 fail-closed。
- 接线：扫描链使用方向正确的 ATR 障碍；样本达到经验基线门槛后把最高/最低概率区间附到 forecast，仍只展示、不成为开仓门。
- 验证：`test_forecast` 12/0、`test_forecast_semantics` 11/0、`test_extrema_calibration` 8/0、`test_exchange_layers` 37/0；short 扫描 forecast 非空、terminal 不截断、三类概率归一。参数 lint 与代码图通过；未联网、未启动/重启实例。

## 2026-08-23 开仓准确率计划·盘口特征可达性修复
- 修复信号扫描中 `_microstructure_features` 被动态 OFI 函数截断的问题，恢复 spread、microprice、多档深度失衡与深度斜率的真实计算。
- 修复原生 OKX `/market/books` 外层 `data[0]` 翻译，避免请求成功后被静默降级为无盘口。
- 增加两层回归：领域特征公式使用非空盘口快照；原生适配器使用真实 envelope 验证 books/OI/basis 翻译。
- 安全边界：只恢复信号时点研究特征，不改变现役规则阈值、风控、下单或 live 状态。

## 2026-08-23 开仓准确率计划·15m 主周期收敛
- 口径：用户指定日内 15m 短线。主信号由 1H 改为已收线 15m；1H/4H 只做环境；预测、反事实标签与新交易最大持有统一为 4h（16 根）。
- 数据身份：策略版本升为 `pullback-15m-v1`；新增 `factor_trials.timeframe/horizon_hours`；因子、Brier 校准、入场/极值模型、Agent 评估和状态机都只消费当前 15m/4h 样本。旧制品 scope 不匹配时拒绝加载。
- 执行对齐：新交易写入 `strategy_timeframe=15m/max_hold_hours=4`；超时走原有 reduce-only + 撤条件单 + 账本释放 + 复盘。旧持仓字段为 NULL，不追溯强平。
- 验证门补齐：因子试验持久化 direction/symbol/regime/month 的 OOS 稳定性；入场模型新增同覆盖率 precision lift，并要求至少 4/5 折提升。
- 定向证据：15m 候选/路径/预测/因子/入场/极值/模型状态/Agent/权重/决策链共 218 项通过、0 失败；全量自动发现 35 个离线测试脚本全部通过、0 个脚本失败。compileall、参数、代码图、隔离和 AI 仓库守卫全绿。
- 数据证据：改造完成时模拟盘与 live 库的 15m/4h 候选、路径、六维平仓、有效 AI 结果、预测校准、validated 因子和 accepted 模型均为 0；只能开始积累，不能宣称准确率已提升。长期样本外 EV 未转正前不扩大预算。
- 运行边界：未启动、未重启任何实例，未操作 live，未改 1%/150/600 和交易所侧止损。

## 2026-08-23 开仓准确率计划·Agent 结果回流闭环
- 权威结果：legacy AI 判断与新 Harness 评价统一消费 `signal_outcomes` 的 15m/4h first-passage 标签，不再各自用成交 PnL 或到期现价近似；路径落库即时回填，worker 小时 sweep 幂等兜底。
- Harness 生命周期：pending 评价在真实路径到达后转为 mature，保留 TP/SL/timeout/ambiguous、MFE/MAE、saved loss、missed profit 与增量 EV；重复 Harness 调用和 pending 写入不能覆盖成熟事实。
- 记忆隔离：SQLite schema 升至 v24，`agent_memories` 区分旧 `outcome_pnl` 与标准化 `outcome_r`；legacy/Harness episodic memory 均继承 signal 的 strategy_version、15m timeframe 与 regime，并经过独立年龄门才进入检索。
- 身份修复：Harness run_id 改由 signal_id 与 prompt/model/context/schema/retrieval 版本稳定生成，入口读取候选的真实策略、周期和特征 schema；Agent 仍固定 shadow，不因本次闭环修复获得 veto 权限。
- 定向证据：Harness/评价/记忆 20 项、路径脚本 16 项、legacy Agent 脚本 17 项全部通过，0 失败；CI 同款自动发现 45/45 个离线脚本通过，compileall、AI 仓库、代码图、参数、隔离和 21 条 fix guard 全绿。未启动或重启任何实例，未写活体库，尚无真实成熟样本，因此不能宣称 Agent 已提升开仓准确率。

## 2026-08-23 开仓准确率计划·真实 SWAP 历史重放与停止裁决
- 数据边界：新增 `tools/replay_15m_research.py`，只接受 OKX `*-USDT-SWAP`，拒绝把现货当合约代理，也拒绝写 `crypto_agent.db`/`crypto_agent_live.db`；15m 已收线 K 形成候选，随后连续 4h/1m 路径结算，缺一分钟保持 missing。
- 下载韧性：公共历史行情按序列提交，页面有限重试、全局限速、错误聚合与重复回填幂等；10 个 USDT 永续、40 条标的×周期序列完成，收到 131,839 行，单序列覆盖 99.44%～100%。
- 历史结果：扫描 7,085 个时点，得到 437 个候选与 414 个完整路径；TP 131、SL 246、timeout 37。按单边 taker 0.05% + 滑点 0.05% 后，全部/long/short EV 分别为 -0.7571R/-0.4599R/-1.3204R，否决扩大预算。
- 因子与模型：41 个试验 validated=0，因子试验身份绑定数据哈希与评估版本，重复挖掘仍为 41 行；正式入场/极值训练均因无验证特征返回 `insufficient_data`，不写模型制品。
- 防泄漏：经验概率和校准增加 as-of 截止；历史重放使用固定 seed、禁用跨样本经验混合。414 条概率校准的多分类 Brier skill=-2.90%，探索极值 pinball 改善 long=-99.47%、short=-344.14%，继续 shadow/拒绝晋升。
- 验证：新增历史重放脚本 7/7、日内因子门 16/16、forecast 13/13、forecast semantics 13/13、Harness 存储 4/4；CI 同款自动发现 46/46 个离线脚本通过。compileall、AI 文档/链接、代码图、参数、隔离和 21 条 fix guard 全绿。
- 证据边界：历史候选不能充当模拟盘自然平仓、六维完整样本或有效 Agent 结果。运行库对应计数仍为 0；未启动/重启实例、未写活体库，后续只允许继续采集并等待跨月/跨 regime 证据。

## 2026-08-23 开仓准确率计划·机器完成度审计
- 问题：既有 `tools/readiness.py` 只回答实盘三盏灯，无法证明 T0～T10 的自然平仓、候选类别、因子、Brier、极值、Agent 和长期 EV 门；人工拼计数容易把独立历史研究误算成 paper 成绩。
- 实现：新增纯只读 `tools/entry_accuracy_audit.py` 和 `GET /research/readiness`，SQLite 以 `mode=ro/query_only` 打开，不迁移、不写 KV；每个门同时返回 actual/required/reason/blocker，`--require-complete` 可供自动化 fail-closed。
- 防串账：自然平仓只计当前 15m/4h `trades`，六维要求 `shadow_dims` 六项均非空；历史候选只进入候选/TP/SL/校准计数。paper、live 两库当前所有统计门仍为 0；独立研究库为候选 437、结果 414、TP 131、SL 246、校准 414，但自然平仓/六维/Agent 均为 0。
- 生命周期修复：Agent 版本从 validated 迁移到 active-veto/observing 时不再用空字典覆盖验证指标；100/30 门改为引用 config。否则状态看似晋升，审计却无法追溯增量下界证据。
- 验证：审计器 4/4（空库、历史不冒充 paper、全门可证明、封存 WAL 快照零写读取），`--require-complete` 未完成时退出 2 且快照哈希不变/无 WAL；Agent 生命周期 2/2，服务 API 43/43；完整 CI 同款 47/47。未启动/重启实例，未写运行库。

## 2026-08-23 开仓准确率计划·Paper Harness 生产接线
- 生产缺口：paper 的 legacy `ai_judgments` 正常增长，但 `agent_runs/evaluations` 为 0；根因是 Harness 调用点要求显式 `agent_model_call`，生产构造器从未注入。
- 接线：仅真实 OKX paper 且 provider key 可用时注入严格 JSON 回调，版本身份和 evidence provenance 一并冻结；FakeAdapter/离线测试禁止外部 AI，live 不启用新 Harness。
- 权限：每个通过量化基线的候选先运行 Harness shadow 留反事实 Trace，随后始终运行 legacy AI；Harness reject 不拦单，legacy reject 仍是唯一现役 AI 否决，模型异常继续回量化基线。
- 证据：Harness 端到端 10/10（含生产构造器组装）、决策链累计 45/45、legacy AI 17/17；CI 同款 47/47 脚本、compileall、AI 仓库、代码图、参数、隔离和 21 条 fix guard 全绿。
- 运行：用户只授权模拟盘。8091 重启后最终唯一 launchd 实例 PID 99395，日志为 `Agent Harness: paper shadow provider ready`；健康、空仓、账本/交易所对账均通过，原生模拟盘四类 pending 条件单合计 0、查询错误 0。8090 实盘 PID 89187 未停止、未重启、未改动。
- 当前统计：paper 自然平仓 1/60、六维完整平仓 1/30、候选 8/300、路径/校准 0、成熟 Harness 结果 0/100。接线完成不等于 Agent 有增量，必须等待新自然候选运行及 4h 路径成熟。

## 2026-08-23 固定 2:1 的开仓前胜率预测门
- 用户口径：目标不是搜索止损/止盈组合，而是每笔固定止损 -1R、止盈 +2R，并提高胜率。无成本盈亏平衡胜率为 33.33%，实际必须按双边费用、滑点和 timeout 计算更高的动态门槛。
- 历史证据：30 天真实 OKX SWAP 重放 1,690 个完整路径，TP/SL/timeout=462/1,110/118；固定 2:1 毛 EV=-0.078866R、成本后 EV=-0.969655R，多分类 Brier skill=-2.5314%，因子 validated=0。因此不能把现有 bootstrap 展示概率直接当成开仓依据。
- 落地：新增 `preopen_2to1_decision`，审计实际 RR、active 模型状态、预测 TP/SL/timeout 与成本后 EV；真实 OKX paper 只有 RR=2 且 EV 单侧 95% 下界>0 才放行。无已验证模型失败关闭，但所有拒绝候选继续结算 4h 反事实标签。
- 执行复核：发现 `stop_adj=0.2` 会把最终订单降成约 1.67:1；严格 paper 现已忽略该经验修正，成交价重锚后仍固定 1×ATR/2×ATR，避免预测标签与实际订单错位。
- 生命周期：`ENTRY_MODEL_SHADOW_ONLY=False` 只表示通过 OOS 与独立 60 候选 shadow 门的 entry 模型可自动 active；extrema 与 Agent 仍保持原影子权限。FakeAdapter 与 live 隔离，本次不触碰 live。
- 运行证据：全量自动发现 47/47 个测试脚本通过，compileall、参数、代码图、隔离、AI 链接与 21 条修复护栏全绿；最终 8091 paper PID 14247 健康、空仓、对账一致，10 个主流合约 pending 条件单合计 0。8090 live PID 89187 未变化。
- 样本闭环：严格 2:1 门会在无 active 模型时拒绝全部候选；Harness 若仍位于门后则 100/30 评价样本永久为 0。现已把无权限 Harness 前移到去重结构候选处，量化门拒绝仍留下 Trace 并在 4h 后成熟；legacy 下单二判位置与权限不变。
- 自动评价：schema v26 持久化 Harness `risk_probability/reason_codes`；按 model+prompt+context+schema+retrieval 组合版本自动汇总费用后增量 EV、95% 下界、Brier、拦亏 precision、原因与分段集中度。worker 成熟 sweep 同步登记版本，100 有效/30 reject 且增量下界为正后只自动到 validated；`AGENT_HARNESS_VETO_ENABLED=False` 与未调用 activate 共同保证不会自动获得交易权限。
- 观测：`GET /agent/evaluation` 新增 `harness` 分项，旧 legacy 指标保持兼容；定向测试覆盖 120 条成熟反事实、正增量自动 validated、风险概率/reason 持久化及服务 schema。

## 2026-08-23 自动因子候选闭环修复

- 缺口：注册表 41 项已经超过旧上限 40，自动交互剩余名额恒为 0；旧任意交互又没有经济依据，只能进入 `hypothesis_only`，无法成为开仓模型特征。
- 实现：取消无假设两两穷举，预注册 5 个信号时点可复算交互：趋势×成交量、拒绝影线×成交量、回踩质量×成交量、趋势×拒绝影线、1h 动量×成交量。统一变换函数供实时扫描、历史重放、研究提取和模型消费复用，候选上限改为 46 并在挖掘入口 fail-fast。
- 真实复核：在 10 个 OKX SWAP、30 天、1,690 条连续 4h/1m 结果上重跑 46 项；23 reject、23 insufficient、validated=0。5 个新交互全部 reject，未降低 t≥3、4/5 折、DSR/PBO、成本和集中度门，也未生成或晋升模型。
- 验证与运行：定向因子门 18/18、入场概率 18/18、重放 10/10；CI 同款完整 47/47 脚本及 compileall/AI 索引/参数/代码图/隔离/fix guard 全绿。只重启 8091 paper 到 PID 17342；健康、空仓、对账一致，交易所六类 pending 条件单 0。8090 live PID 89187 未变化。当前 paper 候选 12、路径结果 0，不能宣称胜率已提高。

## 2026-08-23 连续多档事件 OFI 影子采集
- 语义修复：旧 `ofi_dynamic` 是稀疏信号时点盘口差，保留兼容但不再标成事件流。新增 top-5 连续 L2 累加器，60 秒窗口按多档 Cont 队列事件规则计算 `sum(OFI)/sum(depth)`；同价队列消退失衡、事件数、数据年龄一起冻结到候选。
- 可用性闸门：窗口少于 10 个事件或最后事件年龄超过 5 秒时两个事件因子均为缺失，不回退到静态盘口。`signal-features-v2` 与 config hash 共同形成新候选身份，避免同一根 K 被旧 schema 去重吞掉。
- 因子门：注册表由 46 增至 50；因子评估升为 v2 并把候选宇宙写入 `trial_key`，避免 DSR 多重检验口径变化却复用旧证据。1,690 条历史路径因没有 L2 事件数据，4 个新增候选均 `insufficient_data(n=0)`；50 项汇总为 reject 22、reject_missing 1、insufficient 27、validated 0。
- 实测：纯回放/缺失/stale/试验身份共 23/23；真实 OKX ccxt.pro 公共 WS 12 秒收到 19 个盘口事件，最新年龄 15.9ms，事件 OFI 与队列消退失衡均非空。该链仅影子采样，不改变 2:1 预测门、下单、止损或 Agent 权限。
- 部署证据：只读 `/realtime/{base}` 增加 status/OFI/队列消退/事件数/年龄；最终 CI 47/47、静态护栏全绿。仅重启 8091 paper 到 PID 22631，BTC 观测实测 missing→insufficient→ready（10 事件、年龄 8.0ms）；空仓、对账一致、六类 pending 条件单 0。8090 live PID 89187 未变化。

## 2026-08-23 策略改进：候选级成本门与吸收形态审计

- 选择依据：30 天 1,690 条固定 2:1 路径中，全量成本后 EV=-0.9697R；预先定义的 `cost_r≤0.35` 子集 176 条改善到 -0.1116R，long 子集 147 条改善到 -0.0378R，但仍未转正。成本是当前策略的首要结构性约束，不能继续用训练平均值代表每笔交易。
- 修复：`execution_cost_r` 按候选自身止损距离换算双边 taker+滑点；purged walk-forward 的选样策略改为与部署一致的 Beta 收缩三分类 `EV_R` 单侧 95% 下界>0，且每折费用后净 EV 必须为正。实时预测输出 `cost_r/binary_breakeven_win_rate/baseline_p_tp`。
- 生命周期：shadow/observing 改用费用后实际 R 与训练期冻结频率基线，新增至少 30 个真实预测放行样本门；放行样本净 EV≤0 或只靠空仓提高组合 EV 时不得 accepted。
- 候选策略：`cost_r≤0.35 + 放量拒绝影线` 在全样本只有 20 条、净 EV +0.1102R，但前四时间折 3 条且全部亏损，正收益集中在最后一折；`趋势+影线+放量` 仅 12 条且全部集中在最后一折。两者证据不独立，继续作为 `wick_volume_absorption` 等因子 shadow，禁止直接启用。
- 运行证据：最终只重启 8091 paper 到 PID 19291，8090 live PID 89187 不变；首批 3 个到期候选由 worker 自动完成 `scanned=3/settled=3/missing=0/errors=0`，结果 TP 2/SL 1，并同步形成 3 条校准与 3 条有效 legacy Agent 反事实（reject 2）。全量 47/47 脚本、全部静态护栏再次全绿；paper 仍空仓且六类 pending 条件单为 0。

## 2026-08-23 策略改进：资金费净 EV 与六维因子物化

- 成本口径：统一 `entry_probability`、因子门、模型训练/生命周期、Agent 评价与研究报告，按候选止损距离扣除双边 taker、滑点和方向不利的预计资金费；资金费按 4h/8h 比例折算，潜在收入保守记 0。旧成本版本模型制品拒绝加载。
- 历史可得性：重放器新增 OKX 已结算资金费分页、重试、幂等和 `--funding-only`；10 个 USDT SWAP 各取得 90 条，共 900 条。1,712 个候选全部用事件时点之前最近两次费率和当时横截面分位，未来费率由回归测试证明不可见。
- 数据语义修复：六维子分此前只在 `shadow_dims`，注册因子矩阵仍是空值；统一物化函数现把 wick/depth/trend/volume/funding/book 本身和五个理论交互一起输出，历史/实时/训练/推理共用。
- 真实裁决：30 天 1,690 条路径的毛 EV=-0.078866R；新净 EV=-0.977041R（long=-0.896368R、short=-1.065521R），多分类 Brier skill=-2.7536%。v3 共 50 项：27 reject、23 insufficient、validated=0；资金费相关项已有完整值但仍未过门，继续 `stop_no_promotion`，不改策略阈值、不扩大预算。
- 验证与部署：CI 同款 47/47 脚本，compileall、AI 文档/链接、参数、代码图、隔离和 21 条 fix guard 全绿。仅重启 8091 paper 到 PID 28084；健康、空仓、账本与交易所对账一致，六类 pending 条件单 0 且查询错误 0，BTC 连续 L2 为 ready（24 事件、年龄 58.2ms）。8090 live PID 89187 未变化。只读审计为候选 19、路径/校准/有效 legacy Agent 各 5（TP 3/SL 1/timeout 1，reject 4），自然平仓仍 1/60、六维自然平仓 1/30、成熟 Harness 0。

## 2026-08-23 历史 5m 波动与横截面行情因子补齐

- 缺口：注册表已有 5m RV/vol-of-vol/HAR、BTC beta/残差动量、横截面排名、市场宽度和相关性集中度，但历史重放未提供这些可由现有 1m/多标的 15m 数据因果重建的字段；实时相关性集中度还被固定为缺失，5m RV 公式也误用了整段 24h 窗口。
- 修复：新增历史/实时共用纯变换；1m 只聚合事件时点前已完成且连续的 5m 桶，横截面只取所有标的同一根已收线 15m；1h RV、vol-of-vol、HAR-RV、EMA20 宽度、相关矩阵首特征值占比、动量排名、BTC beta/残差统一计算。特征 schema 升为 v3，因子评价升为 v4。
- 防复现：重放管线版本与 bootstrap 随机种子版本解耦；特征管线升级不能仅因 seed 改变就让 Brier 指标漂移。回归覆盖未来 1m 排除、同收线横截面、少于 5 标的缺失、实时接线和 seed 稳定性。
- 真实裁决：1,712/1,712 个候选取得横截面，1,703/1,712 取得 5m RV；50 项变为 reject=34、reject_missing=1、insufficient=15、validated=0。新解锁 7 项均 reject，HAR-RV 以 10.24% 缺失触发 reject_missing；固定 seed 的多分类 Brier skill=-2.6286%，净 EV 仍为 -0.977041R。因此只增加可证伪范围，不晋升因子、不训练模型、不扩大预算。
- 架构修订：先接入“行情权重 → 策略候选 → 固定 2:1 概率/成本门”的 shadow 元策略。首版只用等权理论轴与 softmax 归一，明确未校准；低置信度、低间隔、disorder 或未实现策略默认 abstain，输出始终 `has_execution_authority=false`。该批次先让 A_pullback 冻结路由证据；随后同日批次已用 schema v27 把 B_breakout 接入共同 15m/4h 标签链，见下一节。

## 2026-08-23 行情优先路由、策略隔离与技术状态因子

- 策略身份：schema v27 为 `signal_samples` 增加 `strategy_id`，配置哈希/候选版本同时包含策略；A/B 同币同方向同 K 可并存。因子挖掘、入场/极值模型、经验概率、校准与完成度审计默认只消费 A，B 不得污染 A 的 300 条门。
- B 共同标签：`B_breakout` 从旧 1H 观察改为已收线 15m，且在 A 的任何额度/冷却/model/AI `continue` 前独立留样；进入同一 4h/1m 首触结果表后立刻标记 shadow rejected，仍不调用预测、Agent 或执行。旧 235 条 hypothetical 不追溯冒充新样本。
- 行情特征：新增布林带宽分位/%B/squeeze、ADX/DMI、Kaufman 效率比、VWAP/ATR 距离/穿越率、量能 z-score，以及 RV/HAR、squeeze×volume 交互；历史/实时/B 路由复用同一纯变换。注册表 50→61，多重检验候选宇宙同步更新，不复用旧 trial 身份。
- 真实裁决：候选/路径仍精确为 1,712/1,690，TP/SL/timeout 与净 EV 均未漂移。61 项为 reject=44、reject_missing=2、insufficient=15、validated=0；11 个新增项无一晋升。`vwap_crossing_rate` 虽 4/5 折且净 spread +0.0911R，但 t=1.4465、DSR=0、筛后 EV 仍负；继续观察。概率 Brier skill=-2.6286%，正式入场/极值模型仍不生成。

## 2026-08-23 最终回归与模拟盘部署收尾

- 测试夹具随模型制品新增 `strategy_id` 后同步更新；决策主循环 50/50 通过。CI 隔离命令明确使用 `PYTHONPATH=.:lib`，从头执行自动发现的 48 个 `tests/test_*.py`，最终 `FULL_SUITE_OK scripts=48`、失败 0。
- 只重启 8091 paper：PID 28084 → 36809；8090 live 始终为 PID 89187。重启后健康、空仓、journal/交易所持仓对账一致，数据库 `PRAGMA user_version=27`，`strategy_id` 列存在。
- 当前 A 候选 24、成熟路径 6（TP 4/SL 1/timeout 1）、校准 6、有效 legacy Agent 6（reject 4）；自然平仓 1/60、六维自然平仓 1/30、成熟 Harness 0。B 新增 1 条 `signal-features-v4` 候选、成熟路径 0，落库为 `shadow/rejected`，不污染 A readiness。
- BTC 连续 L2 在重连后由 1 事件 `insufficient` 恢复为 34 事件 `ready`，事件年龄 179.5ms，多档 OFI 非空。部署前后模拟账户六类 pending 条件单总数均为 0、查询错误 0；预算扩大锁继续关闭。

## 2026-08-23 B 突破独立历史重放与策略级证据隔离

- 隔离修复：schema v28 为 `factor_trials` 增加 `strategy_id`；试验键、因子挖掘、入场/极值模型训练和制品身份全部按策略隔离。B 的突破窗口与量比只进入 B 配置身份，修改 B 不会使 A 候选身份漂移。
- 公平重放：replay v4 在相同 30 天真实 OKX SWAP 市场库上同时生成 A/B，并为两者冻结相同 causal 行情权重/路由。A 仍为 1,712/1,690，原标签与损益零漂移；B 为 1,973/1,931。
- 证伪结论：B 的 TP/SL/timeout=673/1,177/81，毛 EV=+0.101312R，但逐候选成本后净 EV=-0.756528R；路由命中的 334 条净 EV=-0.461477R。A/B 路由命中合并 385 条净 EV=-0.513970R。A/B 各 61 个因子 validated=0，Brier skill 分别为 -2.6286%/-4.3575%。
- 权限边界：行情路由和 B 继续 shadow，严格 2:1 门、风险预算和现役 Agent 权限不变；历史证据不抵扣 paper 的 60/30/100 自然样本门。

## 2026-08-23 每日 SWAP 候选池 CCXT 场所修复

- 部署后证据暴露：每日扫描只得到人工追加的 9 个美股/公司代币，且成交额全为 0，阶段 1 全部剔除后静默回退固定五币。根因是 CCXT `fetch_tickers()` 无参数默认只取 SPOT，而适配器随后按 SWAP symbol 过滤。
- 修复：目标场所显式传 `instType=SWAP/SPOT`；批量 ticker 缺失 `base` 时从 markets/symbol 恢复标的名；USDT 成交额归一仍封装在 exchange 层。新增离线测试同时锁定请求场所、缺 base 映射和 SWAP `volCcy24h×last`/SPOT `quoteVolume` 语义。
- 美股边界：9 个沙盘实测永续仍由 `STOCK_SWAP_TOKENS` 强制并入观察池，但和加密币共同经过实际 venue、流动性、1H/4H 趋势及 ATR 门，不获得特殊下单权。
- 最终证据：隔离扫描与部署后扫描都得到 64 个观察标的、60 个流动性合格、35 个 1H 趋势/ATR 合格、30 个 4H 共振，原子写入 12 个非回退候选；当天美股未进前 12。全量 48/48 和全部静态护栏复跑为绿；仅重启 8091 paper 到 PID 48502，schema v28、健康、空仓、对账、BTC L2 ready（54 事件）和六类 pending=0 均通过，8090 PID 89187 未变化。

## 2026-08-23 模型制品与 readiness 策略隔离补完

- 审计缺口：候选/标签/因子虽已按 A/B 隔离，但模型表、SQL 选择、生命周期父子替换、默认回滚、预算锁和 readiness 的因子/模型门仍可能跨策略串证据。
- 修复：schema v29 将 `strategy_id` 提升为模型制品一等列；训练、入场/极值推理、在线 conformal、shadow/observing、父模型选择、默认回滚、预算判断和完成度审计统一带策略 scope。观测输出同时显示模型/因子策略。
- 回归：B-only validated 因子、kept entry 与 accepted extrema 均不能点亮 A readiness；A/B 同方向 active 模型按候选策略各取自己的制品，不再出现“较新的 B 遮挡 A”。
- Combo 口径：7 个有经济依据的预注册二阶/状态交互各自接受完整因子门；多个 validated 因子才会作为同一特征向量进入开仓概率与极值模型的 purged walk-forward 联合评价。禁止同数据集无假设穷举全部两两/三三组合；当前 validated=0，不生成“最佳组合”。
- 机器证据：新增 Combo 联合特征向量回归与 A/B active 模型互不遮挡回归；全量 48/48 个独立测试脚本通过，compileall、AI 仓库、参数、代码图、测试隔离与 21 条 fix guard 全绿。
- 运行证据：只重启 launchd 托管的 8091 paper，最终唯一 PID 57060，schema=29，健康、空仓、账本/交易所一致；BTC 连续订单流恢复 ready（90 事件）。部署前后六类 pending 条件单均为 0、查询错误 0。8090 live PID 89187 未停止或重启。
- 当前边界：A 候选 26、路径/校准/有效 legacy Agent 结果各 8（TP 4、SL 3、timeout 1），自然平仓 1/60、六维自然平仓 1/30、Harness 成熟 0、validated 因子与模型均为 0；长期成本后 EV 未转正，预算扩大锁继续关闭。

## 2026-08-23 A/B 自动研究生产调度补完

- 缺口：B 已进入独立候选/标签表，但 worker 每日只调用默认 A 的因子与模型训练；同时日内研究异常被空 `except` 吞掉，且运行时间已前移，一次失败会静默停 24 小时。
- 实现：新增分策略研究周期，A/B 各自运行 61 项因子、long/short 概率模型和 long/short 极值模型，再统一推进生命周期。失败写 `/error` 和隔离 `engine_errors`，按配置 15 分钟退避重试；trial key 与模型 ID 保证重试幂等。
- 离线证据：服务生产组装测试新增 4 项，覆盖 A/B 调用集合、双方向概率/极值、失败可观测与 15 分钟重试；该脚本 48/48，全量仍为 48/48 个独立脚本。compileall、参数、代码图、隔离、AI 仓库和 21 条 fix guard 全绿。
- 生产证据：只重启 8091 paper 到 PID 57060。首次日志实际返回 A/B 各 61 项，四类模型均按策略调用，lifecycle=[]、errors=[]；A 有 26 候选/8 路径，B 有 2 候选/0 路径，两边均因数据和 validated 特征不足停在 insufficient。服务健康、空仓、对账一致、六类 pending=0；8090 PID 89187 未变化。

## 2026-08-23 自然平仓与 Agent 版本策略隔离补完

- 审计缺口：A/B 候选、因子和模型已隔离，但 `trades` 无 `strategy_id`，自然平仓只按 15m/4h 汇总；`agent_versions` 又取全局最新，B 的证据未来可能误点亮 A 的 60/30 或 Agent 增量门。
- 实现：schema v30 为交易与 Agent 版本增加策略归属和复合索引，旧数据安全默认 A；开仓台账与 `/journal` 显式输出策略。`audit_status(..., strategy_id=...)`、CLI、`GET /research/readiness`、因子列表、模型快照与 Agent 生命周期按策略过滤，未知策略 fail-closed；展示版本复用候选采样器的配置身份哈希。
- 回归：新增 A 59 笔+B 1 笔不能凑成 A 60 笔、B Agent 版本不能点亮 A、未知策略返回 422、journal 写入策略身份等证据。最终全量自动发现 48/48 个脚本通过；compileall、AI 文档/链接、参数、代码图、测试隔离、21 条 fix guard 与 diff 检查全绿。
- 运行证据：只重启 launchd 的 8091 paper 到最终 PID 61829，schema=30，健康、空仓、对账一致、`/error` 为空，BTC L2 ready（37 事件）；部署前后六类 pending 条件单均为 0、查询错误 0。8090 live PID 始终为 89187。A 当前仍为自然平仓 1/60、六维 1/30、候选 26/300、路径/校准/有效 legacy Agent 各 8、validated 因子和模型 0；B 为候选 2、路径/自然平仓 0，预算扩大锁保持关闭。

## 2026-08-23 跨配置候选伪重复修复

- 缺口：`strategy_version` 变化会正确保留新审计快照，但同币、同方向、同 15m K 的不同配置行仍被因子、模型和 readiness 当成独立观察。生产 A 的 26 个 signal_id 中实际只有 23 个自然市场机会，直接计数会构成伪重复并夸大显著性。
- 实现：schema v31 新增动态只读 `signal_samples_canonical` 视图，以 `strategy_id+symbol+direction+timeframe+kline_ts` 为自然实验单位选择最新快照。原始表和每版本结果不删除；因子挖掘、概率/极值训练、shadow 生命周期、经验预测、校准、Agent 评价、研究报告与完成度统一消费 canonical 视图。
- 回归证据：新增“同 K 多配置原始保留 2 条、canonical 只计 1 条”和“原始 300 行含 1 条版本重复，训练门仍为 299”断言；相关定向 124 项、全量 48/48 个独立脚本以及所有静态门通过。
- 运行证据：仅重启 8091 paper 到最终 PID 63433，数据库 schema=31；原始 A/B=26/3、独立 A/B=23/3，readiness 显式返回 `raw_candidate_snapshots=26`、`duplicate_version_snapshots=3`、A 独立候选 23/300。服务健康、空仓、对账一致、BTC L2 ready（31 事件）、`/error` 为空，部署前后六类 pending=0；8090 PID 89187 未变化。
- 持续闭环证据：18:31 新增 1 个真实 A 路径，结算器返回 `scanned=1/settled=1/missing=0/errors=0`；校准同步到 9/30，Harness 首条自然结果进入 mature，A 因标签身份变化自动产生新一轮 61 项 canonical 因子试验。A 当前 TP/SL/timeout=4/4/1、有效判断 9/100；B 独立候选自然增长到 6、尚无到期路径。无到期未结算候选、无研究或引擎错误。

## 2026-08-23 数据看板决策化改版

- 可读性问题：原总览以快照数、扫描总数和历史全量盈亏为主，无法直接回答“现在是否应开仓、为何空仓、当前 15m 策略和 Agent 到底成熟到哪一步”。
- 改版：`data-dashboard` 首页更名为“决策中心”，只读聚合 `/status`、`/health`、`/research/readiness`、`/agent/evaluation`、模型与预测校准接口；首屏给出当前动作结论、实时风控、权益/仓位、自然平仓 1/60、有效 Agent 9/100 和预算锁。
- 口径：固定 2:1 的理论盈亏平衡胜率 33.3% 明确标为未扣成本理论值；TP/SL/timeout 明确标为候选路径而非成交收益；历史持仓曲线折叠为次级信息。
- 能力页：自然平仓、六维完整平仓、去重候选、极值校准、有效 Agent 和 Agent reject 改为中文进度条；因子、模型、Agent 增量均显示“未证明”，长期样本外 EV 未转正时预算保持锁定。
- 验证：临时 8900 只读实例使用真实 paper 数据完成 DOM 与视觉检查；Python/JavaScript/HTML 语法通过，前端控制台 0 error/warning，paper 决策摘要与 overview 接口通过。

## 2026-08-23 Agent 主动候选提案 Shadow

- 能力：新增 `C_agent_proposal`，每根已收线 15m K 对候选池 Top 5 最多批量调用一次，模型只返回 0～2 个标的/方向/置信度/理由/证据 ID；无清晰机会允许空列表。
- 确定性门：AI 不提供价格。系统用收线价和 ATR 计算 1R 止损、2R 止盈，验证候选成本、扣费保本胜率和 active 概率模型 EV 95% 下界；当前无 C active 模型时记录 `no_validated_active_model`。
- 权限：仅真实 OKX paper 装配 provider，live 与 FakeAdapter 不装配；有效提案固定 `shadow/rejected`、`execution_authority=0`，不调用开仓、通知或交易所接口。
- 数据：schema v32 新增 `agent_proposal_runs/agent_proposals`；几何有效提案进入共同 4h/1m 路径结算，因子/概率/极值研究按 C 策略独立运行，不污染 A/B。
- 观测：新增只读 `GET /agent/proposals`，显示运行健康、2:1、概率门、signal_id 与成熟 TP/SL/timeout 结果。
- 工程修复：初版出现 `decision → engines` 反向依赖，改为引擎层显式注入 sample recorder 后代码图恢复单向。
- 最终证据：专项 7/7、服务接口 52/52、完整自动发现 49/49 个测试脚本、compileall、AI 文档/链接、参数集中化、代码图、隔离、21 条 fix guard 与 diff 检查全部通过，失败 0。部署前 OKX 模拟盘完成迷你开仓→SL/TP→取消→平仓冒烟，全部 `sCode=0`；仅重启 8091 paper，PID 63433 → 75913，schema=32，健康、空仓、账本/交易所对账一致、`/error` 为空，六类 pending 条件单合计 0。`/agent/proposals` 返回 `shadow_only=true`、`execution_authority=false`，启动日志确认 paper shadow provider ready；8090 live PID 89187 始终未变化。

## 2026-08-23 加密/美股每日候选池拆分

- 问题：两类资产原先共用一个成交额排名和 `WATCH_N` 截断，美股沙盘合约即使通过全部硬门，也可能被高成交额加密币挤出每日候选。
- 实现：流动性、1H 趋势/ATR、4H 共振仍完全共用同一组保守闸门；通过后按显式 `is_stock` 分类，成交额分数在类内计算，加密/美股各自取 `WATCH_N`。加密池空时仅回退主流加密币，美股池空时保持空。
- 观测：引擎保留 `crypto_watchlist` / `stock_watchlist` 两份状态，`GET /watchlist` 和 `POST /scan/daily` 分池返回；兼容 `items` / `candidates` 并集字段。执行扫描遍历两池并集，但仍共用原有单笔、组合总敞口、冷却和止损闸门，没有增加下单权限。
- 证据：新增离线用例在 `watch_n=2` 时同时保留 2 个加密与 2 个低成交额美股候选，落库后分类不丢失；完整自动发现 49/49 个测试脚本通过，compileall、AI 仓库、代码图、参数、隔离、21 条 fix guard 全绿。OKX 模拟盘公开行情实扫为 64→60→35→30，原子写入加密 12 个 + 美股 1 个（CRCL）。当时 8091 paper 未运行，本批次未启动引擎、未下单、未触碰 live。

## 2026-08-23 接口优先与功能分层整改

- 审计：发现服务层直达 `DirectionalTrader` 内部协作者并散写 SQL、生产路径依赖 CLI `tools`、存储层反向 import 决策契约、引擎默认协作者未统一使用实例数据库，以及代码图遗漏 `storage/interfaces` 导致假绿。
- 分层：新增中立 `interfaces` 契约层；服务状态与控制统一走 `TradingRuntimePort`/`DirectionalRuntimeAPI`，决策能力统一走 `decision.api`，HTTP 查询走 `storage.query_api`，台账/持仓/运行错误与异常事件走独立 repository。
- 装配：`DirectionalTrader` 支持 journal、决策、经验、账本、风控、通知与事件记录器注入；默认实现继续兼容，但统一绑定实例 `db_path`，Fake/Stub 可替换。
- 守卫：代码图纳入 `storage/interfaces`，新增服务直连 `storage.db`、核心 import `tools.*`、跨包私有符号三类拒绝项；专项接口测试同时验证 Protocol、行为快照和 AST 边界。
- 行为边界：未修改任何策略参数、风险闸门、下单语义或 HTTP 下单权限；本轮只做离线验证，不启动或重启 paper/live 实例。
- 实测证据：接口边界 17/17、服务接口 53/53；自动发现 51/51 个独立测试脚本通过、失败 0。compileall、AI 入口/文档、参数集中化、代码图及 selftest、测试隔离、21 条历史修复护栏和 diff 检查全部通过。

## 2026-08-23 行情终值、每日回补与质量审计闭环

- 审计结论：旧 `klines` 抽样对比 OKX 官方终值时，1m 每 99 根有 76～80 根不同，15m 有
  94 根不同。根因是未收线 K 首次快照配合 `INSERT OR IGNORE` 永久冻结；旧表缺来源、场所、
  时区、收线和 as-of，正式登记为 `legacy_unverified`，保留但退出当前 15m 研究默认路径。
- 严谨数据集：同一 `market.db` 新建 `klines_v2`，仅写 OKX `USDT-SWAP`、`confirm=1`、UTC
  终值；保存 close time、采集时间、as-of 和原始值哈希，校验 OHLCV 不变量，并用终值 UPSERT
  支持修订与幂等。15m 重放一旦发现 v2 就不再静默回退旧表，健康检查也改查 confirmed v2。
- 闭环调度：采集失败不再吞异常，运行指标落 `market_collection_runs`；守护进程每天回补前一
  UTC 日五个周期并精确审计。源端缺失时间槽做独立定点二次查询，仍不存在才进入
  `market_data_gaps`，不补零、不插值；交易所后续补发终值会原子撤销缺口。备份脚本失败改为
  非零退出，避免守护进程把“上传失败”报成成功。
- 生产实测：只重启 `com.okx.collect` 行情作业，未重启或操作 paper/live 交易引擎。首轮五周期
  增量均为 89/89 序列成功、非法行 0，当前未收线 K 全部排除。2026-08-22 全量对账为
  445/445 序列成功、坏行 0；89 个标的五周期未解释缺口 0。9 个美股/商品合约的 1m 共 22 个
  时间槽经历史接口二次确认缺失，显式保留，陈旧缺口 0。官方抽样复核 BTC/ETH/AAVE/XRP/
  DOGE 的重叠 confirmed 1m 行均为 mismatch=0；边缘 1～2 行差异是采集时点后的新收线，不是值错。
- 回归证据：数据质量专项 8/8、15m 重放 12/12；自动发现全量 53/53 脚本通过、失败 0。
  py_compile、代码图、参数集中化、测试隔离、AI 文档检查和 diff 检查全绿。COS 本次实际上传
  仍失败，现已正确显示失败；不把本地完整性结论夸大成云备份成功。

## 2026-08-23 入场与 Agent Harness 精准率证据链加固

- 基线裁决：A_pullback 当前独立 15m/4h 路径 23 条，TP first=4、SL first=18、timeout=1，
  TP-first precision=17.39%，毛 EV=-0.3931R，平均交易成本约 0.2686R，净 EV=-0.6617R；long
  3 条全部 SL，short 20 条净 EV=-0.5907R。当前证据明确反对“已经提高胜率”的结论。
- Harness 基线：成熟旧结果 16 条中 approve/abstain/reject 都落在亏损路径；当前完整旧版本仍没有
  30 个 reject，且旧行缺输入快照、结构化证据和 provider 成本，不能用于新版本晋升。
- 可重放链：schema v33 为 `agent_runs` 增加完整版本、canonical 输入快照、跨版本 evidence hash、
  confidence、证据/缺失信息、cache token 与美元成本。champion/challenger 只比较相同 evidence hash；
  GET 评价保持只读。provider 用量缺 cache 明细时按 cache miss 保守计价。
- Harness 门：当前版本按策略独立计 100 mature/30 reject；晋升同时要求费用后增量 EV 单侧 95% 下界
  大于 0、`saved_loss > missed_profit + model_cost`、Brier 不劣于频率基线、Trace/概率/reject 证据覆盖
  100%、最大单段占比不超过 0.8。生命周期仍最多自动到 validated，`veto=false` 未改变。
- 入场模型门：同一 15m K 的跨币批次不可拆分 train/test；5 折 purged walk-forward 用现役连续分
  Top-K 做完全同覆盖率对照。除 Brier skill>5%、至少 4/5 折稳定外，新增至少 30 个 OOS 放行样本
  和实际费用后净收益 95% 下界>0；标签、成本或特征修正会改变完整数据哈希，不复用旧制品。
- 运行修复：全量测试发现 CI/测试/ccxtpro 入口仍可注入旧 Python 3.9 `lib/`；已统一到 Python 3.12
  `.venv` 并增加 AST 护栏。只重启 `com.crypto.paper`，最终 PID 44390；`com.crypto.agent` live PID
  90574 未变化。paper `/health=ok`，最终代码重启后观察 63.5 秒、心跳 3.1 秒，空仓、账本敞口 0、近 5 分钟
  引擎错误 0、SQLite quick_check=ok，`/reconcile balanced=true`、`/error` 为空；OKX 模拟盘六类
  pending 条件单（conditional/oco/trigger/move_order_stop/iceberg/twap）均为 0；Harness
  `shadow_enabled=true`、`veto_enabled=false`。
- 工程证据：自动发现 53/53 个离线测试脚本通过、失败 0；compileall、AI 仓库、代码图、参数集中化、
  测试隔离、23 条 fix guard 与 diff 检查全绿。当前仍只有候选 27/300、TP 4/60、SL 18/60、
  当前可评价旧证据版本 Harness 3/100 且 reject 0/30；新 context-v3 challenger 尚无到期成熟样本。
  因此结论是“证据不足，继续 paper shadow”，不调阈值、不扩大预算。

## 2026-08-23 Harness 验证后自动否决闭环

- 用户授权：`AGENT_HARNESS_VETO_ENABLED=True` 表示验证通过后可直接接入，但不等于当前版本立即
  获权；无 active 入场模型、量化门拒绝或 Harness 证据不足仍保持空仓。
- 决策口径：prompt 升为 `harness-risk-v2-loss-calibrated`，要求风险概率表示未来 4 小时扣费后
  亏损概率；仅风险概率与信心均不低于 0.70 的结构化 reject 计入晋升和实际否决，中间区间 abstain。
- 生命周期：策略、模型、prompt、context、schema、retrieval、工具和价格口径共享一个版本函数；
  100 mature/30 qualified reject 及费用、Trace、校准、证据、分段、净 EV 下界全部通过后，已授权
  版本从 validated 自动进入 active-veto。任何版本字段不匹配都继续 shadow。
- 执行边界：Harness 仍只否决不放行；早期候选调用继续保证 2:1 门拒绝时也有反事实样本，但返回值
  只在阈值、2:1 active 模型、经验与风险等硬门全部放行后才消费。未改变 1%/150/600、交易所侧
  止损、模拟盘边界或 HTTP 禁止下单约束；扫描器还必须显式传入 paper 授权，live 固定 shadow。
- 专项证据：策略核 5/5、生命周期 5/5、Harness 端到端 11/11、增量评价 10/10、主决策链
  53/53，失败 0；覆盖未晋升 shadow、低置信 reject 不拦、同版本 active-veto 拦单和 legacy 权限隔离。

## 2026-08-23 confirmed 终值 30 天重放复核

- 数据重建：从 OKX 公共历史接口为 BTC/ETH/SOL/XRP/DOGE/LINK/ADA/AVAX/BNB/LTC 十个
  USDT SWAP 下载独立研究库；仅保留 `confirm=1`。共 469,745 根 K、900 条历史资金费、4,720 次
  请求、错误 0；1m 覆盖 99.94%～100%，15m 99.97%～100%，资金费/横截面覆盖 100%。
- 公平重放：相同市场时点与 4h/1m 首触口径下，A 产生 1,695 候选/1,648 完整路径，B 产生
  1,994 候选/1,940 完整路径；独立库标记 `research_only=true`，不抵扣自然 paper 成熟度。
- A 裁决：TP/SL/timeout=450/1,084/114，TP-first 27.31%，毛 EV -0.0827R、成本后
  -0.9598R；61 因子为 reject 46/insufficient 15、validated 0，多分类 Brier skill -2.44%，
  概率与极值模型均不生成。
- B 裁决：TP/SL/timeout=682/1,178/80，TP-first 35.12%，毛 EV +0.1082R，但成本后
  -0.7359R；61 因子为 reject 35/insufficient 26、validated 0，多分类 Brier skill -4.12%，
  继续 shadow，不因毛 EV 为正而忽略成本。
- 成本候选复核：预先关注的 B 多头 `cost_r≤0.35` 在五个顺序时间折中净 EV 分别为
  -0.3522/+0.2930/无样本/-0.1057/+0.1000R，仅 2 折为正；`≤0.50` 也只有后 2 折为正。
  正收益不稳定且近期集中，禁止把局部窗口改成硬成本门或直接生成 active 入场模型。
- 结论：confirmed 终值修复没有证明现有 A/B 具备成本后正期望；维持 2:1 fail-closed、模型空、
  预算锁定。Harness v2 只继续自然 paper shadow，达到 100 mature/30 qualified reject 并通过完整
  增量门后才会按用户授权自动进入 paper active-veto；live 固定 shadow。

## 2026-08-23 Harness v34 活体迁移修复

- 现场证据：paper 的 A 候选在 22:48～23:35 连续新增 16 条，但 `agent_runs` 仍停在旧 v1 的
  21 条。对活体库做只读备份并注入离线假模型复现：Harness 返回 completed，最终却没有 run；解除
  静默捕获后得到 `sqlite3.OperationalError: agent_runs has no column named evidence_hash`。
- 修复：schema v34 对已标 v33 的老库幂等补齐完整 replay evidence 列；图的 record 节点不再吞掉
  Trace 错误。持久化失败时返回 baseline pass、`veto=false` 并输出明确错误，禁止无审计否决。
- 边界：只修研究审计与 fail-safe，不修改入场阈值、1%/150/600 风控、止损、预算或 live 权限。
- 部署证据：完整自动发现 53/53 个测试脚本、compileall、AI 仓库、代码图、参数、隔离和 23 条
  fix guard 全绿。仅重启 8091 paper 到 PID 65281；8090 live PID 90574 未变化。活体 schema=34、
  quick_check=ok，Harness v2 新增 4 条 completed/replayable/evidence 完整 Trace 和 4 条 pending 评价，
  未再出现 Trace 持久化告警。paper 健康、心跳 0.7 秒、空仓、对账一致、`/error` 为空；六类
  pending 条件单均为 0。入场模型仍为空、预算扩张仍锁定，不据此宣称胜率已提高。

## 2026-08-23 Harness v3 反事实市场证据口径

- v2 活体诊断：4 条不同 A 候选均为 abstain，风险/信心固定 0.55/0.60，缺失理由全部锚定空入场模型；
  Trace 已完整，但这种标签没有分辨率，也不可能积累 30 个 qualified reject。
- v3 单变量变更：只改 prompt 身份与语义，不改模型、context、0.70/0.70 reject 门、量化基线或风险预算。
  明确忽略 `no_validated_active_model`、未校准预测和 route abstain 等治理元数据，只按冻结市场证据做
  反事实亏损概率标注；证据不足仍 abstain，Agent 仍只能否决、不能放行。
- 本地证伪：同一 30 天终值候选把固定 2:1 风险距离扩到 0.75～4 倍，A/B 多空四组扣费后 EV 仍全部
  为负；B 多头最好为 -0.087R，禁止改止损。预声明 11 特征本地 logit 对 B 多头的 10%～30% 覆盖
  也全部负 EV，且 precision 不优于同覆盖率现役分数，禁止生成模型。外部历史 replay 因数据出站
  未获明确授权而停止，不把缺失的模型调用结果伪装成证据。

## 2026-08-24 Harness 概率分辨率硬门

- 新增版本级 `probability_mean/std`；100 条自然成熟样本即使 Brier 不差于频率基准，只要概率标准差
  小于 0.03，仍以 `probability_resolution_too_low` 拒绝晋升。active-veto 观察期出现同类退化则回滚。
- 目的：防止 v2 的固定 0.55/0.60 模式，或任何恰好贴近总体损失率的常数概率，伪装成“已校准”。
  不改变 0.70/0.70 reject 门、样本门、费用后增量 EV 门或执行权限。
- 验证与部署：生命周期/评价/增量审计专项全部通过，完整自动发现 53/53 个测试脚本及 AI 入口、
  代码图、参数集中化、测试隔离和 23 条修复护栏全绿。仅重启 8091 paper 到 PID 71115；健康、
  空仓、未熔断、账本/交易所对账一致、`/error` 为空，OKX 模拟盘六类 pending 条件单合计 0；
  8090 live PID 90574 未变化。当前模型仍为空，A 自然候选 47/300、路径 25、当前完整 Harness
  3/100 且 reject 0/30；v3 尚无自然运行，继续保持禁止下单和预算扩大锁关闭。

## 2026-08-24 B_breakout Harness 独立影子采样

- 先证伪一个预声明本地组合门：A 的 `cost_r≤0.35` → `high_vol` → 训练折 VWAP crossing 中位数
  三层筛选，五折只有最后一折为正；最深层 75 个样本中 58 个集中于最后一折，前四折负收益或无
  样本。汇总 +0.313R/TP-first 46.7% 是近期段主导，禁止上线、禁止生成模型。
- 接线改进：A/B 去重结构候选现在复用同一个 Harness 调用方法。B 仍沿共同 4h 标签链成熟，但
  lifecycle/Trace/evaluation 以 `B_breakout` 独立计数；调用固定 `allow_veto=False`，即使未来 B 的
  Agent 版本达统计门也不获得执行或否决权限。
- 离线行为证据：构造“B 触发、A 不触发”行情，B 候选产生一条当前 prompt 的 `shadow_reject`
  Trace 和一条 pending evaluation，同时 fake orders=0、journal=0；策略 B 脚本 32/32 通过。
- 完整验证与部署：53/53 个测试脚本以及 AI 入口、代码图、参数集中化、测试隔离和 23 条修复护栏
  全绿；仅重启 8091 paper 到 PID 75159，健康、空仓、未熔断、对账一致、`/error` 为空，六类
  pending 条件单合计 0；8090 live PID 90574 未变化。部署时 v3 已自然产生 A 两条 abstain，均为
  0.55/0.60；样本仍太少且概率尚无分辨率，B 尚无新结构候选，因此不宣称精准率改善，模型与预算锁
  继续保持关闭。

## 2026-08-24 Harness v4 语义修复门

- 触发证据：v3 自然 A 两条均为 0.55/0.60 abstain，且 `insufficient_evidence` 对应的
  `missing_information` 为空，abstain_reason 仍引用已禁止的模型就绪/预测校准治理状态；因此 v3
  不能继续作为权威实验身份。
- v4 只改变输出质量控制：结构合法后继续验证缺失证据一致性、治理元数据隔离和 reject evidence_id
  锚定。首次语义失败把原响应与精确违规原因放入同一冻结候选的修复 prompt，最多重试 1 次；两次
  token/cache/美元成本和延迟累计，MODEL step 以 retry_count=0/1 分别持久化。
- 权限边界不变：修复成功才是 completed；第二次仍违规则 schema_error、baseline_pass，不能形成
  reject 或有效成熟样本。A/B 策略身份、0.70/0.70、100/30、概率分辨率、费用后增量 EV 及
  paper-only veto 门全部保留。
- 离线证据：53 个 `tests/test_*.py` 脚本当场全量通过（53/53、红 0），并通过
  `test_ai_repo_check`、`ai_repo_check`、`params_lint`、`test_isolation_lint`、`fix_guard`、
  `code_graph --check` 与 `git diff --check`；代码提交 `f1fb9fe`。
- 模拟盘部署证据：仅重启 `com.crypto.paper`，PID 75159→79777；8090 现役进程 PID 90574
  未变化。`/health` ok、未暂停，`/status` 空仓/未熔断/敞口 0，`/reconcile` balanced，
  `/error` 为空，`/models/entry` 仍为 0 个且 `budget_expansion_allowed=false`；OKX 模拟盘
  conditional/oco/trigger/move_order_stop/iceberg/twap 六类 pending 均为 0。
- 自然观察：00:42 一轮 19 个候选标的全部无回踩确认信号，因此本轮没有 v4 自然 Trace，也没有
  下单；不以人工触发或历史行情外发凑样本。候选结算链同期完成 2 条，研究日志的第二训练口径中
  A 21→22、B 16→17；两者仍为 `insufficient_data`，模型生命周期为空，继续保持空仓与预算锁。

## 2026-08-24 90 天限价执行精度预声明

- 目的：区分 A/B 方向信号无效与“立即市价执行成本过高”；只做 research-only 诊断，不改现役
  下单、模型生命周期或风险预算。
- 第一方案在看结果前固定为信号价限价、有效一根 15m；入口仍按 taker 费率计，只免入口滑点，
  出口保留 taker+滑点。填单分钟无法证明先后的有利 TP 忽略，同分钟止损保守计 SL。
- 第二方案在 BNB/LTC 90 天结果尚未落库前预声明：限价相对信号价改善当前估计的双边
  `taker+slippage` 成本，成交后以原 ATR 风险距离重锚 -1R/+2R，有效期仍为一根 15m。前 8 币
  BTC/ETH/SOL/XRP/DOGE/LINK/ADA/AVAX 为开发集，BNB/LTC 为未看标的留出集。
- 固定否决门：至少 30 个完整成交、5 个顺序时间折至少 4 折费用后为正、按同一 15m 市场事件聚类的
  单侧 95% EV 下界大于 0、最大单币正贡献不超过 50%；开发集通过但留出集不一致也不得接入。
- 数据证据：10 个主流 OKX USDT SWAP、90 天 1m/15m 与 180 天 1H/4H 上下文，共
  1,436,326 根 `confirm=1` K、2,700 条资金费、14,390 次公开请求、错误 0；40/40 序列覆盖
  99.91%～100%。公平重放扫描 85,803 个已收线 15m 时点，A 为 5,297 候选/5,227 完整路径，
  B 为 5,878/5,833；11,175 候选的资金费和横截面均可用，115 条不连续路径保持 missing。
- 即时成交基线：A TP/SL/timeout=1,589/3,235/403，毛 EV +0.0222R、费用后 -0.6767R；
  B=1,834/3,722/277，毛 EV +0.0022R、费用后 -0.6505R。两者 61 个因子均 validated=0，
  多分类 Brier skill 分别 -1.49%/-3.54%，入场与极值模型均不生成。
- 信号价限价：A/B 填单率 98.47%/98.66%，费用后每成交 -0.5144R/-0.5042R，聚类 95%
  下界 -0.5667R/-0.5725R；5/5 时间折、4/4 自然月、10/10 标的全部为负。
- 成本回收限价：固定改善 20bp 后，A/B 填单率 32.08%/46.58%，完整成交 1,677/2,717，
  每成交 -0.3657R/-0.4012R，每原候选 -0.1173R/-0.1869R，聚类 95% 下界
  -0.4248R/-0.4182R；仍为 5/5 折和 4/4 月全负。未看标的 BNB/LTC 也分别为
  A -0.4772/-0.3318R、B -0.5558/-0.4841R，留出集否决与开发集一致。
- 裁决：两种限价都 `stop_no_promotion`，只保留可复现诊断工具；不新增执行策略、不生成模型、
  不改固定 2:1、阈值、风险预算或 Harness 权限。模型列表继续为空是证据驱动的正确状态。

## 2026-08-24 策略 C 极端反转预声明

- 目的：A/B 方向信号在 90 天证据中均未形成正期望，另起一个与趋势突破/回踩不同的
  research-only 方向假设；不得把本轮结果混入 A/B、自然 paper 或 Harness 成熟度。
- 在查看策略 C 结果前固定候选：只消费已收线 15m K；RSI14 多头不高于 25、空头不低于 75，
  收盘越过 Bollinger20 的 2 标准差边界，同时出现拒绝影线（对应侧影线至少等于实体），且
  Wilder ADX14 不高于 20。信号在 K 线收盘后成立，按下一根 1m 开盘成交。
- 固定交易几何：以信号 K 的 Wilder ATR14 为 1R，止损 1 ATR、止盈 2 ATR，最长 4 小时；
  同一分钟同时触及止损止盈一律先计止损，双边都按现役 taker+slippage 成本扣除，不用
  限价成交或 maker 费率美化结果；同一标的在前一候选 4 小时标签窗结束前不重复发候选，
  防止把同一段行情冒充多次独立机会。资金费使用信号时点最近已结算费率按 4h/8h 比例
  保守扣除，可能收入一律按 0，口径与现役 `execution_cost_r` 一致。
- 固定数据切分：BTC/ETH/SOL/XRP/DOGE/LINK/ADA/AVAX 为开发集，BNB/LTC 为完全未看标的
  留出集；不因候选数量或结果调整上述阈值。
- 固定晋升门：完整候选至少 100、5 个顺序事件时间折至少 4 折费用后 EV 为正、按同一信号
  时点聚类的单侧 95% EV 下界大于 0、最大单币正贡献不超过 50%；开发集通过后，留出集还须
  至少 30 个完整候选、费用后 EV>0 且聚类下界>0。任一失败即 `stop_no_promotion`，只保留
  否决证据，不接入现役扫描、Harness、模型生命周期或订单链。
- 开发集实测：数据哈希 `cbd4355fddfbad1f1692686b3887b7a58dd4956a107f81e4751685880d057634`；
  90 天仅形成 6 个完整候选，TP/SL/timeout=0/5/1，胜率 0，毛 EV -0.7149R、费用后
  -1.2286R、事件聚类单侧 95% 下界 -1.7247R，5/5 时间折均负。样本量和效果同时失败，
  裁决 `stop_no_promotion`；BNB/LTC 留出集按预声明保持 `sealed_not_opened`，没有为了找正
  结果而开启或调参。

## 2026-08-24 Harness 首触风险先验预声明

- 目的：修复完整 Harness 版本把多数候选机械标成 `risk_probability=0.55` 的不可校准问题；
  不让 LLM 自行创造概率，先验证冻结的确定性 4h 首触预测能否充当风险先验。
- 在查看条件结果前固定规则：只读取候选时点已经冻结的 `forecast.p_hit_sl`；沿用现役
  `AGENT_HARNESS_REJECT_MIN_RISK=0.70`，当且仅当 `p_hit_sl≥0.70` 时形成 shadow reject
  假设，其余候选保持 baseline pass。阈值不搜索，不使用结果标签补特征，不恢复任何基线拒单。
- A_pullback 与 B_breakout 分开按全成本评估，不能互借成熟度；同一 15m 时点跨币候选按市场事件
  聚类。每条策略固定门为：可用预测至少 300、reject 至少 30、保留候选至少 100；SL 概率相对
  常数基准 Brier skill>0；5 个顺序事件折至少 4 折 policy 增量 EV>0 且保留候选 EV>0；reject
  阻亏精度至少 70%；保留候选的事件聚类单侧 95% EV 下界>0，最大单币正贡献不超过 50%。
- 两条策略必须同时通过才可把确定性先验加入下一版 Harness shadow Context；任一失败即
  `stop_no_promotion`，不改 Prompt 身份、不重启进程、不接入 veto 或订单链。当前 27 条自然校准
  只用于交叉核对，不替代 90 天样本外门，也不因近期 B 的小样本胜率调整阈值。
- 90 天实测：A 的 5,221 条可用预测中拒绝 1,125 条（21.55%），阻亏精度 69.60%，Brier
  skill -2.12%；policy 每原候选从 -0.6756R 改善至 -0.4927R，但保留候选仍 -0.6280R、
  聚类下界 -0.6951R。B 的 5,833 条中拒绝 1,208 条（20.71%），阻亏精度 68.79%，Brier
  skill -5.52%；每候选从 -0.6505R 改善至 -0.4841R，但保留候选仍 -0.6105R、聚类下界
  -0.6919R。两者每个时间折的“少亏”增量均为正，但 5/5 折保留候选 EV 全负，按预声明
  `positive_folds=0`，裁决均为 `stop_no_promotion`。
- 自然样本交叉核对：A 27 条中同规则拒绝 14 条，保留 13 条仍 -0.8062R、下界 -1.2880R；
  B 虽有 29 条已结算候选且毛 TP/SL=17/12，但 29/29 的冻结快照都没有 `forecast`，不能把近期
  小样本胜率冒充已校准风险先验。该缺口应先补 B 的因果 forecast 留样，再自然积累校准证据。
- B 留样修复：`_scan_strategy_b_shadow` 在 `enrich_shadow_signal` 后复用与 A/历史重放相同的
  `forecast_for_trade`，只传已收线 15m 窗口，并显式用候选 `event_ts` 限制经验标签 as-of。
  bootstrap seed 固定绑定 `FORECAST_REPLAY_SEED_VERSION + inst_id + kline_ts + direction`，实时
  与历史重放可逐候选复算；forecast 随首次候选快照一起冻结，仍保持 B `final_decision=rejected`、
  Harness `allow_veto=false`、订单和 journal 均为 0。引擎专项 33/33 通过。
- B 留样部署：提交 `9b7695b` 后只 kickstart `com.crypto.paper`；模拟盘 PID 79777→11627，
  第二心跳年龄 3.8s，`/reconcile balanced=true`、持仓/台账均空、`/error` 为空。8090 实盘仍为
  原 PID 90574，未重启、未写入。新 forecast 覆盖率须从下一次自然 B 候选起统计，历史缺失不回填。

## 2026-08-24 Harness 结构错误有界修复

- 现象审计：完整 v4 Prompt 的自然运行中 completed=21、schema_error=8；失败响应的累计
  output_tokens 最小 200、8/8 均不低于 200。部分单次响应恰好撞到 200-token 上限，另一些先触发
  语义修复、第二次又成为普通 `ValueError`。旧图只重试 `AgentSemanticError`，普通 JSON/schema
  错误直接丢样本，导致相同的一次修复预算没有被使用。
- 修复：`agent_graph.validate` 对结构解析/契约错误也使用既有的一次有界 repair；repair 指令明确
  只返回一个无 Markdown/额外字段的完整 JSON，并列出全部必填字段。第二次仍无效则保持
  `schema_error + baseline_pass`，无效 reject 永远不能成为 veto；失败 step 保存错误类型、摘要、
  response hash 和 retry_count，便于区分截断与语义违规。
- 证据隔离：运行行为变化后把 `AGENT_HARNESS_TOOL_POLICY_VERSION` 升为
  `tool-policy-v2-structural-repair`，新旧版本不混计 100/30 成熟度。专项新增“首轮截断 JSON、
  第二轮合法 approve”路径，15/15 通过；这只提高有效 shadow 样本产量，不改变 0.70 reject 门、
  生命周期门或 veto 权限，真实失败率改善必须等新版本自然候选验证。
- 部署证据：提交 `908832a` 后只 kickstart `com.crypto.paper`，模拟盘 PID 11627→12828；第二次
  `/health` 为 ok、心跳年龄 8.6s，空仓、`/reconcile balanced=true`、`/error` 为空。本地加载身份
  为 `tool-policy-v2-structural-repair`；`/agent/status` 在尚无新自然候选时仍展示旧库中最新版本，
  不人工制造调用或迁移旧证据。8090 `com.crypto.agent` 继续保持原 PID 90574，未触碰。

## 2026-08-24 Agent 主动候选零运行修复

- 现象：配置 `AGENT_PROPOSAL_SHADOW_ENABLED=true`、paper provider ready，且扫描尾部已调用
  `_run_agent_proposal_shadow`，但 `/agent/proposals` 长期 `run_count=0/proposal_count=0`；不是
  模型返回空 proposals，因为 `agent_proposal_runs` 连一次调用记录都没有。
- 根因：每周期恰好请求 `AGENT_PROPOSAL_MIN_BARS=60` 根，随后因果过滤当前未收线 K，通常只剩
  59 根；`build_market_snapshot` 要求 15m 至少 60 根，于是所有标的都在调用模型前被跳过。
- 修复：三周期统一预取 `MIN_BARS+2`，过滤逻辑和 60 根有效门完全不变；回归测试显式断言三次
  请求均为 62 根、proposal run 可落库、2:1 几何仍由代码生成、fake orders/algos 均为空。
  修复只恢复 paper-only shadow 反事实采样，不给 C_agent_proposal 执行、veto 或预算权限。
- 部署验收：提交 `9f94579` 后只 kickstart `com.crypto.paper`，模拟盘 PID 12828→14086；8090
  `com.crypto.agent` 始终为原 PID 90574。02:35 自然扫描首次记录 1 个 run、1 个 AAVE long 提案，
  `runtime_status=completed`、确定性几何 `reward_risk=2.0`、`valid_count=1`；因
  `no_validated_active_model` 保持 `prediction_passed=0`、`execution_authority=0`，没有订单。
  `/health` ok、未暂停，`/status` 空仓/未熔断/敞口 0，`/reconcile balanced=true`、`/error`
  为空；`/models/entry` 仍为空且 `budget_expansion_allowed=false`。这证明采样链已恢复，不代表
  提案或入场模型已经通过正期望验证。

## 2026-08-24 A 空头收缩强趋势精度过滤预声明

- 目的：在不改变 A_pullback 候选定义、固定 1R:2R、4h 首触或成本口径的前提下，验证“强方向
  环境中的低宽度收缩”能否只保留更高 TP-first 精度的空头回踩；该研究只能否决或进入新的
  paper shadow，不能直接点亮入场模型、Harness veto 或订单权限。
- 探索边界：只查看 BTC/ETH/SOL/XRP/DOGE/LINK/ADA/AVAX 的前 60 天
  （2026-05-26 08:00 UTC 至 2026-07-25 08:00 UTC），从 9 个预先限定的单/双条件可解释变体中
  选择固定规则：`strategy_id=A_pullback`、`direction=short`、冻结特征
  `adx>=0.24` 且 `bb_width_percentile<=0.21`。训练段 155 条，TP-first 85、SL-first 64、
  timeout 6，TP-first 54.84%，保守全成本净 EV +0.1037R；此结果仅用于提出假设，不参与验收。
- 数据身份：研究库 SHA-256
  `0451c9e1be7e76b40131ca4701a31422b82ebe9ebe72e0b574787dd631a9b454`；行情库
  SHA-256 `cbd4355fddfbad1f1692686b3887b7a58dd4956a107f81e4751685880d057634`。
  规则冻结前未查看后 30 天或 BNB/LTC 的该过滤结果。
- 第一验证层固定为同 8 币后 30 天（2026-07-25 08:00 UTC 起）：完整候选至少 50、TP-first
  精度至少 45%、费用后净 EV>0、按同一 15m 市场事件聚类的单侧 95% EV 下界>0、4 个顺序事件折
  至少 3 折为正、最大单币正贡献不超过 50%。任一失败立即 `stop_no_promotion`，不打开标的留出集。
- 只有第一层全部通过才打开 BNB/LTC 90 天留出：合计至少 30 条、两币各自费用后 EV>0、合计
  聚类下界>0；随后全验证集还须 Wilson 单侧 95% TP-first 下界高于逐候选成本对应保本胜率的中位数。
  所有门通过也只允许新增独立版本的 paper shadow 过滤器，至少再积累 60 条自然结算且实际费用后
  EV 下界>0，才可另提人工批准；不得回填自然 paper 计数或扩大 1%/150/600 风险预算。
- 后 30 天实测：形成 56 条，TP/SL/timeout=17/35/4，TP-first 30.36%，平均成本 0.9175R，
  费用后 -0.9020R、市场事件聚类单侧 95% 下界 -1.2692R；4 个顺序折仅首折为正，最大正贡献
  完全由 ADA 单币提供。训练参考段自身聚类下界也为 -0.1535R、仅 2/4 折为正，说明训练均值
  +0.1037R 不具稳定性。裁决 `stop_no_promotion`，BNB/LTC 保持 `sealed_not_opened`；不接入扫描、
  模型、Harness 或订单链。

## 2026-08-24 Agent 主动提案因果历史回放预声明

- 目的：不等待每条自然提案 4 小时结算，直接检验当前已冻结的 `agent-proposal-v1` +
  `deepseek-chat` 是否具有方向选择精度；历史回放只给否决权，不能计入自然 paper、模型生命周期、
  Harness 成熟度或执行授权。
- 输入在看结果前固定：BTC/ETH/SOL/XRP/DOGE 五个高流动性 SWAP；每个 UTC 00:00/12:00
  批次只读取此前已收线的 60 根 15m、1H、4H K，复用现役 `build_market_snapshot`；模型只能从
  五个快照中返回 0～2 个方向，入场为最后收线 15m close，系统固定 1 ATR 止损、2 ATR 止盈，
  随后用完整 4h/1m 路径首触结算，同分钟双触按止损。行情库 SHA-256 为
  `cbd4355fddfbad1f1692686b3887b7a58dd4956a107f81e4751685880d057634`。
- 第一阶段固定为 2026-05-27 00:00 至 2026-07-25 00:00 UTC，共 118 个相隔 12h 的批次；输出
  写入独立 research-only DB，逐批幂等，可中断恢复。provider/schema 失败也保留 run，不重试到改变
  原判断；输出方向、理由和证据 ID 仍走现役契约门。模型费用按每批输入字符全按 cache miss token、
  再加 200 output tokens 的上界估算，并按该批有效提案分摊到每条 R 成本，禁止漏报推理成本。
- 第一阶段固定门：completed run 至少 100、schema 成功率至少 95%、有完整路径的有效提案至少 100、
  TP-first 精度至少 45%、Wilson 单侧 95% 精度下界高于每条全成本保本胜率中位数、包含交易和模型
  成本后的净 EV>0且按 12h 批次聚类下界>0、5 个顺序折至少 4 折为正、最大单币正贡献不超过 50%、
  long/short 任一方向占比不超过 90%。任一失败即 `stop_no_promotion`，不打开时间验证段。
- 只有第一阶段全通过才打开 2026-07-25 00:00 至 2026-08-23 12:00 UTC 的 60 个批次，并重复同一
  门（完整提案下限降为 50、completed run 下限 50，其余不变）。两阶段通过也只证明固定五币面板上的
  历史可迁移性；现役 C_agent_proposal 仍须独立自然 paper 结算至少 100 条并满足既有生命周期门，
  才能另提人工批准，绝不直接下单。
- 第一阶段实测：118/118 批次完成调用，109 completed、9 schema_error，schema 成功率 92.37%；
  形成并完整结算 49 条提案，路径缺失 0。49/49 全为 long，TP/SL/timeout=11/34/4，TP-first
  22.45%、Wilson 单侧 95% 下界 14.24%，而全成本保本胜率中位数为 55.78%。平均交易成本
  0.7565R，按 10 USDT 名义保守折算的模型成本 0.0082R，最终净 EV -0.9490R、12h 批次聚类
  下界 -1.4114R，5/5 时间折全负；仅 XRP 单币为正，正贡献集中度 100%。
- 裁决：除 completed run 数外全部固定门失败，`stop_no_promotion`；后 30 天时间验证段保持封存，
  不为寻找正结果继续调用。v1 的“可空仓”减少了提案数量，但没有形成方向优势；不得接入模型、
  Harness veto 或订单链。独立回放库保留在 `/private/tmp/crypto-agent-proposal-replay-v1.db`，
  research-only 结果不计入任何自然成熟度。

## 2026-08-24 高波动 Alt 面板扩展预声明

- 目的：现有 90 天十币库以 BTC/ETH 等主流币为主，A/B 即时入场成本约 0.65R，Agent 五币面板
  的保本胜率中位数也达 55.78%；现役自然提案的 AAVE/CRV 成本仅 0.16～0.29R。固定扩展一组
  更接近当前 watchlist 的高波动合约，区分“方向完全无效”和“主流币 ATR 太低导致成本吞没”。
- 在下载和结果前固定标的为 AAVE/CRV/INJ/NEAR/ZRO，来源是 2026-08-24 02:34 CST 自然 daily
  scan 的前列加密候选，不因后续结果替换币种。独立行情库只写
  `/private/tmp/crypto-agent-90d-alt-market.db`；抓取 90 天已确认 1m/15m 与 180 天 1H/4H、同区间
  已结算 funding，禁止写 `data/market.db` 或运行 DB。
- 第一层先用现役、未调参的 A_pullback/B_breakout 做全量因果重放，前 60 天只作开发参考、后 30 天
  为时间验证；仍固定 1R:2R、4h/1m 首触、同分钟止损优先和双边 taker+slippage+不利 funding。
  每策略验证段须至少 300 条、费用后 EV>0、市场事件聚类单侧 95% 下界>0、5 折至少 4 折为正、
  最大单币正贡献≤50%，才允许进入该面板自身的模型研究；否则 `stop_no_promotion`。
- 只有 A/B 至少一条先通过第一层，才在同一固定面板上回放 `agent-proposal-v1`；Prompt/Schema/模型
  和每 12h 频率不改，复用 100/50 提案、Wilson 精度下界、成本、五折、方向平衡和时间级联门。
  历史 alt 结果无论多好都不能替代至少 100 条自然 C 提案结算，也不能直接生成 active 模型或订单。
- 数据验收：7,195 次公共 K 线请求、718,109 根已确认 K、1,620 条已结算资金费，错误 0；20 条
  symbol×bar 序列覆盖 99.91%～100%。行情库 SHA-256
  `1d87bd81d4943f0f06b689c4d8e96a1d12c46c665294e8c947123bc20d5b70ea`；A/B 重放形成
  5,533 个候选、5,418 条完整 4h/1m 路径，115 条缺失保持 missing，研究库 SHA-256
  `f9f21a068f939a45cad28b503802219530e3ac56531ae4752e247e0eb52baf59`。
- 后 30 天实测：A 763 条，TP/SL/timeout=238/484/41，TP-first 31.19%，毛 EV +0.0126R、
  平均成本 0.4500R、净 EV -0.4375R、事件聚类下界 -0.5331R；B 936 条，304/604/28，
  TP-first 32.48%，毛 EV +0.0183R、成本 0.4120R、净 -0.3937R、下界 -0.4845R。两策略
  都是 0/5 折为正且五币逐一为负；前 60 天参考段也分别为 -0.2401R/-0.4415R、0/5 折为正。
- 裁决：A/B 均 `stop_no_promotion`，没有策略达到“才允许 Agent alt 回放”的前置门，因此不调用
  provider、不做第二次寻找正结果。Alt ATR 的确降低了成本 R，但方向精度仍未达到 2:1 全成本保本
  要求；不生成模型、不接入 Harness 或订单链。

## 2026-08-24 Agent 主动提案 v2 微观结构自然验证预声明

- 背景：冻结的 `agent-proposal-v1` 在主流币历史回放中 49/49 全为 long、费用后 EV
  -0.9490R；未调参 A/B 在高波动 Alt 面板也均为负。OHLCV 上继续搜索价格条件没有形成稳定方向
  优势，不能把空仓率或低成本标的误当成入场精度。
- 固定变化：Prompt 身份升级为 `agent-proposal-v2-microstructure`。每个自然已收线 15m 批次除
  原有 EMA、ATR、1h/4h 动量和量比外，冻结资金费、盘口失衡、价差、微观价格、多档深度、预估
  滑点、相邻快照订单流/撤单失衡、持仓量变化、基差和实时多档事件 OFI；缺失值保持 `null`，禁止
  模型编造。历史行情库没有完整的同口径盘口事件流，因此 v2 不用 OHLCV 伪造历史微观结构结果，
  只从部署后的自然 paper 时点积累。
- 确定性门：模型提案方向必须与 15m EMA20/EMA50 带、1h 收益动量、4h 收益动量三者严格同号；
  任一缺失或冲突均在 2:1 几何和 signal sample 之前以 `direction_evidence_conflict` 拒绝。该门由代码
  执行，不依赖模型遵守 Prompt；不新增可搜索阈值，也不修改 A/B 或现役下单参数。
- 隔离与权限：Prompt 版本进入 cycle key 和 `C_agent_proposal` strategy version，新旧证据不混计；
  v2 仍为 paper-only shadow，`execution_authority=0`，只用确定性 1 ATR 止损、2 ATR 止盈和共同 4h
  首触链形成反事实结果。入场模型为空、未达到自然成熟门时保持 `prediction_passed=0`，不得下单、
  veto、扩大预算或回填 v1 历史样本。
- 验收不变：至少积累 100 条自然完整结算提案，再按费用后 EV、事件聚类下界、顺序时间折、精度
  相对逐候选保本率、方向平衡和单币贡献集中度验收；全部通过也只允许进入既有模型影子/晋升流程，
  仍需人工批准。当前实现测试只证明证据冻结、冲突拒绝、幂等和零订单，不证明胜率已经提高。
- 回归隔离：首次全量测试发现全局 v2 身份会让冻结的 v1 历史回放套用新方向门，导致原研究夹具不再
  可复算。回放工具现以作用域明确锁定 `agent-proposal-v1`，同时恢复 v1 payload 形状、System Prompt、
  cycle key 与 signal strategy identity；退出作用域必定还原现役 v2。历史否决证据保持原协议，v2
  自然样本也不会反向污染 v1 重放。
- 部署竞态隔离：提交后重启前，旧 PID 14086 热读到新 Prompt 字符串，曾产生 1 个标为 v2、但
  `input_hash` 与同 K 的 v1 完全相同的 run（2 条提案）；它不含新微观结构实现，不能计入 v2。
  模拟盘已先重启至 PID 34590 结束旧内存。随后新增独立
  `AGENT_PROPOSAL_IMPLEMENTATION_VERSION=agent-proposal-impl-v2-microstructure`，同时进入 v2 cycle key、
  prompt payload 和仅 C 策略的 signal identity；因此该竞态 run 与最终 v2 自然证据永久隔离，A/B
  身份不受影响。v1 回放作用域仍省略该字段，保持原 cycle key/payload/identity。
- 最终部署证据：实现提交 `38f6ebf`、身份隔离提交 `550bdfd`；最后一轮自动发现的离线测试
  57/57 通过，params/code graph/AI repo/test isolation/fix guard 全绿。只 kickstart
  `com.crypto.paper`，PID 14086→34590→34968；`com.crypto.agent` 始终为 PID 90574。首次重启时
  LaunchAgent 曾有一次旧 `lib/numpy` 导入失败和一次 worker 重启失败，KeepAlive 随即恢复；最终实际
  job 为 running、程序 `.venv/bin/python`、`PYTHONPATH=/Users/wuhai/crypto-agent`，服务 uptime 195.6s、
  心跳 0.0s，`/error` 为空。
- 自然验收：最终进程完成新 K 线 run `proposal-run-16ca0539810bd30a40ba2b76`，cycle key
  `16ca0539810bd30a40ba2b763698f72bc5767831bb71afcd201d380ad25bdfc0` 与包含 implementation version
  的本地确定性复算完全一致；`runtime_status=completed`、549ms、模型返回 0 提案，按设计选择空仓。
  `/status` 空仓、未熔断、敞口 0，`/reconcile balanced=true`，入场模型仍为空且禁止扩大预算；提案
  接口继续 `shadow_only=true/execution_authority=false`。这证明最终 v2 自然链已运行，不代表其胜率
  已通过；有效性仍等至少 100 条独立成熟结果。

## 2026-08-24 Agent 主动提案 v3 可审计输入预声明

- 触发证据：最终 v2 自然链只有 1 个正确实现批次，模型返回 0 提案；运行表只保存 input hash，无法
  回答模型当时看到哪些盘口/订单流字段、缺失率多少、因何空仓。只等 100 条会把不可解释样本继续
  放大，无法针对条件精度做可靠诊断。
- 固定协议：版本升级为 `agent-proposal-v3-audited-microstructure` / schema
  `agent-proposal-schema-v2-abstain-reason` / implementation
  `agent-proposal-impl-v3.1-audited-microstructure`。每个 paper 批次原子冻结完整 canonical input snapshot、
  版本、input hash、标的数和 15 个微观字段的 present/total/coverage；不保存凭证或下单能力。
- 证据与空仓契约：每个自然微观快照新增带 as-of 毫秒的 evidence ID；非空提案必须至少引用对应标的
  一个 K 线证据和 microstructure 证据，否则以 `microstructure_evidence_required` 在几何前拒绝。
  空提案必须从 no_aligned_candidate、microstructure_conflict、insufficient_microstructure、
  liquidity_too_weak、no_clear_edge 中给唯一标准原因；有提案时该字段必须为 null。
- 评价隔离：只读接口按完整 implementation version 汇总 run、completed、abstain、proposal、mature、
  非空覆盖率与加权微观覆盖率；旧 v1/v2 和无冻结输入的竞态 run 不计入当前协议。v1 历史工具同时
  冻结原 Prompt/Schema/实现，payload 仍无 v3 字段并保持原契约。
- 权限与晋升门不变：v3 仍是 OKX paper-only shadow，0 下单、0 veto、0 预算扩大；可审计覆盖率只是
  数据质量，不是收益证明。至少 100 条当前协议的独立成熟非空提案及既有费用后 EV/精度下界/时间折/
  方向平衡门全部通过后，才能提出下一阶段人工批准。
- 部署前隔离：旧 PID 在代码重启前热读 v3 配置，留下 2 条无 input audit 的伪 v3 run；历史行不删除，
  最终实现身份提升为 v3.1，当前协议统计只接受带完整 audit 且 implementation 精确等于 v3.1 的 run。
- 最终部署证据：实现提交 `12c9d7b`、竞态隔离提交 `0a200ab`；最终自动发现离线测试 57/57 通过，
  params/code graph/AI repo 与 diff check 全绿。仅 kickstart `com.crypto.paper`，PID 34968→36919；真实
  服务 PID 90574 未变化。重启后 `/health` 正常、`/status` 空仓且 open notional 0、未熔断，
  `/reconcile balanced=true`、`/error` 为空、`/models/entry` 仍无 active model 且禁止扩大预算。
- 自然 v3.1 验收：run `proposal-run-1adc01f308b494ae465e51f9` 在已收线 K
  `1787515200000` 上 completed，463ms；冻结 5 个自然标的快照，15×5=75 个微观字段中 51 个有效，
  coverage 0.68。保存的 input hash
  `ce90827437a3169f8e68a981badab3f550b6cbc53d0d68a6495dd069aeb7d792` 与冻结 snapshot 本地复算一致；
  模型按 schema 返回 `no_aligned_candidate`，因此 proposal/mature/order 均为 0。接口保持
  `shadow_only=true/execution_authority=false`；该结果证明审计链和诚实空仓生效，不证明胜率已提高。

## 2026-08-24 Agent 主动提案 v3.2 盘口单位修正预声明

- 触发证据：v3.1 首个冻结快照中 DOGE 价差仅 0.63 bp，但预估滑点达到 7910.24 bp。OKX 官方当前
  `DOGE-USDT-SWAP ctVal=1000 DOGE`，证明 order book 的张数被上层误当成基础币，可见 USDT 深度
  低估约 1000 倍；这是输入单位错误，不是市场真实流动性结论。
- 固定修复：`ExchangeAdapter.fetch_order_book` 契约统一为 USDT 价格+基础币数量；原生 OKX 以
  instrument ctVal、CCXT 以 market contractSize 在适配层完成换算。上层盘口失衡比例保持不变，
  `price × qty` 的可见名义额与 expected slippage 恢复同一单位。
- 实验隔离：implementation 升为 `agent-proposal-impl-v3.2-base-qty-book`；v3.1 的 1 个自然 run 保留
  audit 但不计入 v3.2。Prompt、Schema、候选数、方向门、2:1 几何与全部晋升门不变，仍是 paper-only
  shadow、零下单权限；本修复只消除错误输入，不预先声称胜率提高。
- 单位实测：同一市场时点 OKX 原生 DOGE 最优档数量约 171～423，CCXT 约 138～208，二者同量级且
  CCXT market 明示 `contractSize=1000`，确认统一 amount 仍是张数，不存在适配器二次换算风险。
- 部署证据：提交 `5cc665d`；最终全量自动发现测试 57/57，交易所分层 51/51，params/code graph/
  AI repo/test isolation/fix guard/diff check 全绿。只 kickstart `com.crypto.paper`，PID 36919→37966；
  真实服务 PID 90574 未变化。重启后空仓、未熔断、open notional 0、`balanced=true`、`/error` 为空。
- 自然验收：v3.2 run `proposal-run-3ec1ed9a6faff50fe7e13e4d` completed，770ms，冻结输入 hash
  `2b83b16dbbffe5bbdd8aa6119a79dad7a5c21e5c49c1f22e48b029cbeb4bc024` 本地复算一致。DOGE
  expected slippage 从错误的 7910.24 bp 降至 9.14 bp，XRP 从 200.06 降至 3.34 bp；低深度 INJ/ZRO
  仍诚实保留 81.39/126.31 bp。模型因无三周期同向标的返回 `no_aligned_candidate`，提案、成熟和订单
  均为 0，接口继续 `shadow_only=true/execution_authority=false`。这证明输入单位修复，不证明胜率已转正。

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
- Phase 3 学习闸门基建: 阈值层喂影子分(FLAG_USE_SHADOW_SCORE_GATE=False 门控默认关, A3 通过后人工开启); experiments 试验注册表 + decision/experiments.py(propose/judge, DSR≥1+PBO<0.3+样本≥30); factors/overfit_guard.py(Deflated Sharpe Acklam 逆正态 + CSCV-PBO, 8 项单测含 n_trials=1 边界)。
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

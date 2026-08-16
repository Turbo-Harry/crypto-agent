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
- FINAL_REPORT.md 交付报告完成

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
- ✅ R1-13 子账户测试文档 subaccount_test_plan.md。
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
- ✅ R2-4 watchdog.py（PID 文件 + 心跳 stale/missing 判定 + 去抖3次 + os.kill(pid) 精确 kill + 飞书告警）；两进程 run() 写 .pid 与 heartbeat；watchdog_launchd.md 模板文档。
- ✅ R2-5 _place_tp（attachAlgoOrds 首选 + 原生降级 + tp_missing 打标）；FLAG_ENABLE_EXCHANGE_TP=False 默认关闭；tp_sandbox_verify.md 验证清单。
- ✅ R2-6 deep_review 补 atr_value/signal_price（log_entry 字段 + open_position 传参 + monitor 传参）。单测：止损太紧教训产出/对照无。
- 全量验证：16 文件语法 + 16 模块导入冒烟 + 各方案累计 10+ 单测全过。

## 收敛状态（第 2 轮设计→实施闭环完成）
- 待办 backlog 剩余：RES-15（execute 复用 execution.py，R1 已改同文件、可安全立项）、R1-13 子账户实测（文档已交付，等用户执行）、R2-5 沙盘验证（等用户执行）、RES-8/11/12/13/14/18/19/20（已标放弃）。

## 沙盘实测记录（协调者只读+受控验证，畸形单问题定论）
- 实测1（只读）：orders-algo-pending 必须带 ordType 参数（不带 → 51000）→ R1-1 取消函数已修（枚举 6 类）。
- 实测2（只读）：全 5 币 0 个挂起条件单，而 ETH 仍有 1.22 多仓 → ccxt 旧写法（type=market+ordType=conditional+triggerPrice）挂单从未真正生效，交易所侧止损是幻觉。
- 实测3（受控挂单）：原生构造 triggerPx → 50015 拒绝；slTriggerPx 结构 → 挂单成功、pending 可见（字段全对）、枚举取消 → 0 残留 ✅
- 代码修正：directional_trader SL 用 slTriggerPx 原生结构；_place_tp 降级用 tpTriggerPx 结构。
- 结论：tp_sandbox_verify.md 的验证门槛现在已可满足——TP 可安全开启（FLAG_ENABLE_EXCHANGE_TP），但 attachAlgoOrds 首选路径未实测（本测走的是原生降级路径）。

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

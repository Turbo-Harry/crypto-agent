# 踩坑记录（pitfalls）

> 本文件是仓库的"事故档案"：写代码前先读这里，同类坑不再踩第二遍。
> 记录模板（每修一个 bug / 每踩一个坑，**当场追加**，不留到事后补）：
>
> ```markdown
> ### YYYY-MM-DD 一句话标题
> - 现象：……
> - 根因：……
> - 修复：……
> - 预防：……
> ```

---

## 交易所 API 类

### 2026-08-16 OKX 条件单挂单报 50015（triggerPx 参数错）
- 现象：挂交易所侧止损用 `triggerPx` 字段，返回 `50015 Either parameter tpTriggerPx or slTriggerPx is required`。
- 根因：OKX 原生 `order-algo` 接口的条件单要求用 `slTriggerPx`/`slTriggerPxType`/`slOrdPx`（止损）或 `tpTriggerPx` 系列（止盈），不接受通用 `triggerPx`。
- 修复：exchange 适配层 `place_conditional_stop` 用 slTriggerPx 系列、`is_tp=True` 用 tpTriggerPx 系列。
- 预防：条件单字段只在 exchange 适配层出现一处；新增字段前先查 OKX 文档。

### 2026-08-16 orders-algo-pending 查询必须带 ordType（51000）
- 现象：查 pending 条件单不带 `ordType` 参数返回 51000。
- 根因：OKX 该端点强制要求 ordType。
- 修复：`pending_algo_ids` 按 6 种 ordType（conditional/oco/trigger/move_order_stop/iceberg/twap）枚举查询后合并。
- 预防：查询类接口缺参报错时，先核对 OKX 必填参数表。

### 2026-08-16 幽灵条件单残留误平新仓
- 现象：平仓后条件止损单未撤销，下次开仓时旧止损单触发把新仓平掉。
- 根因：平仓路径只平仓不撤条件单；进程崩溃后残留无人清理。
- 修复：开仓前 `_cancel_stop_orders` 清理该 instId 全部 pending algo 单；平仓/强平成功后也撤销。
- 预防：任何"平仓"代码路径必须配对"撤条件单"。

### 2026-08-16 Cloudflare 403 error 1010（缺 User-Agent）
- 现象：原生 urllib 请求 OKX 返回 `HTTP 403: error code: 1010`。
- 根因：请求头没有 User-Agent，被 Cloudflare 拦。
- 修复：exchange 传输层统一带 `User-Agent: Mozilla/5.0 …` 头。
- 预防：所有直连外部 API 的请求统一走 transport 层，不在业务层裸发 urllib。

### 2026-08-16 history-candles 返回倒序
- 现象：K 线序列看起来时间乱序，指标计算错位。
- 根因：OKX `history-candles` 返回新→旧（倒序），需反转为升序。
- 修复：适配层 fetch_candles 用 `reversed(rows)` 统一成升序。
- 预防：K 线进入策略前在适配层做归一（升序 + Candle 模型），策略层不再碰原始顺序。

### 2026-08-16 资金费率账单金额字段不是 amount 而是 balChg
- 现象：统计资金费收入拿到 0。
- 根因：OKX `/account/bills` type=8（funding）的金额在 `balChg` 字段，不是 `amount`/`fee`。
- 修复：`fetch_bills(bill_type="8")` 后按 `balChg` 求和。
- 预防：接入新账单字段前先拉一条真实账单看结构，别凭直觉写字段名。

## 数量与风控类

### 2026-08-16 floor_to_lot 用 round 导致 0.5555 进位错误
- 现象：`floor_to_lot(0.5555, 0.001)` 期望 0.555 却得到 0.556。
- 根因：`round(0.5555*1000)` 用的是银行家舍入，半值进位不稳定。
- 修复：改为 `int()` 截断（只向下，不四舍五入）。
- 预防：所有"向下对齐"一律用 int 截断；单测覆盖边界值（.5 半值）。

### 2026-08-16 名义不足最小下单量时放大到 1 张（击穿 150 USDT 上限）
- 现象：150 USDT 名义买不起 0.01 BTC（最小 1 张）时，旧代码 `qty = ct_val` 兜底 → 实际下单 630 USDT，超小仓位上限 4 倍。
- 根因：把"数量为 0"兜底成"最小张数"，未考虑名义上限。
- 修复：最小下单量不足时**直接拒绝开仓**（宁可错过，不放大仓位）；单测覆盖。
- 预防：兜底值必须过风控闸门审查——凡是"补数量"的逻辑都视为红旗。

### 2026-08-16 round(x, float) TypeError（ccxt 精度是 float）
- 现象：`round(amount, mkt["precision"]["amount"])` 抛 TypeError。
- 根因：ccxt 的 precision 是 float，round 第二个参数必须是 int。
- 修复：自研 exchange 层用 `floor_to_lot` + Instrument.amount_precision 统一换算。
- 预防：数量换算只在 execution 层一处；禁止业务层直接 round 精度。

## 工程/流程类

### 2026-08-16 单测污染活体状态文件
- 现象：单测跑完，活体 journal/账本出现测试交易。
- 根因：单测复用真实 TradeJournal/PositionLedger 默认路径。
- 修复：单测注入临时目录路径（tempfile.mkdtemp），断言后自动丢弃。
- 预防：任何测试不得读写活体状态文件（trade_journal.json/watchlist.json/position_ownership.json）。

### 2026-08-16 引擎 tick 状态未在 __init__ 初始化导致接口报错
- 现象：/arb/status 访问 `signal_state` 报 AttributeError。
- 根因：运行时状态字典只在 `run()` 里初始化，服务模式直接调 `tick()` 未先走 run()。
- 修复：把 price_history/alert_cool/signal_state/decision_cool 初始化移到 __init__。
- 预防：引擎公共状态统一在 __init__ 初始化，run() 只负责循环。

### 2026-08-16 暂停后恢复 tick 不立即扫描（15min 计时）
- 现象：单测 pause→resume→tick 不触发开仓，误判为 bug。
- 根因：tick 内 15 分钟扫描计时 `_last_scan` 在暂停 tick 时已置位，恢复后需等下一周期。
- 修复：测试显式重置 `_last_scan`（生产语义不变——暂停后不追单是正确行为）。
- 预防：读 tick 类代码先理解计时语义；测试断言与真实节奏区分开。

### 2026-08-16 沙盘市场清单与生产不一致（XIAOMI-USDT-SWAP）
- 现象：公开接口显示 XIAOMI-USDT-SWAP live，但沙盘（ccxt 市场清单）查不到。
- 根因：沙盘（demo trading）市场清单是独立快照，与生产不同步。
- 修复：以实际下单环境为准探测场所；文档标注此边界（docs/architecture/exchange_layers.md）。
- 预防：新标的先在生产与沙盘各自探测，两边一致才纳入候选池。

### 2026-08-16 周末流动性骤降导致候选池空
- 现象：周末 volCcy24h 大跌，MIN_VOL=1000 万过滤后 0 候选。
- 根因：美股代币/小币周末成交量萎缩。
- 修复：MIN_VOL 降到 500 万 + 空结果回退主流池（BTC/ETH/SOL/XRP/DOGE）。
- 预防：流动性门槛是市场环境函数，保留回退路径而不是硬撑高门槛。

### 2026-08-16 美股代币不是"只有现货"（ANTHROPIC 有永续）
- 现象：假设美股代币仅现货可交易，漏掉 ANTHROPIC-USDT-SWAP。
- 根因：凭"X 前缀=现货"经验外推，未实测每个标的。
- 修复：场所探测改为逐币实测（venue_for 先合约后现货），ANTHROPIC 走合约路径。
- 预防：范围类结论必须实测验证，不靠命名规律外推（用户指正过一次）。

### 2026-08-16 迁移 ccxt 时 XIAOMI 缺失被误判为"ccxt bug"
- 现象：ccxt 沙盘查不到 XIAOMI，一度怀疑 ccxt 有缺陷。
- 根因：真实原因是沙盘市场快照缺失（见上一条），与 ccxt 无关。
- 修复：原生 OKX 适配层 + 逐币实测，彻底移除 ccxt。
- 预防：先定位根因再归责；"工具不行"是最容易的假结论。

### 2026-08-16 journal size 单位错位：旧记录是"张"不是"币"（用户追问暴露）
- 现象：投注额回填显示 3 笔 ETH 合计 2300 USDT，与"小仓位慢跑"原则明显不符。
- 根因：旧版 journal 的 size 存的是合约【张数】（0.53 张 = 0.053 ETH），回填按"币"算放大了 10 倍；且当时"有没有记录当前仓位"全凭记账，无交易所持仓快照可对。
- 修复：① 旧记录回填改按 张 × ctVal × entry 计算并标 size_unit="contracts(legacy)"；② 新记录显式 size_unit="base"；③ 新增本地仓位快照 positions_snapshot.json（每 60s 落盘）；④ 新增 /reconcile 对账端点：journal 记账 vs 交易所持仓，差异如实报告。
- 预防：任何"金额统计"先与交易所持仓对账再报数；单位语义必须显式落盘，不允许靠约定。

### 2026-08-16 JSON 切 SQLite 时把 JSON 文件当 SQLite 打开（file is not a database）
- 现象：测试对同一路径先写 JSON 再初始化 TradeJournal，报 sqlite3.DatabaseError: file is not a database。
- 根因：先跑 init_db（建表 executescript）再检测 JSON 头，JSON 文件已被 SQLite 建表写入破坏。
- 修复：顺序反转——先读文件头判断是否旧 JSON（首字符 `{`），是则读入内存、删除原文件、再 init_db 转库。
- 预防：任何"格式迁移"必须先嗅探再建库；建库动作必须放在读取之后。

### 2026-08-16 风控熔断误报：净值口径用现金余额而非账户总权益（用户追问暴露）
- 现象：活体服务报"单日亏损 2.1% 触发停手"，但实际最大单笔亏损仅 -0.095%。
- 根因：RiskManager 喂的是 usdt_total（交易账户现金），开仓冻结保证金、平仓滑点
  都会让现金下降 → 被误判成"亏损 2.1%"→ 假熔断。真实口径应为 total_eq（账户总权益，
  含持仓浮盈亏），当时 total_eq=80,143 几乎没变。
- 修复：两引擎 risk.update_equity 改喂 total_eq；下单数量公式仍用 usdt_total（那是
  可用资金口径，正确）；新增 risk_events 表 + /risk/events API，熔断/恢复各记一条
  （含净值与持仓数快照），熔断事件从此可复盘。
- 预防：风控净值口径必须用"含浮盈亏的总权益"，现金余额只用于下单可用资金判断；
  两个口径不许混用。熔断类事件必须落库，不能只打日志。

### 2026-08-16 决策层"信号失效拒绝"分支恒不可达（读错经验状态）
- 现象：单测审计时发现 decide() 的"信号类教训≥3 拒绝下单"永远不触发。
- 根因：该分支读 bank.relevant()（只返回 trusted），而"信号失效"教训经亏损验证后
  只会进 discarded——语义错配，分支是死代码。
- 修复：_ExpAdapter 增加 discarded()；decide 的信号失效检查改读 discarded。
- 预防：写决策分支前先想清楚数据来源的语义（trusted=证明有用，discarded=证明有害）；
  行为测试必须覆盖每条决策分支的可达性。

### 2026-08-16 熔断强平不释放账本 + 测试污染生产账本（双 bug 连查）
- 现象：测试跑完 production ownership 表残留 BTC/LINK claim（测试未隔离账本）；
  且 LINK 被熔断强平后 claim 未释放（真 bug）。
- 根因：① test_decision_loop 建 trader 时没替换 ledger（用了生产库）；② _liquidate_all
  的 ledger.release 放在 if pos 分支内——交易所持仓已归零时 pos=None，跳过释放。
- 修复：① 测试 ledger 隔离（临时路径）；② release 移出 if pos 分支（journal 是本策略
  事实源，无论交易所持仓是否存在都必须释放）；③ 清理污染 claim。
- 预防：测试必须隔离所有带状态的对象（journal/ledger/exp_bank/threshold），清单核对；
  "清理"类操作不依赖"查询到才执行"，要无条件执行。

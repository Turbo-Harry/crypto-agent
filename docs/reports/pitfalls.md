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

### 2026-08-24 Agent 把正新闻和普通波动误报为拒绝风险族
- 现象：v8/v6 自然 GRASS long 在 `news_score=0.5714`、`composite=0.5157`、bull 11/bear 3 时仍使用 `news_direction_conflict`；DOGE/HOOD 又把普通 `vol_expansion` 或高波动写成 `extreme_market_event`，HOOD 还重复同一 market evidence ID。
- 根因：Prompt 没写清情感分的 [-1,+1] 符号契约，初次判断契约和确定性校验也没有新闻方向及严重事件资格；模型可把 routine volatility 当作单一严重事件，从而绕过两个普通风险族要求。
- 修复：v9/v7 把新闻方向、显式严重事件和 evidence 唯一性提升为机器语义门；正新闻只冲突 short、负新闻只冲突 long，且只有冻结布尔 `extreme_market_event=true` 才允许单事件 reject。
- 预防：任何能绕过多证据门的特殊 reason code 必须由显式机器字段授权；归一化分数必须把区间、中性点和方向写进 Prompt 与同源 validator contract。

### 2026-08-24 Agent 把顺向空头动量与有利资金费误判成风险冲突
- 现象：Harness v6 首批 ADA short Trace 把负的 1H/4H 动量描述为“正动量冲突”，并把正资金费当成空单成本；AAVE 在账户空仓、风控可交易时把市场波动标为 `position_risk_conflict`。两条均形成尚未生效的 `shadow_reject`。
- 根因：Prompt 只要求按方向检查特征，没有明确 long/short 的符号语义；确定性校验只检查概率门和 evidence_id 是否存在，没有核验风险族数量、动量方向和 reason-code 与冻结账户事实的一致性。
- 修复：Harness v7 明确 long/short 动量与资金费成本方向；普通 reject 至少两个不同风险族，单证据只允许 `extreme_market_event`；确定性校验拒绝无方向冲突的 `signal_inconsistency`、无真实账户冲突的 `position_risk_conflict` 和引用有利资金费的 reject，最多修复一次后失败关闭。
- 预防：LLM 的自然语言“解释正确”不能代替机器语义门；每新增可执行 reason code，都要用正反冻结反例证明字段、方向和风险族一致。

### 2026-08-24 Harness 的 4 秒预算被语义修复按请求重复消费
- 现象：首条自然 v7 A/ZAMA 经一次语义修复后总延迟 4,446ms，超过配置的 `AGENT_HARNESS_TIMEOUT_MS=4000`；理论最坏可由两次各 4 秒的请求扩大到接近 8 秒。
- 根因：provider 回调把 4 秒当作每次 HTTP 请求 timeout；LangGraph 只累计展示延迟，没有在重试前计算剩余总预算，也没有丢弃超总预算才返回的结果。
- 修复：Tool Policy v4 将 4 秒定义为整个 Harness 的硬总预算；生产 provider 每次只取得剩余秒数，预算耗尽不再重试，迟到的合法结果也记 timeout 并保持量化基线结果。
- 预防：任何“最多重试 N 次”的外部调用都必须同时有每次预算和端到端总预算；只限制单次 timeout 会把尾延迟按重试次数倍增。

### 2026-08-24 Agent 把方向性盘口失衡和回踩质量误读为绝对深度
- 现象：v7/v4 首轮 SOL short 的点差/预期滑点仅 1.063/1.739 bps、XRP 仅 3.389/5.372 bps，模型却把负 `book/depth_imbalance` 或高 `depth` 分解释为“流动性失败”，两条都形成 shadow reject。
- 根因：冻结特征名 `depth` 容易被自然语言误解为盘口深度，但其公式实际是回踩位置质量；`book` 与 imbalance 是方向压力，不是绝对深度。Prompt 有字段歧义，机器语义门又没有核验点差/滑点。
- 修复：v8 明确字段公式，禁止 depth/book/imbalance 支持 `liquidity_failure`；只有 `spread_bps≥8` 或 `expected_slippage_bps≥10` 才取得该风险族，低于门槛只能影响概率。
- 预防：给 LLM 的特征名必须携带业务公式和单位；凡是能触发离散 reason code 的连续字段，都要有冻结、非 outcome 搜索的机器资格门。

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

### 2026-08-16 测试进程写生产库：scan_decisions 矛盾行 + thresholds 临时 key（DEF-8）
- 现象：生产库 scan_decisions 出现同秒矛盾行（18:58:54 同币一条 open 一条 reject「阈值 85」），thresholds 表出现 /var/folders/... 临时路径 key。
- 根因：test_decision_loop 的 test_threshold_gate 构造 DirectionalTrader 后调 scan_signals()，而 _log_scan_decision 无条件写共享生产库；thresholds 临时 key 同理来自测试 ThresholdLearner 未传 db_path。
- 修复：DirectionalTrader.__init__ 加 db_path 参数，_log_scan_decision 落库走隔离路径；test_decision_loop/test_service_api/test_phase0_review 全对象隔离（journal/exp_bank/ledger/threshold/scan_decisions）。
- 预防：任何"审计/日志落库"接口都必须支持 db_path 隔离；测试构造引擎时必须全对象隔离清单核对（见 test_phase0_review._make_trader 注释）。

### 2026-08-16 套利引擎整线移除（用户决定"不需要"）
- 现象：用户决定不再需要资金费率套利引擎。
- 根因：策略方向调整（非 bug）。
- 修复：engines/decision 套利模块 + 3 个套利测试 git mv 归档 legacy/；worker/app/models/config/watchdog 同步移除；活体 launchctl kickstart 重启后心跳/持仓衔接验证通过。
- 预防：整线移除必须"代码归档 + 服务解挂 + 心跳/watchdog 适配 + 测试闭环 + 文档同步"五件套一次走完；方向性代码行级零改动以保零回归。

### 2026-08-16 采集守护两次整夜停更（进程死亡/挂起 + 无监管）
- 现象：market.db 1m 行情停更 2 小时以上（H5 体检两次告警）；第一次进程存活但挂起，第二次进程直接消失，日志停在 COS 上传失败处。
- 根因：① collect_daemon.main() 循环体无整体 try/except——任何未捕获异常让常驻进程永久退出；② 用户自建的 com.okx.collect.plist 从未加载，且其参数 `--bar 1m` 是无效参数（会 argparse 崩溃循环）；③ 首次挂起疑为网络请求超时路径，进程无外部监管无人拉起。
- 修复：① 循环体整体兜底（失败丢一轮不丢进程 + flush 日志）；② 修正 plist（移除无效 --bar 参数、全量采集）并加载进 launchd（KeepAlive=true 崩溃自动拉起，日志落 data/collect.log）；③ H5 体检持续盯防。
- 预防：**常驻进程必须同时具备三件套**：循环体兜底 + 外部监管（launchd KeepAlive）+ 健康检查（H5）。三者缺一就是"等下一次停更"。

### 2026-08-16 测试进程把假开仓单发到用户飞书（通知通道泄漏）
- 现象：用户反馈"飞书有 BTC 的单子，但看板没有"——test_decision_loop 等跑全量套件时，open_position 的 notify() 把假 BTC 开仓消息真的发到了飞书。
- 根因：notify() 是模块级函数、无条件发送；8 个测试文件中只有 2 个静音了它（与 DEF-8 生产库污染同类——"测试不得触碰外部通道"只靠人记）。
- 修复：结构性修复——DirectionalTrader.__init__ 注入 `self._notify`（exchange.name=='okx' 才指向真实 notify，fake 一律静音 lambda），类体内 12 处调用全部改走 self._notify；生产 okx 行为零变化。
- 预防：外部通道（飞书/库/文件）的写入必须挂在对象上而非模块级函数，测试注入 fake 即自动隔离——不用依赖每个测试"自觉静音"。

### 2026-08-16 采集加速后 watchdog 误杀慢启动引擎（崩溃循环）
- 现象：采集加速上线后服务反复重启（launchctl 持续 exit -15），/health 卡 degraded；err.log 里的 EPERM 是 17:47 的陈旧日志，误导排查方向。
- 根因：加速后首轮扫描 = 8 候选 + 10 回退 ≈ 17 币 × 多周期 K 线请求 + 每日全市场刷新（数分钟），期间 tick 阻塞、心跳停更 > 30s → watchdog 按超时 SIGTERM → KeepAlive 拉起 → 死循环。
- 修复：① watchdog 方向性超时 30→120s；② 长阻塞段（screen_daily 前 + 扫描循环每币）主动刷新心跳——扫描期间心跳缺口 < 10s。
- 预防：任何"扫描/刷新耗时增长"的调整必须连带评估 watchdog 心跳超时余量；排查崩溃循环先看日志 mtime（陈旧日志会误导）。

### 2026-08-16 激进档二连击：首轮扫描时长超 watchdog 心跳余量（第二次崩溃循环）
- 现象：激进第二档（20 币扫描池 + 1M 流动性全市场初筛）上线后服务再次反复重启（exit -15），/health 卡 degraded，H9 快照停更。
- 根因：首轮 tick 阻塞数分钟（screen_daily 全市场初筛 + 20 币信号扫描），即使扫描循环内每币刷新心跳，screen_daily 段的单一前置心跳也不足以覆盖——心跳缺口再次超 120s watchdog 超时。
- 修复（结构性，不再依赖"哪里阻塞哪里补心跳"）：worker._dir_loop 增加**独立心跳线程**（每 10s 写心跳 + 更新 last_hb_dir），心跳与 tick 彻底解耦——任何长阻塞都不再产生缺口。
- 预防：任何"让扫描更重"的调整（更多币/更低门槛/更多周期）都不再需要评估 watchdog 余量；真卡死的引擎仍会被 watchdog 抓住（无任何心跳 vs 线程持续心跳）。

### 2026-08-16 体检 H7 误报熔断（重启窗口内回退读旧 halt 事件）
- 现象：服务重启窗口内用户收到"系统体检异常: H7 风控未处于熔断停手"飞书告警；实际 risk_halted=False、持仓完好。
- 根因：H7 主路径读 /status API，重启窗口内 5s 超时走 DB 回退；回退判定"最近 halt 事件 24h 内→判熔断"，抓到 7 小时前的旧误报事件（服务已重启多次、风控早已重置，且无 recovery 行）。
- 修复：回退窗口 24h→1h，超 1h 视为历史事件，附注"以 /status 为准"。
- 预防：基于事件表的"当前状态"推断必须设置新鲜度窗口，不得把历史事件当现状；恢复类事件缺失时（进程重启重置风控）历史 halt 不算数。

### 2026-08-17 沙盘所有新订单失败：clOrdId 连字符触发 51000（被 code=1 掩盖）
- 现象：XRP/BICO/BNB/GRVT 空单全部"下单异常且反查无法确认成交: code=1 All operations failed"；实测 ETH 多空单同样失败——系统自 P0 幂等键改造后无法开出任何新订单（持仓全是旧代码遗留）。
- 根因：① clOrdId 格式 "ca-…-…" 含连字符，OKX 要求纯字母数字 → 51000 Parameter clOrdId error，被沙盘通用 code=1 "All operations failed" 掩盖（transport 只抛顶层 code/msg，data[0].sCode 的真相被丢弃）；② 另有 BICO/GRVT 沙盘 51001 合约不存在（生产行情有、demo 无，XIAOMI 同款坑）。
- 修复：① clOrdId 改纯字母数字（ca+毫秒+hex8），实测多空四单全 sCode=0；② config.DEMO_UNTRADABLE 黑名单预检拒绝 BICO/GRVT；③ H11 升级为逐笔新增告警（此前只有 24h 聚合）。
- 预防：交易所错误必须穿透到 data[0].sCode/sMsg 再归一（顶层 code=1 是噪声）；新幂等键格式先对照交易所字段规范（只允许字母数字）；"全部失败"类模式问题先做"确定可交易标的上的最小开平"对照实验。

### 2026-08-20 开关型功能上线后旧测试断言未同步（"恰好 1 张条件单"过时红）
- 现象：test_exchange_layers "FakeAdapter 记录了止损条件单" 断言红；一度像止损未挂（安全不变量 2 告急）。
- 根因：断言写死 `len(fake.algos) == 1`；08-19 FLAG_ENABLE_EXCHANGE_TP 上线后开仓挂两张条件单（止损+止盈），止损其实在，是"数量恰好 N"式断言过时。
- 修复：按单据类型分别断言——止损恒 1 张；止盈张数与开关值联动（`1 if FLAG else 0`）。
- 预防：开关型功能（FLAG_*）上线时全库检索受影响的"数量恰好 N"断言并改为按类型/按开关联动断言；测试红先查断言语义再查功能（本例功能是好的）。

### 2026-08-20 进化门回滚值被学习器 min/max 夹逼偷偷抬高
- 现象：设计走查发现（未上生产）：apply_threshold 若复用 calibrate 的 [60,90] 夹逼，回滚到基线 THRESHOLD_INITIAL=35 时会被抬到 60——"回滚"变成静默收紧。
- 根因：夹逼保护是为自动校准设计的；进化门写入口的两类值（已夹逼的提案值、用户拍板的基线值）都不该二次夹逼。
- 修复：apply_threshold 精确写入不夹逼，注释写明两类来源各自的保证。
- 预防：任何"写入口复用防护逻辑"先枚举全部调用方来源；对"恢复基线"类操作，精确性优先于防护性。

### 2026-08-20 monitor 现货平仓异常分支引用未定义变量（潜伏 NameError）
- 现象：上帝类拆分逐行搬移时发现（未在生产触发）：monitor 现货路径 `except Exception as e:` 分支调用 `self._log_order_failure(..., qty, ...)`，但 `qty` 在该作用域从未定义——一旦现货平仓抛异常，会二次抛 NameError 吞掉真实错误且失败不落库。
- 根因：该行从合约路径复制而来，合约路径有局部 `close_qty`/`qty`，现货路径数量在 `t["size"]` 里，复制时没改参数。
- 修复：改为 `abs(float(t["size"]))`（与该笔台账数量一致）。
- 预防：异常处理分支里的变量引用必须逐一核对作用域（异常分支平时不执行，py_compile/测试都难覆盖）；复制相似路径代码时把"取数来源"列为必查项。

### 2026-08-20 SWAP ticker 的 volCcy24h 是币本位，被当 USDT 比较（ANTHROPIC 每天被误杀）
- 现象：唯一配置了合约的美股代币 ANTHROPIC 从未进过候选池（watchlist 历史 0 行、扫描决策 0 条），用户问"为什么没有美股合约交易"才查出。
- 根因：OKX ticker 字段单位随 instType 变——现货 volCcy24h 是 USDT 计价（口径对），合约 volCcy24h 是币本位。daily_scan._stock_pool 直接拿币数（9,257）与 MIN_VOL=100万比较，实际 USDT 额 168 万达标却每天在阶段 1 被刷掉。
- 修复：合约成交额 = volCcy24h × last；与"legacy 单位错位"同模板。
- 预防：任何跨 instType 复用交易所字段前，先查该字段在每种 instType 下的单位定义；新数据源接入时用两个已知标的手工对账一次数量级。

### 2026-08-20 沙盘元数据 lotSz 与真实撮合粒度不一致（51121）
- 现象：ANTHROPIC 沙盘 instruments 接口报 lotSz=0.001，但实测 sz=0.001/0.831 全被 51121（非 lot 整数倍）拒，0.01/0.83 通过——真实撮合粒度是 0.01，元数据细了 10 倍。
- 根因：沙盘（demo）的合约元数据与撮合引擎配置漂移；按元数据 floor_to_lot 产出的数量对撮合引擎非法。
- 修复：okx_adapter 增 51121 自愈——粗化有效粒度 ×10 重试（最多 3 次），学到的粒度按 instId 缓存，止损/平仓单沿用；51121 是干净业务拒绝（未成交），换新 clOrdId 重试无重复成交风险。桩传输层单测 5 项覆盖。
- 预防：交易所元数据当参考不当真理；对"数量/精度"类错误码设计自愈路径而不是直接失败（沙盘元数据问题生产未必有，但自愈两边都无害）。

### 2026-08-20 引擎数量对齐到整张 ctVal，美股合约（ctVal=1 币）全被误杀
- 现象：修完成交额单位后美股合约仍不可交易：ctVal=1 的合约（NVDA/ANTHROPIC 等，1 张 ≈180-500 USDT）在 150 USDT 名义上限下 floor 到 0，被 reject_min_size；实测 9 个美股合约旧口径 6 个被拒。
- 根因：open_position 精度对齐用 `floor_to_lot(qty, ct_val)`（整张），但 OKX 允许小数张（lotSz<1）——真实可交易增量是 lotSz×ctVal（0.01 张 ≈1.8 USDT）。加密币 ctVal 都很小（BTC 0.01），坑一直没暴露。
- 修复：对齐粒度改为 `inst.lot_sz * inst.ct_val`；最小量校验（min_sz×ct_val）不变，仍只向下取整不超发。
- 预防："能不能下单"的换算必须用交易所的最小增量字段组合（lotSz×ctVal），不得用单一字段近似；新增标的类别（美股合约）上线前跑一遍名义上限→数量的全清单核算（本次 9 币核算表进优化记录）。

### 2026-08-20 用首字母判类别：XRP 被 startswith("X") 误标成美股代币
- 现象：watchlist 里 XRP 带 is_stock=1 标记（XLM/XTZ 同样会中招），看账/统计数据被污染。
- 根因：daily_scan 用 `base.startswith("X")` 兜底判美股——X 前缀只是 OKX 美股现货代币的命名习惯，不是类别判据。
- 修复：is_stock 只信池来源标记（_stock_pool 显式打标），删除首字母兜底。
- 预防：类别判定用显式清单/来源标记，禁止用命名模式猜测。

### 2026-08-20 字符串护栏与文件路径耦合：搬代码必须同步搬护栏
- 现象：上帝类按功能拆分后，fix_guard 的 G5/G7/G10/G12 会立即误报"护栏被破坏"——它们用 `"关键字符串" in 文件` 检查修复是否在位，而字符串随方法搬进了新模块。
- 根因：字符串护栏天然与文件路径耦合；重构搬移代码时护栏路径不会自己跟上。
- 修复：拆分同一提交内更新 G1/G5/G7/G10/G12 指向新文件（position_mgmt/risk_monitor/signal_scan），拆分后 `python3 tools/fix_guard.py` 12 条全绿。
- 预防：任何"方法搬家"类重构，先 `rg 目标文件名 tools/` 找出所有字符串护栏/体检项引用，护栏更新与代码搬移同一提交落地；fix_guard 必须进重构后的验证清单（本仓已在全量回归四件套里）。

### 2026-08-20 交易 ID 基于内存长度生成导致多进程撞号覆盖
- 现象：`log_entry` 用 `txn_{len(self.trades)+1:03d}` 生成主键；服务进程与 `directional_trader.py --once` 调试进程同时开仓、或重启后内存列表与库不同步时，会产出相同 ID。落库是 `INSERT OR REPLACE INTO trades`（主键 id），撞号会静默覆盖另一笔，台账丢数据。`_save()` 还把全部历史逐笔全量重写，注释写了增量 UPDATE 但从未实现。
- 根因：ID 绑定进程内列表长度，不是库内唯一键；写库路径把"新增"做成"按主键替换"。
- 修复：新 ID 改为 `txn_{秒级时间戳}_{4位随机hex}`，与旧 `txn_001` 共存、不迁移旧行（lessons.source_trade / trade_features.trade_id 仍引用旧 ID）。`log_entry` 纯 INSERT（主键冲突抛错不覆盖）；`log_exit` / `save_review` / `review` 只 UPDATE 本笔对应列；`_save()` 全量重写仅留给 JSON 迁移与 legacy 回填。
- 预防：主键生成禁止依赖内存集合长度；新增行用 INSERT、更新行用 UPDATE，禁止用 REPLACE 当"保存"。多进程共享库的对象必须用临时库做双实例撞号回归。

### 2026-08-20 交易路径仍裸打 OKX URL（未收敛到 exchange 层）
- 现象：分层架构已有 transport/adapter，但 `engines/daily_scan.py` 自己 urllib 打 `/market/tickers` 和 `/public/instruments`，K 线走 `data/fetch_okx`（history-candles + 24h 文件缓存）；`data/realtime_okx` REST 预热、`tools/deploy_guard` 穿透 `ex.t.private_get`、引擎 `make_cl_ord_id` 直 import okx_adapter。
- 根因：适配层只覆盖了下单/持仓/单币 ticker，全市场 ticker 与幂等键生成没进 ExchangeAdapter；每日扫描沿用研究数据层，等于交易路径旁路。
- 修复：Adapter 增 `fetch_tickers`（SWAP 成交额在适配层 × last 归一成 USDT）和 `new_cl_ord_id`；daily_scan / 信号扫描 / HTTP `/scan/daily` 注入同一适配器；WS REST 预热注入 `fetch_candles`；deploy_guard 改 `cancel_algos`；交易四层静态禁止 `okx.com`/`/api/v5/`。
- 预防：新增 OKX 端点只加在 transport/adapter；`test_trading_layers_no_okx_url` 进分层套件，泄漏立刻红。研究/回测的 history-candles 仍留 `data/fetch_okx.py`（非交易路径）。

### 2026-08-20 沙盘不可交易币进候选池白占名额
- 现象：BICO/WLD/ZEC/HYPE 等生产有行情、沙盘下不了单（51001/51087/51155）的币凭成交额进每日候选，占 12 席中数席；开仓层才 `reject_untradable`，用户看到"为什么这些币在池子里却从不交易"。
- 根因：黑名单只接在开仓预检（`DEMO_UNTRADABLE` ∪ `untradable_symbols`），筛选阶段不查——筛选以为"能交易"，执行层才发现不能。更深一层：传输层只给**签名请求**打 `x-simulated-trading`，公开的 instruments/tickers/K 线走生产全集（400+ 永续），INTC/SOXL 凭生产流动性进池，沙盘下单才 51001。
- 修复：① `screen_daily` 阶段 1 前按黑名单剔除，回退池/信号扫描同步跳过；② 适配器 `venue_for==swap` 再滤一层；③ 沙盘头打到全部 HTTP（含公开接口），仪器表与 ticker 与本账户一致（实测 instruments 436→138）。
- 预防：沙盘适配器的公开行情也必须带模拟盘头；"占名额的候选"必须是这个账户实际能下单的合约。

### 2026-08-20 watchlist 先删后插非原子，崩溃留半截候选池
- 现象：每日扫描重建当日 watchlist 时先 `DELETE FROM watchlist WHERE date=?` 再逐条 `INSERT`，每条走独立短连接自动 commit。中途崩溃（进程被杀/异常）会留下空池或半截池；当天所有开仓决策读的就是这份残缺候选。
- 根因：`storage/db.py` 只有 q/q1/x 三个原语，x() 每条写立刻 commit，没有跨多条语句的事务。DELETE 已落盘后 INSERT 才开始，两步之间没有原子边界。
- 修复：新增 `tx()` contextmanager（正常退出 commit，异常 rollback 后重抛，finally close）；daily_scan 的 DELETE+全部 INSERT 包进同一个事务。顺手把 worker/signal_scan 一轮仓位快照的多行 INSERT 也包进事务（同一轮 = 同一时刻持仓全集）。
- 预防：凡"先清空再写全量"或"一轮多行必须同时可见"的落库，必须用 tx() 而不是循环调 x()。tx() 块内只用 conn.execute，禁止再调 x()/q()（那些会另开连接，看不到未提交变更）。

### 2026-08-20 飞书 `--text` / `--markdown` 都不渲染 GitHub Markdown
- 现象：开仓/平仓/每日看账消息里的 `**加粗**`、`# 标题`、表格在飞书里原样显示星号和竖线，用户看到的是一堆未渲染的 MD。
- 根因：飞书个人消息三种发法能力不同。`--text` 纯文本；`--markdown` 只是把正文塞进 post 的 md 标签，实测同样不解析星号；只有 interactive 卡片里 `tag=lark_md` 的元素才渲染加粗/列表/行内代码。而且 lark_md 是子集，不支持 `#` 标题、围栏代码块、表格——这些即使用卡片也会难看。交易通知此前各自 `--text`，告警通道才用过卡片。
- 修复：统一走 `decision/notify.py`：正文转成 lark_md 子集后发 interactive 卡片；CLI 失败再 `--text` 并剥掉标记。开仓/平仓/看账文案改成「首行标题 + 换行字段」，关键数字用 `**`（卡片里会加粗）。
- 预防：飞书新消息禁止直接 `--text` 塞 GitHub MD；只从 `decision.notify.notify` 发。改文案前用 `tests/test_notify.py` 看卡片 JSON 是否仍是 lark_md。

### 2026-08-20 看账「开仓」虚增：想开仓就算开仓
- 现象：飞书每日看账「开仓 159」对不上「交易 24 笔」。活体库 08-17 开仓意图 49 vs 成交 4；08-19 意图 59 vs 成交 8。当天 ALLO 00:35 记了 open，00:36 下单 51001 失败，台账 0 笔。
- 根因：`scan_signals` 在决策放行后立刻把 `scan_decisions.decision=open` 落下，再调 `open_position`。下单失败 / 最小张数拒绝 / 黑名单拒绝不会撤回这条 open。看账把这条意图数当成成交笔数。
- 修复：① 成交入账（`log_entry` 返回 tid）之后才记 `open`；失败记 `open_failed` 或既有 `reject_*`。② 看账改报「成交 N 笔（已平 M）」，不再把扫描意图叫开仓。
- 预防：「开仓」只等于台账成交；扫描日志的 open 必须后置于成交。计数类文案禁止混用意图和成交。

### 2026-08-20 看账「总盈亏」把百分比加总会失真
- 现象：每日看账写「总盈亏 +4.00%」。两笔都是 +2%，一笔名义 150、一笔名义 50，账户实际只赚 4 USDT，不是账户涨了 4%。
- 根因：`pnl` 存的是价格变动比例，看账用 `sum(pnl)*100` 当总盈亏。比例不能跨笔直接相加。
- 修复：总盈亏 = 各笔 `pnl × notional_usdt` 之和，飞书看账和 `/journal` 都写实际 USDT。
- 预防：凡对外展示的「盈亏/盈利」默认是 USDT 金额；百分比只作为单笔括号备注，禁止把多笔百分比加总当总收益。

### 2026-08-20 user_version 已升到最新后,旧索引可能被活体旧进程建回来
- 现象：只读看活体 `crypto_agent.db`，`PRAGMA user_version=2` 且新索引已在，但旧 `idx_anom_status` 仍在。按"version 已最新就跳过迁移"的逻辑，下次重启也不会 DROP。
- 根因：① HTTP 层 `sdb.init_db()` 不带 db_path，TestClient 回归会把新迁移跑到活体库（version 被升到 2、新索引建上）；② 活体进程仍跑旧代码，旧 SCHEMA 里有 `CREATE INDEX IF NOT EXISTS idx_anom_status`，会把刚删掉的旧索引建回来；③ v2 迁移因 version 已是 2 不再执行。
- 修复：SCHEMA 每次 `executescript` 都 `DROP INDEX IF EXISTS idx_anom_status`（幂等），不依赖"迁移还没跑过"。v2 里仍保留同款 DROP。
- 预防：改名/替换索引时，DROP 旧名必须进 SCHEMA（每次 init_db 都跑），不能只放在"只跑一次"的迁移函数里；测试 init_db 必须传隔离 db_path，HTTP 只读端点的 init_db() 默认路径会碰到活体库。

### 2026-08-20 扫描尺子放宽不得自动生效
- 现象：未触发复盘能看出「影线差一点就能出信号」，若机器直接把 REJECT_WICK_RATIO 调低，会用同一批近失样本自我证明，过拟合后交易变多、质量变差。
- 根因：放宽门槛产生的是「以前没做过的新交易」，用提案窗口的画像当效果等于用训练数据当考试。
- 修复：提案只进 experiments；候选影线比只记 A_wick 影子；满 30 笔且 DSR 达标才标 accepted；必须 POST /scan/evolve/approve 才写 kv。config 基线不改，可 rollback。
- 预防：扫描参数的唯一活体写入口是 approve()；fix_guard G13 锁「永不自动改尺子」。新扫描尺子沿用同一闭环，禁止在 scan_signal 里直接改 config 常量。

### 2026-08-23 AI 入口只靠人工维护导致安全指引与代码漂移
- 现象：`llms.txt` 仍链接已归档的 `engines/trading_main.py`；AI 友好文档引用不存在的 `tools/dependency_graph`；AGENTS/README 的启动命令未显式指定模式，而代码默认模式已变成 live；扫描间隔与行情后端说明也过时。
- 根因：入口文档、docs 索引与本地链接只有人工约定，没有 CI 守卫；历史事实与当前运行事实未定义优先级。
- 修复：新增纯标准库 `tools/ai_repo_check.py` 与变异测试，检查入口、本地链接、llms 关键覆盖、docs 全索引和 AGENTS 操作护栏；接入 CI；启动示例统一显式 `CRYPTO_AGENT_MODE=paper`；建立事实优先级与任务路由。
- 预防：关键入口或 docs 有变动必须先过 AI 仓库自检；高风险语义冲突按 AGENTS 安全约束 fail-closed，代码能力不得自动解释成操作授权。

### 2026-08-23 ccxt 适配器名变化导致真实交易通知被静音
- 现象：切换到 `okx-ccxt` 后，开仓/平仓和熔断路径正常执行，但飞书交易通知不再发送；原生 `okx` 正常。
- 根因：引擎用 `adapter.name == "okx"` 判断是否启用外部输出，把同属真实 OKX 的 `okx-ccxt` 当成测试适配器。
- 修复：在 `config.py` 集中维护真实通知适配器白名单，由 `trade_notifications_enabled()` 统一判断；原生与 ccxt OKX 启用，FakeAdapter 静音。
- 预防：外部副作用能力按显式能力/白名单判断，禁止把单一实现名当接口类型；G14 与通知单测同时锁定两种真实适配器。

### 2026-08-23 SQLite 隔离不等于测试完全隔离，JSONL 仍会污染活体
- 现象：测试虽传临时 `db_path`，开平仓事件仍追加到共享 `logs/events.jsonl`；引擎还为写事件反向 import `service.events`，破坏分层。
- 根因：事件实现固定在服务层且路径是全局常量，事件通道没有继承测试数据库的隔离边界。
- 修复：事件实现下沉 `execution/events.py`；环境变量优先、其次 `<db_path>.events.jsonl`、最后才用生产默认文件；引擎注入 `_log_event`，FakeAdapter 直接静音，`service.events` 只保留兼容转发。
- 预防：测试隔离检查必须覆盖数据库、文件和外部通知全部副作用；G15、事件隔离单测和 CI 独立 `CRYPTO_AGENT_EVENTS_FILE` 三层拦截。

### 2026-08-23 通知重试实现升级后，旧测试把第二次调用误判为文本兜底
- 现象：通知实现改为卡片最多重试 3 次后才降级纯文本，旧测试仍断言第一次失败后第二次立即 `--text`，导致实现正确但测试失败；测试还会写共享通知统计 kv。
- 根因：测试只覆盖旧调用次数，没有分别建模瞬时失败与持续失败，也没有隔离 `_stat` 副作用。
- 修复：分别验证“瞬时失败后第二次仍发 interactive”和“持续失败后 3 次卡片 + 1 次纯文本”；测试中替换 sleep、subprocess 和统计写入，避免真实等待、外发与共享库污染。
- 预防：重试测试必须覆盖恢复与耗尽两条状态机路径，并显式封住所有外部副作用。

### 2026-08-23 正则隔离检查误报位置参数已隔离的测试
- 现象：把隔离检查纳入 CI 后，10 处已用位置参数传临时路径的构造调用被报“缺少 db_path”。
- 根因：旧检查仅在调用文本中搜索 `db_path=`/`path=`，不理解 Python 参数位置和嵌套语法。
- 修复：改用 AST 定位构造调用，按各构造器签名同时接受隔离关键字或对应位置参数。
- 预防：源代码门禁应基于语法树而非字符串近似；隔离门禁本身纳入 CI，保证新增测试立即受检。

### 2026-08-23 storage.db 单一初始化标志导致多库 Harness 串库
- 现象：同一进程先初始化一个临时 SQLite 库后，再初始化另一个 `db_path`，后者缺少 Agent Harness 新表，离线记忆测试报 `no such table`。
- 根因：`init_db()` 只用一个全局 `_initialized` 布尔值，首次初始化后跳过后续路径的建表、迁移和 JSON 导入。
- 修复：改为按规范化绝对路径维护 `_initialized_paths` 集合；每个数据库实例独立执行 schema/迁移和一次性导入。
- 预防：所有 paper/shadow/测试库必须以路径为隔离单元；新增存储测试在同一进程连续初始化至少两个临时库，不能只验证单库。

### 2026-08-23 额度/冷却先于信号计算导致反事实样本选择偏差
- 现象：`scan_signals` 在调用结构信号前先检查当日额度和冷却；被这两道门挡住的时点没有候选快照，成交表只包含最终放行样本，无法诚实评价规则或 AI 的增量。
- 根因：旧链路以“是否准备下单”为采集入口，而监督学习需要以“结构候选是否出现”为采集入口；5 分钟扫描读取 1H K 还会把同一机会重复计数。
- 修复：结构信号形成后立即写 `signal_samples`，再走额度/冷却/分数/经验/AI/执行；以币种、方向、周期、K 时间和含配置哈希的策略版本做唯一键，重复 K 不再开第二次；所有拒绝继续用完整 24H 1m 路径结算。
- 预防：研究样本入口必须位于第一个可改变决策分布的门控之前；测试固定验证同 K 12 次只写 1 行，规则/AI 拒绝仍有最终 outcome。

### 2026-08-23 用到期现价回填 AI 否决结果会破坏首触标签
- 现象：旧 `sweep_outcomes` 在 24 小时后只取一个当前价，既无法判断期间 TP/SL 谁先触达，也无法计算 MFE/MAE 和最高/最低点；网络延迟还会改变标签。
- 根因：把终点收益近似成路径依赖结果，忽略止损/止盈障碍和同分钟双触歧义。
- 修复：新增交易所层历史 K 分页与纯函数路径结算器；只有 24H 1m 覆盖完整才落 `signal_outcomes`，同 bar 双触保守按 SL 并标 `ambiguous`，缺数据保持 pending。
- 预防：任何 first-passage 标签必须来自完整时间路径；当前价只能做观测，不能作为路径标签替身。

### 2026-08-23 空单预测沿用多单障碍且终值在触障后被截断
- 现象：扫描层给 short 预测仍构造“stop 在下、TP 在上”的多单障碍，预测直接返回 None；模拟路径一碰 TP/SL 就 break，所谓 24H 终值分位其实是障碍退出价分布。
- 根因：方向障碍、first-passage 与 terminal distribution 三种语义混在一个循环；iid 单步抽样还破坏了收益连续性和波动聚集。
- 修复：按方向构造真实 ATR stop/TP；完整移动区块 bootstrap 路径始终走满 H，终值分布和首触概率分别消费路径；历史概率权重改为 `n/(n+先验强度)` 收缩；无足够路径标签时明确 uncalibrated。
- 预防：预测测试必须同时断言 short 接线非空、终值不在 TP 截断、三类概率和为 1；校准标签只读 `signal_outcomes`，禁止用固定 ±2%/±1% PnL 近似。

### 2026-08-23 极值预测静默排序会掩盖分位模型失效
- 现象：若 q10/q50/q90 发生交叉，直接排序虽然能得到“看起来正常”的区间，却掩盖模型未学到一致条件分布。
- 根因：把展示格式修复误当作统计模型修复，导致异常模型仍可能进入通知或门控。
- 修复：极值模型显式验证分位单调，交叉立即拒绝输出；最高/最低只输出条件分位和 conformal 校准后的概率区间，不写保证点位。
- 预防：任何概率区间都要同时检查 pinball loss、滚动覆盖率和分位交叉；显示层不得重新排序模型结果。

### 2026-08-23 辅助函数插入点错误会让盘口特征整族静默缺失
- 现象：盘口接口有真实 bids/asks，但 `spread_bps`、`microprice_bps`、`depth_imbalance` 和 `depth_slope` 全部返回 `None`；因缺失策略允许留样，主流程不会报错。
- 根因：新增动态 OFI 函数时插在 `_microstructure_features` 的早退分支之后、正常计算体之前，原计算体变成 `_dynamic_ofi` 返回语句后的不可达代码。
- 修复：恢复两个函数的完整边界；补充真实盘口快照回归测试，断言 spread、microprice、depth imbalance 和相邻快照 OFI 均为可达数值。
- 预防：在函数之间插入辅助函数后必须用“非空正常输入”覆盖原函数，而不能只测空值降级；全量特征质量报告要区分“数据源缺失”和“实现恒缺失”。

### 2026-08-23 OKX books 响应有外层 data，不能把外层字典当档位
- 现象：原生 OKX 盘口请求成功却返回 `None`，进而使盘口相关因子全部缺失。
- 根因：`/market/books` 返回 `data=[{bids:[...],asks:[...]}]`；旧实现直接遍历 `data` 中的字典并按 `r[0]/r[1]` 读取，异常又被降级逻辑吞掉。
- 修复：先取 `data[0]`，再分别翻译其 bids/asks，并把价格、数量归一为浮点数；适配器单测固定真实响应形状。
- 预防：交易所响应翻译测试必须保留真实的外层 envelope，不得只用已经扁平化的理想输入。

### 2026-08-23 独立训练三条分位线会在样本外发生大面积交叉
- 现象：合成的单调极值数据上，q10/q50/q90 独立 SGD 训练后在 208 个样本外点交叉，按 fail-closed 规则导致所有预测被拒绝。
- 根因：三个无约束线性模型各自优化 pinball loss，有限样本下斜率没有顺序约束；“训练集各自拟合成功”不等于条件分位全域有序。
- 修复：首版改为 location-shift 受约束模型：共享 L2 正则化中位斜率，q10/q90 使用训练残差分位作截距平移，从模型结构上保证三条超平面平行且不交叉；消费端交叉拒绝仍保留。
- 预防：分位模型必须在样本外逐点统计 crossing count；不得用输出排序掩盖交叉，也不得只检查训练集分位顺序。

### 2026-08-23 嵌套函数里的 locals() 读不到未引用的外层 4H 序列
- 现象：1H/4H K 线均已拉取，但 regime 计算收到的 4H 收盘序列始终是 `None`，4H 趋势特征静默退化。
- 根因：嵌套 `_shadow` 用 `locals().get("c4")` 读取外层变量；`locals()` 只返回当前局部命名空间，未被闭包直接引用的外层 `c4` 不在其中。
- 修复：外层先初始化 `c4=[]`，嵌套函数直接引用闭包变量传给 `compute_regime`，同时用同一序列计算 4H 动量。
- 预防：闭包数据必须显式参数或显式自由变量传递，禁止用 `locals()` 猜测外层状态；多周期测试需断言 4H 输入实际参与计算。

### 2026-08-23 给 ServiceTrader 传临时库不等于所有 HTTP 端点都已隔离
- 现象：服务测试的交易、扫描写进临时库，但 `/analysis/daily` 仍向默认 `crypto_agent.db` 写 `analyses/lessons`；`/analysis/latest`、`/risk/events`、`/anomalies`、`/reconcile` 也读默认库，测试结果混入生产事实。
- 根因：端点持有隔离的 `trader._db_path`，下游 `analyst` 和若干 `sdb.q/x` 调用却省略 `db_path`；构造器级隔离 lint 无法发现函数内部的默认路径回落。
- 修复：`analyst.analyze/run_daily` 全链参数化 db_path 与 notifier；所有相关 HTTP/worker 调用显式传 `trader._db_path`，FakeAdapter 复用静音 notifier；服务测试把默认 DB 指向哨兵并断言零写入。
- 预防：隔离测试除检查构造参数外，还必须设置“默认路径哨兵库”，执行真实端点后断言哨兵业务表仍为空；依赖注入要贯穿调用链，不能只停在最外层对象。

### 2026-08-23 paper 测试读取 directional 活体心跳会形成假绿
- 现象：测试以 `CRYPTO_AGENT_MODE=paper` 运行，代码实际写 `heartbeat_paper.txt`，断言却读取工作区 `heartbeat_directional.txt`；本机活体持续刷新该文件时，即使测试根本没写心跳也会通过。
- 根因：测试没有按实例名解析心跳文件，也没有隔离 PID/heartbeat/tick 运行时目录；共享工作区状态被误当作测试产物。
- 修复：`execution.pidfile` 支持 `CRYPTO_AGENT_RUNTIME_DIR`，CI 每脚本使用独立目录；服务测试读取自身临时目录下的 `heartbeat_paper.txt`。
- 预防：测试运行态文件必须同时断言“正确实例名”和“正确隔离目录”，禁止用仓库 cwd 中可能被活体刷新或遗留的文件作通过证据；依赖 paper 文件名的脚本还必须在导入 `config` 前自行锁定 `CRYPTO_AGENT_MODE=paper`，不能假设调用者总会带 CI 环境。

### 2026-08-23 每日 INSERT OR REPLACE 同一模型会重置生命周期
- 现象：模型已经进入 shadow/accepted，第二天没有新增样本仍以相同 `model_id` 重训，`INSERT OR REPLACE` 把状态重置为 validated，并清空 parent、activated_at 等观察证据。
- 根因：制品身份只取样本 ID 哈希，既不含算法/特征/超参版本，也没有“相同制品直接复用”的幂等门；持久化把训练结果和生命周期状态当成同一份可覆盖数据。
- 修复：模型 ID 改由数据哈希、算法版本、特征清单和关键超参共同确定；相同 ID 直接返回现有状态；新制品先记录 candidate，再记录 OOS validated/rejected，后续状态变化追加到 `model_state_events`。
- 预防：训练任务不得覆盖已有制品的生命周期字段；制品内容不可变、状态只通过状态机更新，重复训练必须有幂等测试。

### 2026-08-23 最近一根未收线 K 会让候选身份和形态在小时内漂移
- 现象：5 分钟扫描读取 1H 最后一根时，交易所可能返回当前尚未闭合的小时 bar；影线、收盘位置和 kline_ts 在小时内持续变化，同一真实机会无法稳定定义。
- 根因：适配器只翻译 OHLCV，没有保留 OKX confirm 字段；信号层默认“最后一行就是已收线”。
- 修复：信号层按 bar 起始时间 + 周期 + 时钟缓冲裁剪 1H/4H 输入，只让已闭合 bar 参与形态、趋势和候选身份；测试在合法闭合信号后追加未收线反向 bar，断言仍使用上一根。
- 预防：任何监督样本主键依赖的 K 线必须先证明已闭合；实时 bar 可做观测特征，但不得充当标签样本身份。

### 2026-08-23 路径标签只检查总行数会漏掉中间断档
- 现象：24H 分钟 K 总行数接近 1440、首尾也覆盖，但中间缺失一分钟或更长时仍可能结算 first-passage；缺口内 TP/SL 顺序不可知。
- 根因：完整性门只校验首行、尾行和 `len(path) >= expected-2`，没有检查相邻时间戳连续性。
- 修复：规范化 K 线后逐对检查相邻间隔，任一间隔大于 bar 周期即保持 pending；同时为原生与 CCXT 区间分页补闭区间、排序和裁窗测试。
- 预防：路径依赖标签的完整性必须同时证明覆盖边界、唯一时间戳数量和内部连续性，不能用总行数近似。

### 2026-08-23 first-passage 模拟只看小时收盘会漏掉小时内插针
- 现象：某小时 high 已越过 TP、close 又回到入场附近，旧模拟判 timeout；真实 1m 标签却会判 TP first，预测与校准对象不一致。
- 根因：移动区块只抽样 close-to-close 收益，生成路径没有 high/low excursion；“完整走满终值路径”修好了终值语义，但没有补足 bar 内首触语义。
- 修复：生产预测同时抽样历史 OHLC 相对前收的 high/low/close profile；首触读取模拟 high/low，同 bar 双触保守按 SL，terminal distribution 仍只读取完整路径最后 close。
- 预防：路径预测和路径标签必须使用一致的障碍可见粒度；若只能用收盘价，输出必须明确标记 close-only fallback，不能冒充 intrabar first-passage。

### 2026-08-23 切换 15m 后不隔离旧 1H/24H 证据会伪造大样本
- 现象：把主周期改为 15m、标签窗口改为 4h 后，模型查询仍会合并历史 1H/24H 候选，旧 active 制品也可能被新策略加载。
- 根因：候选表有 timeframe/horizon，因子试验和模型消费端却没把它们当作证据身份；“同一个字段名”被误当成“同一个统计问题”。
- 修复：所有因子、入场模型、极值模型、校准、Agent 增量与状态机查询限定当前 15m/4h；`factor_trials` 增加 scope 列；制品内嵌 scope，不匹配即 fail-safe 拒绝加载。
- 预防：任何 timeframe、horizon、费率或标签版本变更都必须触发“数据集身份迁移”审计；样本数不能跨口径相加。

### 2026-08-23 并行分支复用 SQLite 迁移版本号会漏执行另一条分支
- 现象：Agent Harness 与 15m 研究分支都曾使用 v12-v14；若数据库先跑过其中一条，合并后会因 `user_version` 已推进而跳过另一条同号迁移，留下缺表或缺列。
- 根因：迁移版本号只保证了单分支内单调，没有在并行分支合并时审计“版本号—schema 语义”是否仍一一对应。
- 修复：保留 15m 研究迁移 v12-v19，将 Harness 顺延为 v20-v22；新增幂等 v23 对账迁移，无论旧库来自哪条分支都重放两套 schema 补齐动作。
- 预防：合并含数据库迁移的分支时必须比较两边完整 `MIGRATIONS`；新增“同号旧库升级”回归测试，不能只验证全新数据库建库。

### 2026-08-23 时间退出若只看全局参数，部署会追溯强平旧持仓
- 现象：直接在 monitor 中用 `MAX_HOLD_HOURS=4` 计算所有 open trade，任何历史持仓在新代码重启后都可能立即超时平仓。
- 根因：将“新策略的开仓契约”错当成“对所有历史交易的当前配置”，没有在建仓时冻结 horizon。
- 修复：新交易落库 `strategy_timeframe/max_hold_hours`，monitor 只对持仓自身的非空 horizon 生效；旧行 NULL 保持原状。到期仍走 reduce-only、撤条件单、释放账本和复盘链。
- 预防：会改变已建仓命运的参数必须在交易创建时持久化；迁移测试必须断言旧 NULL 持仓不被追溯处理。

### 2026-08-23 Harness 有运行 Trace 不等于评价与记忆已经闭环
- 现象：Harness 每次判断都有 `agent_runs/agent_steps`，但 `agent_evaluations` 会永久停在 pending，`/agent/evaluation` 的成熟样本一直为 0；跨五分钟桶重试还可能命中旧幂等运行，却把评价写到新的孤立 run_id。
- 根因：真实 15m/4h `signal_outcomes` 落库后没有成熟 Harness 评价；运行身份混入墙钟桶；记忆导入只认识旧 `outcome_pnl`，也没有消费 Harness 的标准化 `outcome_r` 与策略 scope。
- 修复：以 signal_id + Harness 版本生成稳定 run_id；入口继承候选的 strategy/timeframe/schema；路径结果落库后同步完成 pending→mature 反事实评价，重试不得退回 pending；v24 为 Agent memory 增加 `outcome_r`，worker 按年龄门导入 legacy/Harness 成熟证据。
- 预防：Harness 端到端测试必须覆盖 signal_id 幂等、pending→mature→scoped memory、重复 sweep 为 0、成熟评价不回退；Trace 数量不得作为 Agent 有效性证据，只有带路径标签的成熟评价才可计入。

### 2026-08-23 Harness 配置已开启不等于生产扫描已经接线
- 现象：Harness 的 contracts/context/trace/evaluation 测试全部通过，配置也为 enabled，但模拟盘 `agent_runs/agent_evaluations` 长期为 0，只有旧 `ai_judgments` 在增长。
- 根因：扫描器只在宿主存在 `agent_model_call` 时调用 Harness，`DirectionalTrader` 构造器却从未注入该回调；离线测试只覆盖显式注入入口，没有覆盖生产组装。
- 修复：仅对真实 OKX 模拟盘且 provider key 可用时注入严格 JSON 回调；每个过基线候选先跑 Harness shadow 留痕，再始终运行 legacy AI 形成现役 verdict。FakeAdapter 不启用任何外部 AI，live 不接入新 Harness。
- 预防：可选子系统必须增加“生产构造器→调用点→持久化结果”的组装测试；enabled flag、模块单测或数据库表存在都不能作为已上线证据。

### 2026-08-23 launchd 托管实例不能再用 nohup 抢启动
- 现象：停止 8091 paper 后手工 `nohup` 启动的 PID 与 launchd 自动拉起的 PID 短暂竞速，日志出现两组应用组装信息；最终由 launchd 实例持锁并监听，手工实例退出。
- 根因：`com.crypto.paper` 已设置 KeepAlive，进程退出后会自动恢复；手工启动没有先识别托管关系。应用工厂在端口/引擎锁裁决前会组装依赖并打印 provider ready，因此重复组装日志不等于双引擎成交，但会干扰审计。
- 修复：以端口、`engine_paper.lock`、`paper.pid` 和进程列表四方复核，确认最终唯一实例 PID 99395；空仓且 `/reconcile balanced=true`，8090 实盘 PID 89187 未变化。
- 预防：重启前必须先查 launchd owner；受 KeepAlive 托管的实例只停止并等待 launchd 恢复，或用明确的 launchctl 生命周期操作，禁止同时 nohup。完成声明以最终持锁/监听 PID 为准，不能用 shell `$!`。

### 2026-08-23 没有 validated 特征不等于没有训练标签样本
- 现象：研究库明明有 long 271/short 143 条完整路径，模型训练结果却显示 `n=0, features=[]`，容易被误读为行情或标签采集失败。
- 根因：训练器只在 feature_names 非空时调用 `_load_rows`；特征验证门失败把整批标签行也短路为空，将“有数据但无合格特征”和“无数据”混成同一状态。
- 修复：无论特征清单是否为空都加载当前 15m/4h 标签行；仍因 `not names` 返回 `insufficient_data`，但 n、TP/SL 类别数保持真实。
- 预防：所有停止裁决必须分别报告 data gate、feature gate 和 model gate；上游门失败不得清零已存在的下游可观测样本数。

### 2026-08-23 历史行情并发下载不能因单页 TLS EOF 丢掉整批证据
- 现象：多序列下载接近完成时，一次 `SSLEOFError` 令主线程在 `future.result()` 直接退出；其他线程虽已下载，结果尚未逐序列提交，批次耗时和数据一起损失。
- 根因：公共行情被当作全有或全无的单次调用，没有页面级有限重试，也没有把每条 bar 序列作为独立提交/错误单元。
- 修复：所有请求共用全局限速，单页按指数退避有限重试；future 逐序列提交，失败汇总到 `errors`，任一错误或序列缺失令命令返回非零并禁止进入重放裁决。
- 预防：历史数据工具必须测试瞬时失败恢复、确认 K 过滤、覆盖率、部分失败非零退出和重复回填幂等；下载成功不等于路径完整，重放结算仍需逐分钟连续性门。

### 2026-08-23 历史重放按币种循环会让经验概率偷看未来
- 现象：先完整重放一个币种、再重放下一个币种时，后者早期候选会读取前者更晚时点的已结算结果；预测在代码上发生于候选时点，统计上却使用了未来信息。
- 根因：经验首触概率只按策略 scope 查询全部已结算样本，没有 `as_of` 截止；数据库写入顺序被误当作时间顺序。
- 修复：经验概率与校准查询增加“结果成熟时间不晚于预测时点”的截止条件；历史重放默认关闭经验混合，只使用固定 seed 的信号时点 bootstrap，确保输出不受币种循环顺序影响。
- 预防：任何历史预测必须携带 `as_of`，并用“数据库中预埋未来结果、预测值仍不变”的回归测试证明无泄漏；不能用处理顺序代替事件时间。

### 2026-08-23 生命周期状态迁移不能清空原验证指标
- 现象：Agent 版本已从 validated 进入 active-veto，但 `metrics_json` 变成 `{}`；之后无法证明当时是否真的满足 100 条有效结果、30 条 reject、增量下界为正和非单段主导。
- 根因：通用 `transition()` 把“调用方没有传新 metrics”解释成“用空指标覆盖旧指标”，把状态迁移与证据替换错误绑定。
- 修复：仅在明确传入 metrics 时更新指标；纯状态迁移继承旧 `metrics_json`。样本门同时引用 config 的 100/30 参数，避免生命周期与统计审计漂移。
- 预防：生命周期测试除断言状态，还必须断言验证证据跨 validated→active/observing 保持；任何晋升状态都必须可追溯到当时的不可变指标。

### 2026-08-23 不能用 cp 单独复制 WAL 模式主文件作为只读证据快照
- 现象：原库可正常只读，`cp database.db snapshot.db` 后的副本却报 `unable to open database file`，容易误判为审计器或 schema 损坏。
- 根因：WAL 模式的最新页和恢复状态可能位于 `-wal/-shm`；单独复制主文件不是一致性 SQLite 快照，且只读打开无法替这个残缺副本完成恢复。
- 修复：证据复核使用 SQLite `.backup` 生成一致性快照；有 WAL/SHM 的活库用普通 `mode=ro` 读取最新提交，无 sidecar 的封存快照用 `mode=ro&immutable=1`，再做哈希前后对比与无 WAL 断言。
- 预防：SQLite 快照必须用 backup API、`.backup` 或受控 checkpoint，禁止用裸 `cp` 主文件宣称数据一致；活体运行中不得自行 checkpoint/VACUUM 干扰写入。

### 2026-08-23 记录了影子预测不等于开仓前已有可用预测
- 现象：候选会保存 bootstrap 和 shadow 模型概率，但没有 active 模型或成本后 EV 下界为负时，旧规则仍可开仓；“有预测字段”被误当成“先预测再开仓”。
- 根因：预测链默认 fail-safe 回现役规则，且 `allow_shadow=True` 的展示查询可能返回较新的 shadow 制品，不能代表通过独立验证、拥有决策权的模型。
- 修复：固定止损 -1R、止盈 +2R；真实 OKX 模拟盘新增严格前置闸门，独立加载 active/observing/kept 模型，要求几何严格 2:1 且成本后 EV 的单侧 95% 下界大于 0，否则失败关闭。被拒候选照常结算反事实路径，不切断训练数据。
- 预防：审计必须分别报告“预测已生成、模型已验证、预测影响决策、订单实际提交”四个层级；展示用 shadow 输出不得直接作为决策证据。

### 2026-08-23 预测时是 2:1 不等于成交订单仍是 2:1
- 现象：候选与模型按止损 1×ATR、止盈 2×ATR 预测，但经验层 `stop_adj` 会在下单前把止损放宽到 1.2×ATR，止盈仍为 2×ATR；实际盈亏比降为 1.67:1，通知却继续写 2:1。
- 根因：预测障碍在结构候选阶段冻结，经验修正在执行层二次改变 stop，三条链没有共同校验最终订单几何。
- 修复：严格 2:1 模拟盘启用时忽略 `stop_adj`，成交滑点重锚后仍固定 1×ATR/2×ATR；legacy/Fake 兼容入口保留旧能力，避免无关测试和未重启 live 被隐式改写。
- 预防：盈亏比必须在候选障碍、模型标签和最终成交重锚三个时点分别断言；任何修改 stop/tp 的经验规则都必须同步重建标签与模型，否则不得在严格门生效。

### 2026-08-23 前置模型门会让下游 Agent 评价样本永久为零
- 现象：严格 2:1 门因没有 active 概率模型拒绝全部订单，同时 Harness 位于该门之后，`agent_runs/agent_evaluations` 无法新增；计划要求的 100 条有效 Agent 结果和 30 条 reject 永远到不了。
- 根因：把“下单前调用顺序”误当成“影子评价采样顺序”；没有区分无决策权的候选 Trace 与有权限的 legacy 下单二判。
- 修复：Harness 前移到去重结构候选留样之后、所有额度/分数/2:1 门之前；它只保存 shadow Trace，不改变任何决策。候选被量化门拒绝后仍按同一 4h 路径成熟评价；legacy AI 保持原下单前位置与唯一实际 AI 否决权。
- 预防：多个串联门控都需要反事实评价时，采样点必须位于最前一道可能拒绝的门之前；端到端测试必须断言“上游拒绝 + 下游影子样本仍增长 + 订单为零”。

### 2026-08-23 Agent 有成熟结果不等于能自动证明增量
- 现象：`agent_evaluations` 可以从 pending 变 mature，但 Harness 的 `risk_probability/reason_codes` 没有持久化，`agent_versions` 也没有生产调度入口；即使未来攒够 100/30，Brier、原因稳定性和生命周期仍不会自动产生。
- 根因：Trace、路径成熟、统计评价和版本状态机分别有单测，却缺少生产闭环；运行表只保存 verdict，没有保存评价所需的完整模型输出。
- 修复：schema v26 为 `agent_runs` 增加风险概率与标准原因码；成熟结果按完整 Harness 版本、固定 15m/4h scope 和费用后 R 自动计算增量 EV、单侧 95% 下界、Brier、拦亏精度与分段集中度。worker 自动登记 shadow 版本，100/30 门达标后最多推进到 validated，绝不自动开启 veto。
- 预防：版本化模型的生产验收必须覆盖“输出字段落库→标签成熟→指标生成→状态迁移→权限仍受独立开关限制”，不能用任一中间表有行代替整条闭环。
### 2026-08-23：候选上限小于注册表时，“自动因子挖掘”可能静默失效

- 现象：注册表已有 41 个基础因子，但 `FACTOR_MAX_AUTO_CANDIDATES=40`；旧实现先跑完注册表，再按剩余名额生成交互，因此剩余名额恒为 0，自动交互从未执行。
- 根因：候选数量上限只作用于生成阶段，没有在入口校验注册表总数；同时任意交互没有经济逻辑，生成后也只能被门标为 `hypothesis_only`，不能进入模型。
- 修复：改为 46 个预注册候选（41 个基础/质量特征 + 5 个有理论依据的二阶交互）；挖掘入口显式校验候选不超过上限，取消无假设的任意两两穷举。30 天 1,690 条完整 SWAP 路径实测 5 个交互全部 reject，validated 仍为 0，不晋升。
- 预防：新增候选必须同时给公式、方向、信号时点数据源与经济依据；注册总数超过上限立即报错，禁止静默跳过。

### 2026-08-23：研究层能生成派生因子，不代表开仓模型能消费

- 现象：若派生公式只写在离线挖掘器里，模型制品一旦引用该特征，运行时 `signal_feature_values` 会因原始快照没有同名字段而返回缺失。
- 根因：训练和推理分别实现特征计算，形成 training-serving skew。
- 修复：新增纯函数 `decision.feature_transforms.materialize_derived_features`，由实时采集、历史重放、注册表提取和模型消费四处共用；只读取已冻结的信号时点基础值。
- 预防：每个派生特征必须测试“冻结快照复算”和“模型消费端复算”，不得只测试研究输出。
### 2026-08-23：训练集平均成本不能代替本次候选的成本 R

- 现象：入场模型制品保存训练样本的平均 `cost_r`，运行时所有候选共用该值；相同预测概率下，1% 止损距离与 0.2% 止损距离会得到相同 EV，窄止损交易可能被错误放行。
- 根因：概率模型输出与候选自身的执行几何脱节；样本外训练按逐笔费用评价，推理和 shadow 生命周期却使用平均成本或毛 `pnl_r`，形成决策口径漂移。
- 修复：按每个候选的 `entry/stop` 计算 `2×(taker+slippage)/risk_pct`；样本外选样、实时预测、2:1 门、shadow/observing EV 全部复用逐笔费用后的同一口径。shadow 还要求至少 30 个实际预测放行样本、放行样本净 EV>0，防止靠极低覆盖率或空仓伪装提升。
- 预防：模型制品中的平均成本只允许作为旧制品兼容兜底；任何影响交易的预测必须输出并审计本候选 `cost_r`、成本后 EV 和二元近似盈亏平衡胜率。
### 2026-08-23：永久 shadow 模型不能用 active/kept 作为唯一完成条件

- 现象：`EXTREMA_MODEL_SHADOW_ONLY=True` 会让通过独立 shadow 门的极值模型停在 `accepted`，但完成度审计只认 `kept`，因此该门在设计上永远不可能通过。
- 根因：把“完成独立影子验证”和“获得交易权限并完成上线观察”混为同一状态；极值预测本来只展示区间，不应为了审计变绿而激活。
- 修复：审计在 shadow-only 配置下把 `accepted/kept` 视为极值独立观察通过；entry 模型仍必须 `kept`，极值模型的 `decision_effective` 继续固定为 false。
- 预防：统计门必须存在一条不突破权限边界的可达状态路径；新增状态机时测试每个最终门确实可达。

### 2026-08-23：相隔数分钟的盘口快照差不能命名为事件 OFI

- 现象：`ofi_dynamic` 只在 15m 回踩结构命中后惰性拉盘口；相邻两次采集可能相隔数分钟，却登记为 `order-book events`，模型会把稀疏状态差误当连续订单流。
- 根因：为了降低 REST 调用量复用了信号时点快照，但特征名称、数据源元数据和 Cont 事件定义没有区分“状态差”与“逐事件队列变化”。
- 修复：保留旧字段并明确标为稀疏快照兼容；两个实时后端新增连续 top-5 L2 订阅和 60 秒滚动累加器，按多档 Cont 规则归一。少于 10 个事件或最新事件超过 5 秒时 OFI 明确为缺失；事件数和年龄随候选冻结，Harness/开仓权限不变。
- 预防：所有微观结构特征必须同时声明采样频率、窗口、档位、归一化和 stale 语义；单元测试要覆盖确定性回放、事件不足与断流 fail-closed，公网实测必须附事件数和年龄。

### 2026-08-23：多重检验候选宇宙必须进入因子试验身份

- 现象：注册候选从 46 增至 50 后，DSR 的试验次数发生变化，但旧 `trial_key` 只包含数据哈希和评估版本；相同数据重跑会被 `INSERT OR IGNORE` 当作同一试验，库中证据仍是旧候选宇宙。
- 根因：把特征值数据身份等同于完整评估身份，遗漏会改变多重检验惩罚的候选总数。
- 修复：因子评估升到 `intraday-factor-oos-v2`，把 `candidate_universe` 写入试验键和详情；同数据/同宇宙仍幂等，宇宙变化生成独立证据。
- 预防：任何影响统计裁决的配置（候选宇宙、折数、阈值、成本口径）变化都必须进入评估身份或提升评估版本，并有“数据不变但裁决配置变化”的回归测试。

### 2026-08-23：六维评分写进快照不等于六维因子已进入挖掘矩阵

- 现象：候选快照的 `shadow_dims` 已有 wick/depth/trend/volume/funding/book 子分，但注册表提取只把这些值用于派生交互，没有复制到基础因子结果；因子试验因此把六维基础因子全部误报为缺失。
- 根因：`materialize_derived_features` 混合了“构造公式输入”和“返回完整特征矩阵”两个职责，只更新派生项，遗漏已经注册的六维原始子分。
- 修复：统一特征物化函数显式把六维子分复制到注册因子结果，再计算五个预注册交互；历史资金费 as-of 回放测试同时断言 funding 不再误报缺失。
- 预防：每个注册因子都必须有“非空冻结输入 → 提取结果非空”的覆盖测试；不能只检查快照 JSON 中存在字段或派生公式可运行。

### 2026-08-23：成本公式升级后旧概率模型不能继续拥有开仓权限

- 现象：开仓 EV 从“手续费+滑点”升级为再包含保守资金费后，数据库里的旧 active/kept 模型仍可能被加载；虽然实时推理会重算新成本，但其样本外晋升证据来自旧成本口径。
- 根因：模型加载只校验 15m/4h scope，没有把成本模型版本视为制品兼容性的一部分。
- 修复：入场模型制品写入 `cost_model_version`，加载时要求与当前配置严格一致；缺失或旧版本制品失败关闭，必须按新净收益口径重新训练、验证和晋升。
- 预防：任何会改变标签、净收益或决策边界的公式都必须进入制品签名并在加载端校验；不能只升级训练器版本字符串。

### 2026-08-23：注册了历史可复算因子不等于重放器真的提供了值

- 现象：5m RV/vol-of-vol/HAR-RV 和跨币排名/BTC 残差/市场宽度/相关集中度都在注册表中，但 30 天行情库已有 1m 与 10 币 15m 数据时仍全部显示 `insufficient_data(n=0)`；实时 `correlation_concentration` 也被固定写成 `None`。
- 根因：注册表、实时采集和历史重放分三处实现；实时 5m RV 还错误累计了整个 24h 窗，与登记的短窗口公式不一致，历史链则没有从 1m 因果聚合 5m 或做同一时点横截面。
- 修复：新增共用纯函数，按已收线数据计算最近 1h 的 5m RV、滚动 vol-of-vol、1h/6h/24h HAR-RV，以及至少 5 币的 EMA20 宽度、相关矩阵首特征值占比、1h 动量排名、BTC beta/残差；实时与重放共同消费，历史缺口继续显式缺失。
- 预防：每个注册因子必须同时覆盖“公式单测、实时冻结快照非空、历史 as-of 非空/未来隔离”；不能用注册表有名字代替数据链可达。

### 2026-08-23：研究管线版本不能隐式改变概率模拟随机种子

- 现象：只升级资金费或特征重放版本，OHLC bootstrap 的 seed 也随 `REPLAY_VERSION` 改变，导致同一候选的预测概率和 Brier 跟着波动，版本升级可能偶然看起来更好。
- 根因：把数据管线 provenance 版本与随机模拟算法版本复用成一个身份；二者变化范围不同。
- 修复：新增独立 `FORECAST_REPLAY_SEED_VERSION`，seed 只绑定预测算法版本、标的、K 线和方向；特征/资金费重放升级不再重抽路径。metadata 同时记录两种版本。
- 预防：随机评估必须把数据版本、算法版本和随机种子身份分开；非随机算法变更的回归测试必须证明预测字节级复现。
### 多策略共用候选表时，若没有 strategy_id 会污染训练与 readiness（2026-08-23）

- 现象：准备把 `B_breakout` 接入与 `A_pullback` 相同的 15m/4h 首触结算时，原
  `signal_samples` 唯一身份和训练查询都只区分方向、周期与 `strategy_version`；B 样本会与
  同 K 的 A 冲突，或被误计进 A 的 300 条训练、概率基线和统计完成度。
- 根因：早期候选监督链按单策略设计，把“规则配置版本”当成“策略身份”，没有独立的
  `strategy_id` 维度；共享标签表后这个假设不再成立。
- 修复：schema v27 增加 `strategy_id`，候选哈希与版本显式包含策略；A/B 同币同方向同 K
  可并存。因子挖掘、入场/极值训练、经验概率、校准和完成度审计默认只消费
  `A_pullback`；B 只形成独立 shadow 结果，仍无执行权限。
- 预防：以后新增策略必须先完成“身份、唯一键、训练 scope、校准 scope、readiness scope”
  五项隔离测试，再接共同结果表；不能用总候选数替代某一策略的有效样本数。
### 占用生成器空 commit 写尾空格，静态检查会被自身状态阻断（2026-08-23）

- 现象：代码与文档内容均正常，但 `git diff --check` 报
  `docs/AGENT_NOTES.md` 活跃占用行 trailing whitespace。
- 根因：`tools/agent_notes.py` 无论 commit 是否为空都拼接 `" | {commit}"`，空值时留下行尾
  空格；每次 claim 都会重新制造。
- 修复：仅在 commit 非空时输出第四段；三段格式继续被现有解析器兼容。
- 预防：生成机器维护 Markdown 时同样运行 `git diff --check`；修内容前先修生成器，不能只
  手工删一次尾空格。
### 2026-08-23 多策略重放不能只隔离候选，不隔离因子试验身份

- 现象：实时 A/B 候选已用 `strategy_id` 隔离，但 `factor_trials` 仍只有 timeframe/horizon；直接跑 B 因子门会让“同名最新试验”覆盖 A。B 的突破窗口/放量参数也未进入候选配置哈希，改参数后可能被旧候选去重吞掉。
- 根因：schema v27 只完成样本表策略隔离，因子证据表和候选配置身份没有同步沿依赖链检查。
- 修复：schema v28 为 `factor_trials` 增加 `strategy_id` 并重建复合索引；因子 trial key、挖掘入口、模型特征选择全部显式绑定策略；B 参数仅进入 B 的 `config_identity`，改 B 不会让 A 的候选身份漂移。
- 预防：新增策略身份时按“候选 → 标签 → 因子试验 → 特征选择 → 模型制品 → readiness”逐层核对，不能只验证同 K 候选可并存。

### 2026-08-23 CCXT 全市场 ticker 不传 instType 会让 SWAP 候选池静默归零

- 现象：模拟盘启动日志显示“只做合约观察池 9 个”，阶段 1 又从 9 变成 0，最终每日候选始终退化为固定五个主流币；配置中的 9 个美股合约虽然被追加，但因成交额为 0 也全部被刷掉。
- 根因：OKX 的 CCXT `fetch_tickers()` 无参数默认返回 SPOT；适配器再按 `:USDT` 过滤 SWAP 得到空集。显式请求后又发现批量 ticker 不填 `base`，若只读该字段会继续生成 `None-USDT-SWAP`；随后只剩人工追加且没有 ticker 的美股清单。
- 修复：`CCXTAdapter.fetch_tickers` 按目标场所显式传 `instType=SWAP/SPOT`，并从 markets/ticker symbol 可靠恢复 base，继续在适配层统一换算 USDT 成交额；增加桩测试锁定请求参数、缺 base 映射和两种场所的成交额语义。
- 预防：任何“全市场”接口必须回归验证返回场所、数量和非零关键字段；不能只测单 ticker 或把 fallback 成功误当扫描成功。

### 2026-08-23 多策略隔离不能止于因子表，模型选择与 readiness 也必须带 strategy_id

- 现象：schema v28 已隔离 A/B 因子试验，但 `model_artifacts` 仍无一等策略列；readiness 又按全库统计 validated 因子和 kept 模型。未来 B 若先通过，会错误点亮 A 的完成门；更晚创建的 B active 模型还可能先被 A 查询命中，再因 artifact 策略不符返回 None，遮挡本来有效的 A 模型。
- 根因：模型制品把策略身份只埋在 artifact JSON 和 model_id 前缀，SQL 选择、生命周期父子替换、预算锁与完成度审计无法可靠按策略约束。
- 修复：schema v29 为 `model_artifacts` 增加 `strategy_id` 与复合索引；训练、推理、shadow/观察、父模型选择、默认回滚、预算锁和 readiness 全部按策略过滤，观测接口显式返回策略；回归构造 B-only 因子/模型证明不能点亮或遮挡 A。
- 预防：策略隔离验收必须覆盖“采样→标签→因子→模型制品→模型选择→生命周期→预算→readiness”完整路径，不能以 JSON 中存在字段代替数据库约束。

### 2026-08-23 联合模型有多维输入不等于评价结果可审计因子组合

- 现象：开仓模型确实把多个通过门的因子作为同一向量做 walk-forward，但 `evaluate_rows` 只返回折指标，不返回特征清单；外部证据无法从评价结果确认究竟评了哪组 Combo。
- 根因：把模型制品中的 `feature_names` 当成足够证据，忽略独立评价函数也会被研究脚本和测试直接调用。
- 修复：评价结果无论成功或样本不足都显式返回有序 `feature_names`，回归断言双因子同向量完成完整 5 折样本外评价。
- 预防：任何组合模型的评价结果必须同时冻结数据身份、特征清单、成本版本与折指标，不能只返回一个总分。

### 2026-08-23 策略留样已自动化不等于该策略研究链也已自动化

- 现象：B 突破会持续写入独立 15m/4h 候选，但 worker 每日只对默认 A 调用因子、概率和极值训练；B 只能靠人工历史脚本评价，paper 自然样本永远无法自动进入模型门。研究任务异常又被空 `except` 吞掉，且时间戳已前移，一次失败会静默停 24 小时。
- 根因：新增 `strategy_id` 时只参数化了研究函数，没有审计生产调度器的调用集合、异常语义和重试时钟。
- 修复：抽出 A/B 分策略 `run_intraday_research_cycle`；每个策略分别运行 61 项因子与 long/short 概率、极值训练，再统一推进生命周期。失败同步写 `/error` 与 `engine_errors`，15 分钟退避后重试，成功仍保持 24 小时周期。
- 预防：新增策略的接线验收必须覆盖“形成候选→自动结算→定时研究→模型生命周期→观测”；后台任务禁止 `except: pass`，并测试失败后的可观测性和重试时间。
### 2026-08-23 多策略完成度不能只隔离候选与模型
- 现象：候选、因子、模型已经按 `strategy_id` 隔离，但自然平仓仍只按 15m/4h 统计，Agent 版本也取全局最新；B 的证据可能让 A 的 60/30 平仓门或 Agent 增量门误变绿。
- 根因：`trades` 与 `agent_versions` 缺少一等策略归属，`/research/readiness` 也固定审计 A，证据链只完成了局部隔离。
- 修复：schema v30 为两表补 `strategy_id`（旧数据默认 A），开仓台账显式保存策略；完成度审计、因子接口、模型快照和 Agent 版本查询全部按策略过滤，并开放只读 `strategy_id` 查询参数；审计展示的 `strategy_version` 复用采样器配置身份算法，B 不再显示成 A 的 pullback 版本。
- 预防：新增策略时逐表核对“候选→标签→因子→模型→Agent→成交→完成度”整条证据链；任何统计门都必须包含策略过滤测试，禁止只看上游表已隔离就推断闭环已隔离。

### 2026-08-23 配置版本快照不能冒充独立市场机会
- 现象：参数或特征 schema 更新后，同币、同方向、同一根 15m K 会按新 `strategy_version` 再留一份审计快照；A 的 26 个 signal_id 实际只有 23 个独立市场机会。若训练和完成度直接数 signal_id，会把重复路径当独立样本，夸大类别数、Agent 结果与显著性。
- 根因：原唯一键刻意允许配置版本变化产生新快照，但下游研究没有再区分“审计版本快照”和“统计独立观察”两个口径。
- 修复：schema v31 增加只读 `signal_samples_canonical` 视图；原始快照全部保留，但每个 `strategy_id+symbol+direction+timeframe+kline_ts` 只选择最新一条。因子、概率/极值训练、模型生命周期、经验预测、校准、Agent 评价、研究报告和 readiness 全部改读 canonical 视图。
- 预防：样本量门必须按自然实验单位计数，不能按数据库主键计数；回归固定“原始 300 行、其中一行是配置重复→训练样本仍为 299”。

### 2026-08-23 看板不能把历史总量冒充当前策略成熟度

- 现象：旧首页突出显示历史 46 笔平仓、21210 条持仓快照和 18500 条扫描决策，用户却无法判断当前 15m 策略能否开仓、2:1 是否有效、Agent 是否已提升；当前策略实际只有 1/60 笔自然平仓。
- 根因：看板按“数据库有什么表”组织信息，没有按“用户要做什么决策”组织；历史全量成交、当前策略自然样本和候选路径标签混在同一视觉层级。
- 修复：决策中心改为结论优先，先显示保持空仓/管理持仓/熔断/服务异常，再分开呈现当前权益与仓位、2:1 理论、15m readiness、因子/模型/Agent 证据和预算锁；快照总数与扫描总数退出首页主卡。
- 预防：任何统计卡必须标注 scope、自然实验单位和用途；运营总量不得替代策略统计门，候选路径不得标成实际成交收益。

### 2026-08-23 Agent 提案落样时从 decision 反向 import engines
- 现象：主动提案专项测试通过，但 `tools/code_graph.py --check` 报 `decision → engines` 反向依赖。
- 根因：提案编排函数为了写 `signal_samples`，直接 import 了引擎层候选采样函数，把“模型决策”和“交易研究接线”混在同一层。
- 修复：`decision.agent_proposals` 只负责快照、契约、几何和提案审计；候选落样改由 `engines.signal_sampling.record_agent_proposal_sample` 实现，并通过显式 callback 注入。
- 预防：decision 层需要上层能力时只能定义数据结果或回调协议；不得为了复用方便反向 import engines/service。新增模块在继续集成前先跑代码图检查。

### 2026-08-23 代码图漏层会把接口绕过误判为全绿
- 现象：旧代码图报告无分层违规，但 `storage` 实际反向 import `decision`；HTTP 还直接读取引擎私有协作者、散写 SQL，并把 `tools` CLI 当生产依赖。
- 根因：层级表遗漏 `storage/interfaces`，检查器只判断目录方向，没有检查服务直连数据库、核心反向依赖工具脚本和跨包私有符号。
- 修复：补齐稳定契约层与持久化层，服务经 `TradingRuntimePort`、`decision.api`、`storage.query_api` 调用；Agent 契约下沉到 `interfaces`，台账/持仓/运行异常改走 repository；代码图新增三类接口绕过守卫。
- 预防：目录拆文件不等于模块解耦。每次新增跨包 import 都要同时检查依赖方向、公开契约和数据所有权；`code_graph --check` 的层级清单必须覆盖全部核心包。

### 2026-08-23 macOS 沙箱会把 py_compile 缓存写到仓库外
- 现象：源码目录可写，但系统 Python 3.9 执行 `python3 -m py_compile` 时尝试写
  `~/Library/Caches/com.apple.python/...pyc`，在 workspace-write 沙箱中报 `PermissionError`。
- 根因：该 Python 的字节码缓存前缀指向用户 Library；源码可写不代表解释器默认缓存目录也在
  授权范围内。
- 修复：验证命令显式设置 `PYTHONPYCACHEPREFIX=/tmp/crypto-agent-pyc`，只改变编译缓存落点，
  不改变源码、导入语义或运行配置。
- 预防：沙箱内运行语法编译时统一指定可写临时缓存目录；不要为写用户缓存申请扩大权限，
  也不要把 `__pycache__` 或 `.pyc` 当交付产物。

### 2026-08-23 全局 paper 测试环境会改变模式敏感状态机的前置条件
- 现象：用户关闭模拟盘连亏冷却和半仓后，生产行为符合新配置，但全量 CI 的
  `CRYPTO_AGENT_MODE=paper` 让原“通用冷却状态机”断言整段失效；决策测试也仍期待 paper 半仓。
- 根因：测试依赖进程级默认模式，没有在用例内冻结自身要验证的 live/paper 前置条件；配置变化后，
  状态机本体测试和模式门控测试相互污染。
- 修复：通用冷却状态机显式切到 live 并在结尾恢复所有配置；模式门控另测 paper 不冷却/不半仓
  和 live 保持冷却/半仓。生产参数与生产代码均未修改。
- 预防：凡行为受实例模式或 feature flag 控制，测试必须在用例内保存、设置、恢复相关配置，
  同时覆盖开/关两侧；不得依赖 CI 的全局环境恰好等于测试前置条件。

### 2026-08-23 未收线 K 线配合 INSERT OR IGNORE 会永久冻结伪终值

- 现象：旧 `klines` 与 OKX 官方终值抽样比对时，1m 每 99 根有 76～80 根不同，15m 有
  94 根不同；采集任务仍显示成功，导致“每天有数据”被误认为“数据可用于验证 2:1 模型”。
- 根因：旧采集把接口返回的当前未收线 K 线一并写入，未保存/校验 `confirm`；唯一键冲突后
  使用 `INSERT OR IGNORE`，后续最终 OHLCV 无法覆盖第一次局部快照。表中又缺来源、场所、
  时区和 as-of，无法证明现货/合约身份或事件时点。
- 修复：新增 `klines_v2`，只接收 OKX `USDT-SWAP` 的 `confirm=1` 终值，并校验收线时间和
  OHLC 不变量；同 K 用原始值哈希做终值 UPSERT。每日回补前一 UTC 日并精确审计缺口，失败
  返回非零并按 15 分钟重试。旧表保留但登记为 `legacy_unverified`，当前 15m 重放优先且只读 v2。
- 预防：采集验收必须同时证明“已收线、场所、时区、as-of、连续性、幂等修订、失败可观测”；
  定期抽样与官方终值逐字段比对。收到非空响应、表行数增长或任务存活都不能替代数据质量证明。

### 2026-08-23 源缺口例外必须与已落库终值互斥

- 现象：OKX `history-candles` 在三个合成合约时间槽返回空，但此前 `candles` 已返回并落库
  confirmed 终值；并发复核一度留下“已有 K 线，同时标记源缺口”的陈旧元数据。
- 根因：缺口同步只比较本次历史响应，没有把另一官方端点已经持久化的 confirmed 行作为更强证据；
  并发的全量/定点复核又使写入先后顺序不确定。
- 修复：缺口写入前查询严格表，已存在终值的时间槽不得登记缺口；任何后续 UPSERT 与缺口删除
  同事务执行，审计再以 `NOT EXISTS klines_v2` 做防御性反连接。
- 预防：质量例外必须和有效事实建立数据库级互斥语义；多端点、多任务并发时不能只凭内存快照裁决。

### 2026-08-23 子脚本打印失败但退出 0 会让守护进程反向误报成功

- 现象：日志同时出现“COS 上传 成功”和内层“上传 market.db：失败”。
- 根因：`upload.py` 只打印 `upload_file=False`，`main` 没聚合结果并始终以退出码 0 结束；守护进程
  只能看到子进程退出码，因而把失败包装成成功。
- 修复：主流程聚合数据库与可选历史包上传结果，任一失败或源文件不存在均返回 1；守护进程继续
  以退出码为唯一成功信号并保留最后一行明细。
- 预防：被调度脚本必须有机器可读失败语义；人类日志中的“失败”不能替代非零退出码和回归测试。

### 2026-08-23 Agent 精准率门不能合并 legacy 与不同版本样本

- 现象：readiness 曾把 legacy AI、旧 prompt/context/schema 和当前 Harness 的成熟结果合并计数，
  看似更接近 100/30 门，但没有任何一个可部署版本真正拥有这些样本。
- 根因：统计按 signal_id 去重，却没有先按策略、模型、prompt、context、schema、retrieval、工具策略
  和价格口径冻结完整实验身份；版本变化后的覆盖率和精确率不可归因。
- 修复：完成门只读取当前完整 Harness 版本的自然成熟样本；版本身份加入策略和 provider 价格口径，
  legacy/旧版本只作诊断。晋升还必须同时满足费用后增量 EV 下界、Brier、Trace、证据和集中度门。
- 预防：champion/challenger 只能在同一冻结证据哈希上配对；任何跨版本样本相加都不得用于晋升。

### 2026-08-23 只有 input_hash 没有输入快照，Agent 结果无法重放也无法计成本

- 现象：旧 `agent_runs` 只有输入哈希、verdict 和粗粒度 token，无法证明模型看到什么；证据 ID、
  缺失信息、confidence、cache token 与 provider 费用也没有持久化。
- 根因：把哈希当成完整审计证据，并把模型成本忽略为近似 0；结果既不能同输入重放，也可能把
  `saved_loss > missed_profit` 的微小假优势误判为正增量。
- 修复：schema v33 保存 canonical 输入快照、跨版本 evidence hash、结构化理由/证据/缺失字段、
  cache hit/miss token 和美元费用；只有冻结账户风险预算可复算时才把美元成本换算为 R，否则阻断晋升。
- 预防：Agent 评估必须达到 Trace/概率覆盖 100%、reject 证据覆盖 100%，并满足
  `saved_loss > missed_profit + model_cost`；只读 GET 评价不得暗含 schema 初始化或迁移。

### 2026-08-23 同一根 15m K 的跨币候选不能拆到训练和测试两侧

- 现象：旧 purged walk-forward 按候选行切块，同一市场时点批量产生的多币候选可能横跨 train/test；
  模型 precision 又拿低覆盖率子集与全体候选胜率比较，覆盖率变化会被误报为提升。
- 根因：把数据库行当成独立实验单位，且没有冻结同覆盖率的现役连续分对照。
- 修复：先按 `kline_ts` 分组再做扩展窗、purge 和 4h embargo；每折用现役 `shadow_score` 排名前 K
  作为完全同覆盖率基线，并要求实际 OOS 放行净收益 95% 下界大于 0、放行样本至少 30。
- 预防：样本外拆分必须以自然市场事件为单位；任何 precision lift 都同时报告两侧选样数并断言相等。

### 2026-08-23 Python 3.12 运行链不能混入旧 Python 3.9 lib

- 现象：全量回归最初 10 个脚本因 NumPy/Pydantic 二进制 ABI 加载失败；paper LaunchAgent 切到
  `.venv` 后仍启动失败，因为 ccxtpro 行情模块又把仓库 `lib/` 插到 `sys.path` 最前。
- 根因：CI、测试脚本、服务入口与两种实时行情模块分散维护兼容路径；只修一个入口不能改变后续
  import 对全局 `sys.path` 的再次污染。
- 修复：CI 统一 `PYTHONPATH=.`，测试与 paper 服务运行链全部移除旧 lib 注入；paper LaunchAgent
  改用 Python 3.12 `.venv/bin/python -m service.main`，G22/G23 用 AST 同时捕获单行和跨行注入。
- 预防：仓库 `lib/` 只视为不可混用的旧产物；解释器、依赖目录和 LaunchAgent 必须作为一个运行身份验收。

### 2026-08-23 活体状态文件存在时，隔离测试不能断言仓库根文件近期不存在

- 现象：阈值进化门测试在 live/paper 活体刚更新根状态文件后失败，尽管测试对象实际只写临时目录。
- 根因：测试把外部活体文件的存在/mtime 当作自身写入归因，违反并行实例下的确定性隔离。
- 修复：断言改为测试实例的 gate 路径位于临时目录且对应文件存在；不再读取或修改活体状态文件。
- 预防：隔离测试只证明自己的依赖注入和写入目标，不能用共享外部状态的“应该不存在”作替代证据。

### 2026-08-23 Harness 授权开关与扫描消费链脱节会形成假上线

- 现象：配置中存在 `AGENT_HARNESS_VETO_ENABLED`，生命周期也能进入 `active-veto`，但生产
  `harness_judge` 始终硬编码 shadow，扫描主链又丢弃 Harness 返回值；打开开关也不会影响下单。
- 根因：证据收集、版本晋升、策略核和最终下单前消费分别完成，却没有用同一完整版本身份把四段
  闭环；测试只覆盖了注入 `PolicyKernel(veto_enabled=True)`，没有覆盖真实扫描链。
- 修复：统一模型/prompt/context/schema/retrieval/tool/pricing/strategy 版本身份；只有该版本处于
  `active-veto/observing/kept` 且授权开关开启时策略核才能 veto，并在全部量化硬门通过后消费结果。
  reject 还必须满足亏损概率与信心双 0.70 门；低置信 reject 只留 shadow 审计。
- 预防：任何“开关已开启/功能已上线”的验收都必须从配置一路验证到最终副作用点；至少覆盖
  未晋升不拦、匹配版本晋升后拦、基线拒绝不可恢复三条端到端路径。

### 2026-08-23 已落地迁移不能靠修改同一版本补列

- 现象：paper 持续产生 A 候选，provider 也显示 ready，但新 Harness 运行数始终为 0；模型实际完成
  推理后，`agent_runs` 写入报 `no column named evidence_hash`，异常又被图节点静默吞掉。
- 根因：`evidence_hash` 在 v33 已部署后才追加进同号迁移；活体库已有 `user_version=33`，不会重跑
  v33。全新临时库直接从最终 SCHEMA 建表，所以全部单测假绿。
- 修复：新增 v34 幂等重放 v33 完整列集；Trace 写入失败时明确打印错误并强制取消 Agent veto，保留
  量化基线动作。增加“user_version=33 但缺晚加列”的真实升级测试和持久化失败安全测试。
- 预防：迁移只追加新版本，已发布版本函数即视为不可变；迁移测试必须同时覆盖全新库和已标旧版本、
  但 schema 不完整的活体形态。任何 Agent 权限消费必须以 Trace 成功持久化为前提。

### 2026-08-23 反事实 Harness 不能被基线空模型状态循环锁死

- 现象：v2 修复 Trace 后连续四个不同候选全部返回 abstain、风险 0.55、信心 0.60；理由都围绕
  `no_validated_active_model` 或预测未校准，合格 reject 永远为 0。
- 根因：prompt 只禁止“因空模型而 reject”，没有禁止“因空模型而 abstain”；模型把治理状态当成
  市场风险证据，形成“没有模型→不标风险→没有 reject 标签→Harness 永远无法验证”的循环依赖。
- 修复：v3 明确这是结构候选的反事实风险标注；空模型、未校准预测和 route abstain 只属治理元数据，
  不得进入 missing/abstain 理由或固定概率。风险必须来自冻结市场、消息、流动性和账户冲突证据。
- 预防：prompt 验收不能只看 schema 合法；还要检查不同输入的概率分辨率、理由来源和拒绝覆盖，任何
  与下游验证门互相依赖的上游状态都不得作为模型拒绝提供标签的理由。

### 2026-08-24 Brier 不差于基准不能单独证明概率有分辨率

- 现象：若模型对所有候选输出同一个风险概率，且该常数恰好接近样本损失率，Brier skill 可等于 0；
  原晋升门允许 `brier_skill>=0`，理论上常数模型仍可能借其他门获权。
- 根因：校准和分辨率是不同性质；Brier 相对频率基准只检查综合误差，没有显式要求不同输入得到
  不同风险分数。
- 修复：评价新增 `probability_mean/std`；晋升和观察期均要求风险概率标准差至少 0.03，常数或近常数
  输出明确拒绝/回滚。覆盖率、Brier、reject 证据和净 EV 门全部保留。
- 预防：概率模型验收同时检查覆盖、校准、分辨率和决策价值，不能用单一综合分数替代四类证据。

### 2026-08-24 多策略已有共同标签链，但 Harness 只接 A 会浪费反事实证据

- 现象：B_breakout 已按相同 15m/4h 口径冻结候选并成熟路径，30 天 confirmed 样本也多于 A，
  但在线 Harness 只在 A_pullback 分支调用；版本切换后若 A 暂无结构信号，Agent 新版本样本停在 0。
- 根因：早期 Harness 接线写在 A 的 `sig` 分支内，没有把“冻结候选评估”抽成策略中立入口；B 虽有
  `strategy_id` 隔离和共同 outcome，却只进入因子/模型研究。
- 修复：抽出共享候选 Harness 方法；B 首次去重留样后也运行同一 prompt/context/Trace，版本和评价按
  `B_breakout` 独立，且调用端固定 `allow_veto=False`，不改变 B 永久 shadow/无执行权语义。
- 预防：新增策略进入共同候选标签表时，同步核对采样、模型、Harness、成熟评价四条研究消费链；是否
  有执行权必须由调用端显式传入，不能从“已采样”推断。

### 2026-08-24 JSON 合法不等于 Harness 证据语义合格

- 现象：v3 首两条自然结果都返回 0.55/0.60 abstain，并带 `insufficient_evidence`，但
  `missing_information=[]`；abstain_reason 还复述“预测未校准/无已验证模型”等 prompt 已禁止的
  治理元数据。Pydantic/schema 全部合法，原图仍把它们记为 completed。
- 根因：验证节点只检查字段类型、枚举和范围，没有检查 reason code 与缺失证据的一致性，也没有核对
  reject 的 evidence_id 是否逐字来自冻结 provenance；prompt 约束没有确定性执行层兜底。
- 修复：中立契约要求 `insufficient_evidence` 只能配 abstain 且必须列出具体缺失市场信息；图验证再
  排除治理元数据、核对 reject 证据锚。语义失败最多带原响应和违规原因修复一次，两次调用成本累计、
  两个 MODEL step 分别留痕；仍失败则 `schema_error` 且保持 baseline，不进入有效评价。
- 预防：模型输出验收分结构、语义、证据锚、决策价值四层；任何自动修复都必须有次数上限、累计成本、
  尝试级 Trace 和失败关闭，不能无限重试或覆盖首个坏响应。

### 2026-08-24 Prompt 身份热重载不能替代进程级代码部署

- 现象：旧 paper 进程先热重载 `AGENT_HARNESS_PROMPT_VERSION=v4`，随后两个自然候选在惰性导入
  新图代码时出现 `ImportError: AgentSemanticError`；进程内已缓存的兼容 re-export 仍是旧符号集合。
- 根因：配置文件可热重载，但 Python 模块代码与 `from ... import *` 的导出快照不会随配置一起原子更新；
  版本身份已经显示 v4，执行代码却仍是旧内存，形成短暂的身份/实现错配。
- 修复：完整重启 paper 后重新导入全部模块，PID 75159→79777，接口与扫描恢复正常；这两次失败保持
  baseline，不形成 Agent 否决或订单。
- 预防：凡 Prompt/schema/context 身份变更同时依赖新代码符号，必须把“提交→全量测试→进程重启→
  新 PID 健康与自然扫描验证”视为一个原子部署；不得把配置热重载提示当成代码已部署证据。

### 2026-08-24 降低执行摩擦不等于创造方向优势

- 现象：90 天 10 币中，信号价限价虽免入口滑点但填单率接近 99%，A/B 仍约 -0.51R/-0.50R；
  再把限价改善到可回收约 20bp 成本，填单率降至 32%/47%，每候选亏损缩小但每成交仍为
  -0.37R/-0.40R，所有时间折、月份和标的都为负。
- 根因：信号本身的 TP-first 精度不足，降低手续费/滑点只能减少负期望幅度；信号价挂单几乎必填，
  没有形成有效选择，而更深限价的成交子集也存在不利选择。把“亏得少”误写成“已有 alpha”会让
  执行优化越权替代方向验证。
- 修复：研究裁决同时报告 fill rate、每成交/每候选净 EV、市场事件聚类下界、时间折、月份、单币
  贡献和未看标的留出；两种限价均明确 `stop_no_promotion`，未接入订单链。
- 预防：执行变体必须在固定方向信号与固定 -1R/+2R 下独立验证；费用改善不能抵扣正 EV 下界、
  跨折一致性和留出集要求，也不能以低成交率造成的空仓收益冒充选时准确率。

### 2026-08-24 经典极端反转条件可能同时稀缺且失效

- 现象：预声明的 RSI14 极端、Bollinger 2σ 越界、拒绝影线与 ADX≤20 组合，在 90 天 8 币
  开发集只产生 6 个互不重叠的 4h 候选，且 0 次 TP、5 次先止损，费用后 -1.23R。
- 根因：多个看似合理的确认条件相乘后覆盖率极低；极端价格越界即使发生在低 ADX 环境，也可能
  是新趋势/波动扩张的开端，拒绝影线本身不能证明随后 4h 均值回归。
- 修复：工具固定下一分钟入场、1m 首触、同分钟止损优先和全成本；开发门失败即封存 BNB/LTC
  留出集并明确 `stop_no_promotion`，没有放宽 RSI/ADX/影线阈值凑样本。
- 预防：复杂条件组合必须先同时检查覆盖率和样本外 EV；稀缺不是精准，少量 0 胜率信号不能因
  “条件严格”获得 shadow 或下单权限，留出集也不能在开发失败后被当成第二次调参机会。

### 2026-08-24 风险排序能少亏不等于保留候选可交易

- 现象：冻结首触预测用 `p_hit_sl≥0.70` 拒绝约 21% 候选后，A/B 每原候选分别少亏
  0.183R/0.166R，5/5 时间折的 policy 增量都为正；但保留候选仍分别亏 -0.63R/-0.61R，
  概率 Brier skill 也均为负。
- 根因：高 SL 概率对“最差的一批”有弱排序能力，但剩余方向信号没有正 alpha；只和更差的全量
  基线比较会把损失收窄误报为胜率/期望已经合格。
- 修复：风险先验门同时要求 reject 阻亏精度、概率校准、policy 增量、保留候选绝对费用后 EV、
  事件聚类下界和跨折一致性；因绝对 EV 与校准失败而明确 `stop_no_promotion`。
- 预防：任何 veto/filter 报告都必须同时给“每原候选增量”和“实际被保留候选的绝对 EV”；只有
  增量为正不能接入 Harness，更不能用空仓相对少亏替代可下单证明。

### 2026-08-24 共同标签表不代表共同预测证据已接线

- 现象：B_breakout 已有 29 条自然 4h 路径结果且 TP/SL=17/12，但 29/29 首次候选快照的
  `forecast` 都为空，因此不能做 B 自身的概率校准或 Harness 风险先验评估。
- 根因：历史重放对 A/B 都调用 `forecast_for_trade`，活体 `_scan_strategy_b_shadow` 却只补因子、
  市场状态和路由；候选进入共同表造成“证据已经一致”的错觉，缺少字段级覆盖验收。
- 修复：B 活体留样复用同一 causal forecast，显式传候选 event_ts 和版本化稳定 seed；专项同时
  断言 forecast 已冻结、B 仍 rejected、Harness 仍 shadow、fake orders 与 journal 均为 0。
- 预防：多策略共享标签链时，验收必须逐策略检查 features 中每个模型输入的覆盖率，不能只检查
  表行数和 outcome 数；实时与 replay 的派生证据必须共享算法、seed 身份和 as-of 边界。

### 2026-08-24 只修语义错误会把可恢复的截断 JSON 当永久失败

- 现象：Harness v4 有 8 条 schema_error，全部失败运行累计输出至少 200 tokens；普通 ValueError
  在首轮发生时不会进入已有 repair，先语义失败再结构失败时也只留下笼统 schema_error。
- 根因：图把“字段语义违规”视为可恢复，却把截断/畸形 JSON 和 Pydantic 契约错误视为不可恢复；
  provider 输出上限和二次修复都可能产生后者，两类错误的人为分流浪费了同一有界重试预算。
- 修复：结构错误与语义错误共用一次严格 repair，错误详情进入 step；重试耗尽继续 fail-closed，
  同时升级 tool-policy 身份，避免修复前后有效率和 EV 证据混计。
- 预防：结构化模型链必须分别测试“首轮结构错后恢复”“结构错重试仍错”和“语义错重试仍错”；
  提高完成率只能减少无效样本，不能自动证明判断更准，也不能因此降低生命周期晋升门。

### 2026-08-24 先拉最小根数再剔除未收线 K 会永久少一根

- 现象：主动提案开关和 provider 都正常，扫描也持续运行，但 run_count 始终为 0，日志没有模型
  失败或空提案记录。
- 根因：请求 60 根后再剔除当前未收线 K，只剩 59 根，恰好低于快照最小门；异常在逐标的
  `ValueError` 分支被作为数据不足跳过，最终 snapshots 为空，模型从未被调用。
- 修复：预取最小门之外的 2 根余量，再按原 as-of 规则过滤；测试断言请求根数、run 落库和零订单。
- 预防：所有“先拉取、再过滤已收线”的窗口必须预留至少一根余量，并用刚好边界样本测试；健康
  验收要看运行记录数，不能只看 provider ready 或开关值。

### 2026-08-24 训练段高胜率可能只是时段依赖

- 现象：A_pullback 空头在前 60 天用 ADX≥0.24、Bollinger 宽度分位≤0.21 过滤后，155 条的
  TP-first 达 54.84%、费用后均值 +0.1037R；冻结规则后，后 30 天却降到 30.36%、-0.9020R。
- 根因：训练段正均值的聚类下界本就为负且只有 2/4 时间折为正；低宽度强趋势组合依赖当时的
  波动与方向结构，后段候选成本升到平均 0.9175R，方向精度和成本稳定性同时崩溃。
- 修复：按预声明先验收后 30 天，失败后不打开 BNB/LTC 留出；工具把分段、事件聚类下界、
  时间折、单币贡献和成本一起输出，并固定返回 `stop_no_promotion`。
- 预防：任何“提高胜率”过滤器都必须把训练均值视为提案而非证据；先要求训练跨折和聚类下界，
  再用完全未看的时间段验证，并把成本分布漂移纳入同一否决门。

### 2026-08-24 Agent 会空仓不代表其非空提案更精准

- 现象：主动提案 v1 在 118 个相隔 12h 的五币历史批次中只输出 49 条有效提案，看似克制；但
  49 条全部做多，TP-first 仅 22.45%，费用后 -0.949R，5/5 时间折全负。
- 根因：输入只有 EMA、短动量、ATR 与量比，Prompt 没有可执行的方向对称、趋势一致或成本门；
  模型把一段偏多叙事机械映射成 long。空列表降低覆盖率，却没有校准非空决策的条件精度。
- 修复：用固定五币、固定频率、因果收线快照和完整 1m 首触路径单独评估非空提案，并把 schema
  成功率、方向占比、Wilson 精度下界、模型成本、批次聚类和时间折全部纳入停止门；后段保持封存。
- 预防：主动 Agent 必须分别报告 abstain coverage 与 conditional precision；任何单向占比超过 90%
  或精度下界低于逐候选保本率的版本都不得因“交易少”获得晋升，Prompt 自报 confidence 也不能替代。

### 2026-08-24 高波动标的降低成本但不会自动产生方向优势

- 现象：固定五个现役高波动 alt 后，A/B 后 30 天平均成本从主流币约 0.65R 降到 0.45R/0.41R，
  但 TP-first 仍只有 31.19%/32.48%，费用后分别 -0.44R/-0.39R，5/5 时间折全负。
- 根因：更大的 ATR 只改善“费用占 1R 的比例”；A/B 毛 EV 仅 +0.013R/+0.018R，几乎没有方向
  优势，远不足以覆盖撮合成本。把“成本更低”当作“信号已更准”仍会得到稳定负期望。
- 修复：按自然 watchlist 来源预先固定 AAVE/CRV/INJ/NEAR/ZRO，补齐独立 90 天分钟路径并复用
  未调参 A/B；后 30 天逐策略、逐币、事件聚类和时间折验收，失败后不继续调用 Agent 寻找正结果。
- 预防：资产池迁移必须同时报告毛 EV、成本 R 和净 EV；只有成本下降不能晋升，基础候选未通过时
  Agent 二判/提案也不得成为绕过方向验证的后门。

### 2026-08-24 Prompt 写了方向一致，不代表 Agent 会稳定执行

- 现象：主动提案 v1 的因果历史回放中 49 条非空提案全部为 long，即使输入同时包含 15m EMA 带、
  1h 和 4h 动量；模型会空仓，但非空方向仍出现严重单边偏置。
- 根因：Prompt 只要求可证伪理由，没有确定性核对提案方向和输入趋势是否一致；自然语言约束无法
  代替执行层不变量，自报 confidence 也不能证明方向证据成立。
- 修复：v2 在模型输出后、几何与留样前用代码要求 15m EMA 差、1h 动量、4h 动量三者与方向严格
  同号；缺失或冲突直接审计拒绝。同时加入只能自然获得的盘口/订单流上下文，并升级 Prompt 身份，
  但继续保持 shadow 和零执行权。
- 预防：凡 Prompt 中可确定计算的资格条件都应在模型外重复验证；版本变更必须隔离新旧样本，新增
  上下文只代表更好的实验输入，不能在自然费用后 EV 下界通过前宣称提高了胜率。

### 2026-08-24 现役 Prompt 升级会反向改变冻结的历史回放

- 现象：v2 方向门实现后，冻结 v1 回放的两条回归失败；平价历史夹具因趋势证据不一致被新门拒绝，
  已声明为 v1 的研究工具还会在元数据中读取现役 v2 版本。
- 根因：回放复用了现役 `run_proposal_cycle` 和全局配置，却没有把 Prompt 版本、payload 形状、System
  Prompt、方向验证行为与信号 identity 作为完整研究协议一起冻结。
- 修复：回放入口用有界作用域切到 v1，现役周期按版本选择 v1/v2 payload、System Prompt 和方向门；
  无论成功或异常都恢复 v2。回归同时覆盖幂等重放、路径结算和 100 条合成晋升门。
- 预防：任何历史研究的“冻结版本”必须包含输入序列化、Prompt、确定性后验门和样本身份，不只是一段
  版本字符串；升级现役协议后，先运行旧研究可复算测试，再允许部署。

### 2026-08-24 配置热读会产生“新版本标签、旧版本代码”的竞态样本

- 现象：v2 文件提交后、paper 重启前，旧进程读取到新 Prompt 版本并自然落了一个 v2 run；该 run 的
  input hash 却与同 K 的 v1 完全相同，证明进程仍执行旧序列化代码，没有微观结构上下文。
- 根因：Prompt 版本来自可热读配置，而 Python 函数实现留在旧进程内存；cycle key 只绑定 Prompt、
  schema、model 和 K 线，没有绑定确定性实现版本，故标签先于实现切换。
- 修复：先只重启 paper 终止旧内存，再把独立 implementation version 加入 v2 cycle key、payload 和
  C 专属 signal identity；旧竞态样本不会与最终 v2 混计。v1 重放显式省略新字段，A/B 身份也不变化。
- 预防：Prompt/配置与代码共同升级时，实验身份必须同时包含实现版本；验收要对比同 K input hash 和
  payload 字段，不能只看 run.prompt_version。发现竞态样本后保留审计记录并用身份隔离，不手工改库。
- 再现与加固：v4 部署前旧 paper PID `37072` 再次热读新配置，生成
  `proposal-run-961ef81431f73b53cfe6170c`；标签为 v4，但 payload 缺少 v4 必备的
  `aligned_direction/eligible_candidates`。保留该行作为负面审计证据，并把最终实现身份升为 v4.1；
  重启后只统计精确 v4.1，首个自然样本还必须逐字段验证 payload，禁止仅凭版本标签验收。

### 2026-08-24 只有 input hash 无法解释 Agent 为什么空仓

- 现象：第一个正确实现的 v2 自然批次 completed 且返回 0 提案，但运行表只有 input hash；事后无法
  区分三周期没有同向标的、微观结构冲突、字段缺失或流动性不足。
- 根因：hash 只能证明“某段输入不同”，不能重建字段、as-of、缺失率或模型可引用的微观证据；空列表
  也没有结构化原因。把这类 run 直接累计到 100 条，仍无法诊断或提高条件精度。
- 修复：v3 原子保存 canonical input snapshot 与微观覆盖率，给微观时点稳定 evidence ID；非空提案
  强制引用该证据，空仓强制标准原因。接口只统计完整 implementation identity 的可审计样本。
- 预防：任何模型实验的 input hash 必须与可重建输入快照成对；abstain coverage 和原因分布应与非空
  conditional precision 分开报告，不能用“会空仓”替代提案本身准确。

### 2026-08-24 沙箱内 loopback 失败不能证明本机服务离线

- 现象：沙箱内 `curl 127.0.0.1:8091` 返回 connection refused，但 `launchctl print` 与 `lsof` 均显示
  paper PID 仍在监听；旧进程因此在真正暂停前又运行了两个周期。
- 根因：受限命令环境的本机网络可见性与宿主 launchd 进程不一致，把单一 curl 失败误判成进程离线。
- 修复：用已批准的宿主侧 loopback 请求成功执行 `/pause`，并以 implementation v3.1 隔离暂停前产生的
  两条无完整 audit 竞态 run；不删改历史数据库记录。
- 预防：运行态变更必须以 `launchctl print + lsof + 宿主侧 health` 三证交叉确认；配置/代码共同升级前，
  若普通 loopback 失败，应立即切换经批准的本机请求，不能继续假设服务已经停止。

### 2026-08-24 合约盘口张数不能直接当基础币计算滑点

- 现象：首个 v3.1 自然输入把 DOGE `expected_slippage_bps` 记为 7910.24 bp，但同一快照价差只有
  0.63 bp；该异常会误导 Agent 把正常流动性判断成几乎无法成交。
- 根因：OKX 与 CCXT 的 SWAP order book 数量是合约张数，上层却直接用 `price × amount` 当 USDT
  可见名义额。DOGE 当前合约面值是 1000 DOGE/张，因而可见深度被低估约 1000 倍。
- 修复：原生与 CCXT 适配器在 `fetch_order_book` 边界统一乘 contract value，接口只向上返回基础币
  数量；策略层原有 `price × qty` 随即恢复 USDT 语义。实现身份升为 v3.2，旧 v3.1 样本不混计。
- 预防：所有行情“数量”接口必须明确单位并覆盖 `ctVal != 1` 的回归；合约单位换算只允许存在于
  exchange adapter，任何上层深度/成交额/滑点计算都不得消费原始张数。

### 2026-08-24 合法 abstain 也不能机械输出固定风险概率

- 现象：最新 Harness v4/v2 自然 run 面对 BTC、ADA 等明显不同的 `p_hit_sl/p_timeout`，仍连续输出
  `risk_probability=0.55`；成熟评估的 probability std 会趋近 0，无法校准也永远过不了晋升门。
- 根因：Prompt 只要求“不要机械固定”，结构校验却没有冻结的数值锚；模型用中间区间 0.55 合法
  abstain，语义上安全但没有概率信息量。
- 修复：用已冻结首触分布生成 `p_loss_prior=SL+0.5×timeout`；abstain 必须在先验 ±0.02 内，越界触发
  一次结构化修复。49 条成熟路径上该先验 Brier 0.2223，优于固定 0.55 的 0.2372，std 0.1023。
- 预防：概率输出不仅要做范围与 schema 校验，还要用已成熟标签检查 Brier、方差和基准技能；模型的
  “不确定”不能退化成跨候选常数，任何新先验必须先固定公式再做样本外持续评价。

### 2026-08-24 长扫描跨过 15m 收线会让同轮标的消费不同 K

- 现象：模拟盘一轮扫描在 04:44:55 开始，排在前面的标的读取上一根已收线 K；ETH/BTC 分别到
  04:45:31/46 才检查并读取新收线 K，同一轮候选的 `source_latency_ms` 达 31/46 秒。扫描顺序因此
  决定能否看到最新信号，既会漏候选，也会让后排标的入场更晚。
- 根因：5 分钟轮询按进程启动秒滚动，可能紧贴收线前启动；`scan_signal`、策略 B 和 Agent 提案又在
  每个标的内部重复读取 `time.time()` 计算闭合截止点，长串行扫描跨周期后自然产生不同可见集合。
- 修复：轮询槽改为 UTC 5 分钟边界并复用既有 2 秒收线缓冲；每轮在进入标的循环前冻结一个
  `as_of_ts`，A 的 15m/1H/4H、B 的 15m/4H 和 Agent 提案 K 线全部消费同一截止点。盘口与实时入场价
  仍按各标的实际检查时刻读取，不把历史快照冒充实时微观结构；无 15m 回踩结构时在主周期预检即
  返回，不再为全池无信号标的串行拉取 1H/4H、ticker 与 forecast。A、B 和影线 challenger 复用同轮
  15m 快照；长启动刷新结束后把扫描计时归入结束时所在槽，避免跨边界后立刻重复扫描。
- 预防：任何横截面或顺序扫描必须显式携带轮次 as-of；回归同时覆盖“缓冲前不可见、缓冲后可见”与
  “边界过缓冲才打开新扫描槽”，并断言无主结构只请求 15m；不得用循环体内墙钟决定历史 K 线集合。

### 2026-08-24 无执行权的影子模型不能阻塞可执行策略扫描

- 现象：05:15 自然轮次出现 7 条 B_breakout 影子候选，每条在进入下一个标的前同步调用 Harness；
  单次模型 1.378–1.952 秒，合计 11.905 秒。B 永远无下单权限，却把后排策略 A 候选的检查时点推迟。
- 根因：为保证 B 在 A 的 `continue` 前留样，旧实现把“冻结候选”和“调用影子模型”绑在同一个同步
  方法里；正确的无选择偏差要求只是先冻结样本与上下文，不要求非执行推理占住时间关键路径。
- 修复：发现 B 时立即冻结 signal、账户、新闻、健康与数据库候选身份，生成零参数 runner；A 全池
  扫描结束后再顺序执行这些 runner。B 的 prompt/Trace/4h 标签和零执行权均不变，A 的潜在下单时点
  不再等待 B 的模型网络响应。
- 预防：任何 shadow-only 外部 IO 默认移出执行关键路径；若延后执行，必须先冻结完整 as-of 上下文并
  用回归断言模型调用发生在 A 全池之后，禁止延后时重新读取账户或市场状态造成时间泄漏。

### 2026-08-24 Harness 仍在 shadow 不等于指标快照可以停止更新

- 现象：当前 v5 A 的只读实时评价已经有 34 条成熟结果，但 readiness 与 `agent_versions.metrics_json`
  仍显示第一次登记时的 5 条；版本一直是 shadow，看似只是展示延迟，实际到 100/30 后也可能继续
  被陈旧指标卡住。
- 根因：生命周期同步只在 candidate→shadow 和 shadow→validated 状态迁移时写 metrics；处于
  shadow 且样本尚未达门时，每小时虽重新计算评价，却没有“只刷新证据、不改变状态”的持久化原语。
- 修复：storage 增加 `refresh_metrics`，在不改变状态/激活时间的前提下原子更新指标快照；每次同步对
  shadow 刷新当前指标，validated 在人工激活前也刷新并重新核对晋升门，新增证据退化则回滚。
- 预防：状态机回归必须覆盖“状态不变、证据增长”的自循环场景，同时断言 metrics 更新且 veto 权限
  仍为 false；readiness 数量要与同版本只读实时评价交叉核对，不能只检查状态迁移测试。

### 2026-08-24 多策略 Harness 有 Trace 不等于每条策略都有生命周期

- 现象：B_breakout 已有 32 条当前 v5 成熟评价，可按策略独立算出 Brier/EV，但 `agent_versions` 只有
  A_pullback 行；B 的证据永远不会进入 shadow→validated 审计链。
- 根因：候选与评价接线已经按 strategy_id 隔离，worker 的每小时同步却仍只调用一次默认 A；采样链
  扩成多策略后，生命周期调度没有同步扩展。
- 修复：新增多策略同步入口，显式同步 A 与已启用的 B；单策略同步增加 `allow_activation`，A 继承
  用户 Veto 开关，B 固定 false，因此 B 可以积累/验证证据但绝不自动进入 active-veto。
- 预防：新增策略 Harness 时四条链必须同时验收：run、outcome、evaluation、agent_versions；批量同步
  回归必须断言完整策略集合和逐策略授权位，不能用 A 的生命周期行代表 B 已接通。

### 2026-08-24 只指出错误 evidence_id 不能保证修复轮引用合法锚

- 现象：首条自然 v8 run 的首响应把整条 `field_provenance` 锚自造为三个字段级路径；唯一修复轮收到
  错误列表后仍继续自造字段路径，最终以 schema error 失败关闭，未产出可评价决策。
- 根因：确定性校验要求 evidence_id 与冻结上下文声明的锚完全一致，但修复载荷只给 violations 和旧响应，
  没有把同一次冻结状态中允许使用的精确 ID 列为候选；模型只能再次猜测命名规则。
- 修复：语义修复载荷增加排序后的 `allowed_evidence_ids`，直接复用校验器同一白名单，并要求逐字复制；
  运行身份升级为 Tool Policy v5，Prompt 与所有决策/风险门槛保持不变。
- 预防：任何“只能引用已声明标识符”的结构化修复协议，必须同时提供机器可读白名单并由测试断言它与
  最终校验来源一致；不能期待模型从错误消息反推出内部命名约定。

### 2026-08-24 研究 CLI 混入旧 lib 会把统计依赖失败伪装成因子不通过

- 现象：90 天真实 SWAP 重放已有 A/B 各方向 2,500～2,900 条成熟标签，所有 61 个因子仍无一验证；
  `factor_trials.dsr/pbo` 全部为 NULL，模型因此始终显示“样本充足但 features=[]”。
- 根因：三个现役研究 CLI 在仓库根之后又把旧 Python 3.9 `lib/` 插到 `sys.path[0]`；Python 3.12
  导入其中的旧 numpy 失败，`overfit_guard` 按可选依赖语义把 numpy 记为不可用，DSR/PBO 返回 None。
  失败没有中止裁决，而是让每个因子确定性落为 reject，表面看像统计结论。
- 修复：研究 CLI 只把仓库根加入路径，依赖统一由当前 Python 3.12 环境解析；新增子进程回归逐个导入
  入口并断言 numpy 不来自仓库 `lib/`。历史 trial 保留审计，新运行用完整 DSR/PBO 重新裁决。
- 预防：现役入口禁止把 legacy `lib/` 加入模块搜索路径；任何关键统计依赖不可用时，评价报告必须显式
  暴露 NULL/不可用，不能把“没有统计引擎”和“统计未过门”混成同一个业务状态。

### 2026-08-24 只允许极端冲突的风险 Agent 无法提高常规入场精准率

- 现象：当前完整 v5 自然成熟样本 A 34 条、B 33 条，67/67 全部 abstain；其中 B 有 14 条
  `risk_probability≥0.70`，事后路径为 12 亏 2 盈，模型仍因“没有极端事件或明确冲突”拒绝给 reject。
  部分合法运行还用“缺乏已验证的入场模型正期望证据”等近义治理措辞绕过了精确字符串校验。
- 根因：Prompt 把 reject 等同于重大新闻、闪崩或流动性失败，常规的趋势逆向、拥挤、波动和执行质量
  组合无法形成决策；同时要求 abstain 贴近未校准先验，使 Agent 只会复制 forecast。治理元数据校验
  又依赖少量固定短语，不能覆盖同义表达。
- 修复：v6 改为 outcome-first 费用后亏损概率任务；未校准先验只作一个特征，两个独立普通风险证据族
  也可形成 shadow reject，缺失只降低信心。确定性语义层新增 verdict 与风险/信心门一致性，并扩充
  治理近义标记；旧 v5 身份仍保留原先验容差以便重放。所有权限和 100/30 门不变。
- 预防：Prompt 验收必须同时检查 verdict 分布、概率分辨率、Brier、证据来源和费用后决策价值；模型
  连续只输出单一 verdict 时先审查任务定义是否把常规案例排除在可判范围，不能靠降低阈值强行造 reject。

### 2026-08-24 要求 JSON 的 Prompt 不等于 provider 已启用 JSON Output

- 现象：首条 v6 自然 run 的第一次模型响应不是 JSON，语义图只能再调用一次修复；累计模型延迟
  3,933ms、input/output token 5,529/398，几乎耗尽 A 策略同步把关的时点预算。
- 根因：系统 Prompt 虽给出 JSON 字段，但 Chat Completions 请求没有设置 provider 原生
  `response_format={"type":"json_object"}`；格式正确性完全依赖模型遵循文字指令。
- 修复：仅 Harness provider 回调显式启用 JSON Output，legacy 判断保持原 text 默认；Prompt 同时保留
  json 字样和完整对象样例。运行身份升级为 `tool-policy-v3-provider-json-output`，新旧格式可靠性和
  100/30 证据不混计；空 content、字段缺失和语义错误仍走既有一次修复后失败关闭。
- 预防：结构化输出必须同时验证请求参数、响应 JSON 和领域语义三层；provider 模式变化属于运行身份，
  不能只改 HTTP body 却沿用旧版本成熟度。自然验收还要比较修复次数、模型延迟和错误率，不能只看最终成功。

### 2026-08-24 最近成熟版本不等于当前进程配置或最近运行身份

- 现象：paper 已自然写入 v6 + `tool-policy-v3-provider-json-output` 的 4 条 run，`/agent/status` 仍把
  v5 + tool-policy-v2 显示为 `current_version`；新 run 尚未满 4h，没有生命周期行，用户容易误判部署失败。
- 根因：状态查询只读 `agent_versions` 最新行，该表表达“已有成熟评价的生命周期身份”，不表达进程当前
  config 或最近一次 run；三个不同时间语义被压成 current。`veto_enabled` 还只识别 active-veto，漏了
  仍具否决权的 observing/kept。
- 修复：保持旧 `current_*` 字段兼容，同时新增 configured prompt/tool policy、latest run prompt/tool policy/
  runtime/evaluation 状态与显式 lifecycle version/status；Veto 展示与执行端一致识别 active-veto、observing、
  kept。全部字段只读，不新增控制或权限。
- 预防：Agent 状态接口必须分别展示“进程配置、最近运行、成熟生命周期”三层身份；pending 新版本不能
  被旧成熟行覆盖，成熟旧版本也不能被最新 pending run 冒充已验证。契约测试同时覆盖两者并核对 Veto 集合。

### 2026-08-24 只在修复轮给证据锚会浪费首次判断和总时限

- 现象：自然 v8/v5 首批 5 条只有 1 条合法完成；模型首次响应反复自造字段级 evidence ID，进入修复后
  又要自行推导动量、账户、流动性和资金费语义，最终三条 schema error、一条 timeout。
- 根因：合法 evidence 白名单只放在语义修复载荷，第一次请求只能从深层 `field_provenance` 猜锚；现有
  机器资格门也只在响应后报单个错误，没有把同一冻结事实的确定性资格提前交给模型。
- 修复：Tool Policy v6 在首次请求加入校验器同源的合法锚和四项冻结资格；旧身份保持原输入形状，
  确定性校验、一次修复与失败关闭不变。
- 预防：机器已经知道的输出契约和合法枚举应在第一次结构化请求中显式给出；不能把可避免的格式探索
  留给失败后的重试，尤其外部调用共享严格端到端时限时。

### 2026-08-24 模型把已同向候选误报成 no_aligned_candidate

- 现象：Agent 主动提案 v3.2 的 52 个自然批次中有 45 个包含确定性三周期同向候选，模型仍几乎全部
  返回 `no_aligned_candidate`，当前协议 0 条提案、无法形成自然 4h 反事实。
- 根因：冻结输入只给 EMA 与动量小数，要求模型自行重复确定性同号计算；空提案原因虽有枚举，却没有
  与输入资格做机器一致性校验。
- 修复：v4 在快照中显式冻结 `aligned_direction`，提案方向必须匹配；已有同向候选时禁止
  `no_aligned_candidate`，全部不齐时又必须使用该原因。错误输出失败关闭，不进入样本或订单。
- 预防：凡是代码可精确计算的资格，不让 LLM 从原始小数重复推导；结构化 abstain 原因也必须与冻结
  输入做双向语义校验，不能只检查枚举合法。

### 2026-08-24 Prompt 要求 JSON 不等于 provider 启用了 JSON 模式

- 现象：首个字段完整的 v4.1 自然批次已有两个确定性同向候选，模型响应仍被严格解析器判为
  `schema_error`，无法形成可结算提案。
- 根因：主 Harness 通过 provider 请求的 `response_format=json_object` 固化输出模式，C 提案调用却走
  默认文本模式；System Prompt 的“只输出 JSON”只是软约束。
- 修复：C production callback 复用同一 provider transport 并显式传 `json_mode=true`；解析器、资格门和
  失败关闭不变，implementation 另升 v4.2，旧错误样本原样保留。
- 预防：任何要求机器严格解析的生产模型调用，测试必须检查实际 provider payload 的结构化输出模式，
  不能只断言 Prompt 文案包含 JSON；自然验收仍要核对运行状态和审计字段。

### 2026-08-24 接口按协议隔离不代表训练链也已隔离

- 现象：C v4.2 接口当前协议计数为 1，模型训练查询却会读到同一 `strategy_id` 下 13 条旧 v1/v2
  成熟结果；表面样本数和胜率会先于当前协议真实结果增长。
- 根因：proposal endpoint 用 audit implementation identity 过滤，而因子、概率、极值、校准和生命周期
  沿用早期 A/B 的 strategy-only scope；协议版本不是这些研究制品的一等过滤列。
- 修复：把采样时的精确 `strategy_version` 同步写入 C 因子试验与模型制品，并让 C 全研究/校准链只消费
  当前 `config_identity`；旧数据不删除、不改写，只退出当前协议统计。
- 预防：任何新实验版本都要从 run 统计一路追到训练行、制品、在线加载、校准和生命周期逐层核对 scope；
  不能用某个只读接口的正确计数推断所有下游消费者都正确。

### 2026-08-24 只记异常类型无法修复严格协议失败

- 现象：JSON mode 后自然运行仍出现 `schema_error`，审计只有 `ValueError` 与不可逆响应哈希，无法判断
  是 JSON schema 还是确定性资格语义失败。
- 根因：异常捕获丢弃了本地严格解析器已经生成的非敏感、枚举化失败文本；为了不保存原始模型内容，
  结果连可操作的本地诊断也一并丢失。
- 修复：审计 output 单独持久化严格解析/语义校验产生的截断 `error_detail`，仍不保存原始响应。
- 预防：隐私最小化应区分“外部原文”与“本地确定性诊断”；前者默认不落库，后者必须足够定位失败门。

### 2026-08-24 定时研究会在统计门已满足后继续空等

- 现象：自然样本可能在 24h 研究周期中段跨过 300/60/60，但模型列表要等下一次固定周期才更新。
- 根因：调度只看墙钟，不看已经结算的研究证据是否跨越训练门；幂等训练能力没有转化为事件驱动调度。
- 修复：为成熟样本计算只读里程碑，首次跨过因子门、分方向模型门以及门后每批新证据时提前运行研究。
- 预防：长周期后台任务凡有明确数据门，都同时保留“定时兜底 + 门槛跨越触发”，且失败退避独立处理。

### 2026-08-24 结构化输出模式仍可能被过小 token 上限截断

- 现象：C v4.2 已启用 JSON object mode，三次自然运行仍有 schema error；最新诊断明确为非完整 JSON，
  同批有 3 个合格候选且耗时已到 3229ms。
- 根因：共享 provider 把所有任务固定为 200 输出 tokens；最多两条提案会重复四个长 evidence ID，
  最小合法响应长度与上限不匹配。JSON mode 约束格式，但不能让被截断的 JSON 自动闭合。
- 修复：提案只返回资格验证真正需要的 15m 与 microstructure 两个锚，并使用独立、可测试的输出预算和
  零温度；主 Harness 的预算不随之扩大。
- 预防：结构化契约新增数组上限时，必须计算最坏/典型合法响应尺寸，并把 max tokens、延迟预算与自然
  finish 状态一起验收，不能只看 `response_format`。

### 2026-08-24 config 热重载身份可能先于 Python 函数体生效

- 现象：v5 代码提交后、paper 完整重启前，旧进程先读到新 implementation 配置并写出一条 v5.0 run，
  但实际 callback 仍是内存中的旧 200-token 函数体。
- 根因：`config.maybe_reload()` 会替换配置属性，已 import 模块的函数和 Prompt 常量不会随之重载；用
  config implementation 作为审计身份时，两者短时间不再原子一致。
- 修复：混合 run 原样保留，implementation 再升 v5.1，并以完整进程重启作为正式身份边界。
- 预防：任何同时修改 config 身份与 Python 行为的协议升级，提交后先停止扫描并完整重启，再接受首条
  新身份样本；不得用热重载结果作为部署完成证据。
### 2026-08-24 signal_inconsistency 不能由 regime 或波动代替方向证据

- 现象：Harness v9 首批自然 HOOD short 的 1H/4H 动量与 EMA 趋势带均顺向，却把 disorder、波动不稳和
  route=abstain 写成 `signal_inconsistency`，与新闻冲突拼成普通 reject。
- 根因：旧校验器只在理由明确提到 momentum 时检查动量方向；模型可以换一种措辞，继续使用同一个风险码。
- 修复：v10 用同一确定性函数解释 1H/4H 动量、`trend_band_atr` 与 `directional_index_spread` 的符号；
  四项都顺向时，无论理由如何措辞都不能使用 `signal_inconsistency`。
- 预防：凡 reason code 可从冻结输入确定资格，都应在首次请求显式给出 qualifier，并由 validator 无条件
  复算；不得依赖理由文本关键词，也不得用治理元数据或 regime 代替字段语义。

### 2026-08-24 聚合资格为真不代表理由引用的每个因子都正确

- 现象：v10 自然 GRASS short 确因正 `trend_band_atr` 取得信号不一致资格，但模型把负的 1H/4H 动量
  写成“正动量冲突”；风险码整体合法，具体解释仍是错的。
- 根因：首次契约只给聚合布尔值，校验器只确认至少一个因子逆向，没有把模型实际引用的因子族与冻结
  符号逐项核对。
- 修复：v11 契约增加排序稳定的具体冲突因子清单；理由引用 momentum、趋势带或 DMI 时，validator
  必须确认相应族确实出现在清单中，否则进入一次受限语义修复，失败则关闭。
- 预防：聚合资格只能回答“是否存在”，不能证明自然语言中的每个子主张；影响拒单的解释必须保留可
  逐项复算的最小 witness，而不是只传总布尔值。

### 2026-08-24 给出单项资格仍不能假设模型会正确组合 reject 地板

- 现象：v11 首批 7 条自然 run 中，BTC/LINK/LTC 各有两种合格风险族并 3/3 首次完成；INJ/ETH/BNB/
  ENA 只有一种合格普通风险族，却在修复后仍尝试 reject，4/4 以 schema error 失败关闭。
- 根因：首次契约逐项给出 qualifier，但仍要求模型自行完成“过滤 false → 去重 → 普通族计数 → 严重事件
  例外”的组合逻辑；修复提示指出某一错误，不等于明确告诉模型当前输入最多只能 abstain。
- 修复：v12 由 validator 同源函数直接给出排序后的 `qualified_ordinary_risk_families` 和最终
  `reject_evidence_floor_satisfied`；false 时 reject 非法，缺失字段不得作为第二风险族。
- 预防：确定性规则的最终布尔裁决若可由同一冻结输入复算，应直接暴露给模型并再次校验；不要把多个
  正确原子事实的组合可靠性留给自然语言推理。

### 2026-08-24 动作强制必须同时服从概率门和证据可行性

- 现象：v12 已规定单一普通风险族时 reject 非法，但旧 validator 仍把任意“风险概率≥0.70 且
  信心≥0.70”的 abstain 判错；高风险单族样本只能违规 reject、虚假压低信心或 schema fail-closed。
- 根因：概率/信心动作门早于证据地板引入，升级时只约束了 reject 的证据合法性，没有同步收紧
  “必须 reject”的前置条件。
- 修复：v13 复用同一确定性地板函数；只有证据地板满足时，高风险高信心 abstain 才必须修复为 reject。
  地板 false 时允许保留原概率和信心并 abstain；旧版本维持冻结回放。
- 预防：任何动作强制都必须检查该动作在全部上游契约下是否可行；禁止校验器通过迫使模型篡改概率、
  信心或证据来解决规则冲突。

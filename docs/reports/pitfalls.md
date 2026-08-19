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

### 2026-08-20 user_version 已升到最新后,旧索引可能被活体旧进程建回来
- 现象：只读看活体 `crypto_agent.db`，`PRAGMA user_version=2` 且新索引已在，但旧 `idx_anom_status` 仍在。按"version 已最新就跳过迁移"的逻辑，下次重启也不会 DROP。
- 根因：① HTTP 层 `sdb.init_db()` 不带 db_path，TestClient 回归会把新迁移跑到活体库（version 被升到 2、新索引建上）；② 活体进程仍跑旧代码，旧 SCHEMA 里有 `CREATE INDEX IF NOT EXISTS idx_anom_status`，会把刚删掉的旧索引建回来；③ v2 迁移因 version 已是 2 不再执行。
- 修复：SCHEMA 每次 `executescript` 都 `DROP INDEX IF EXISTS idx_anom_status`（幂等），不依赖"迁移还没跑过"。v2 里仍保留同款 DROP。
- 预防：改名/替换索引时，DROP 旧名必须进 SCHEMA（每次 init_db 都跑），不能只放在"只跑一次"的迁移函数里；测试 init_db 必须传隔离 db_path，HTTP 只读端点的 init_db() 默认路径会碰到活体库。

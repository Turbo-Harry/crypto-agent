"""
策略配置 — 全部参数集中管理
核心理念：宁可做对，也不做错；空仓是默认，持仓是例外。
激进档：最大回撤 15%~20%，单笔风险 1.5%，+5%/-3% 盈亏比。
"""
import os

# ============ 双实例运行（2026-08-23 用户指示"模拟盘和实盘同时跑"） ============
# CRYPTO_AGENT_MODE 环境变量决定本进程实例身份:
#   live  (默认) → 实盘,真实 OKX 账户,数据库 crypto_agent_live.db
#   paper        → 模拟盘,OKX sandbox,数据库 crypto_agent.db(延续历史)
# 数据库路径也可用 CRYPTO_AGENT_DB 环境变量直接指定(launchd 双实例互不串库)。
CRYPTO_MODE = os.environ.get("CRYPTO_AGENT_MODE", "live")
INSTANCE_NAME = "paper" if CRYPTO_MODE == "paper" else "directional"

# ============ 数据源 ============
BASE_URL = "https://data-api.binance.vision"  # 币安官方公开数据端点（不受地区限制）
INTERVAL = "1d"                                # 日线级别

# ============ 观察池 ============
# 稳定币 / 锚定币，排除出观察池
STABLECOINS = {
    "USDC", "USDT", "TUSD", "FDUSD", "BUSD", "DAI", "USD1", "RLUSD", "U",
    "EURI", "XAUT", "EUR", "GBP", "AEUR", "USDY", "PYUSD", "USTC",
    "PAXG", "WBTC", "WBETH", "USDE", "USDP", "TRIBE", "SUSD", "USDX",
}
# 杠杆代币（后缀）
LEVERAGED_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")
# 观察池：按 24h 成交额排序取前 N 个（过滤后）
OBSERVE_POOL_SIZE = 80

# ============ 美股/公司代币（tokenized stocks） ============
# 2026-08-20 用户拍板: 只做合约、不做现货。OKX 生产实测 19 个美股/公司代币
# 永续合约(NVDA/TSLA/AAPL/MSTR/COIN/HOOD/META/GOOGL/AMZN/MSFT/INTC/SNDK/
# SOXL/LITE/CRCL/AMD/PLTR/ANTHROPIC/OPENAI),24h 成交额全部 ≥100 万 USDT。
# 本表只放【沙盘(demo)实测有合约的】——AAPL/MSTR/COIN/META/AMZN/INTC/SNDK/
# SOXL/LITE/AMD 生产有但沙盘暂缺(XIAOMI 同类缺口),沙盘补上后再扩表。
# 旧 X 前缀清单(XNVDA 等)是现货版,已随"只做合约"决策弃用。
STOCK_SWAP_TOKENS = ["NVDA", "TSLA", "HOOD", "GOOGL", "MSFT",
                     "CRCL", "PLTR", "ANTHROPIC", "OPENAI"]

# ============ 关卡 1：大盘环境 ============
BTC_EMA_FAST = 20
BTC_EMA_SLOW = 50
FEAR_GREED_MAX = 75          # 恐惧贪婪指数上限（超过=过热，不做）

# ============ 关卡 3：个币共振 ============
BOX_MIN_DAYS = 14            # 吸筹箱体最少天数
BOX_AMP_MIN = 0.15           # 箱体幅度下限 15%
BOX_AMP_MAX = 0.30           # 箱体幅度上限 30%
VOLUME_BREAKOUT_MULT = 1.5   # 突破放量倍数（相对20日均量）
RS_TOP_PERCENT = 0.20        # 相对强度进前 20%
EMA_FAST = 20
EMA_SLOW = 50

# ============ 入场模式（回踩确认） ============
PULLBACK_WINDOW = 5          # 突破后最多等 N 天回踩确认
PULLBACK_BREAK = 0.99        # 收盘跌破箱体上沿的 99% 判假突破
ENTRY_PREMIUM = 0.01         # 限价买单挂在箱体上沿上方 X%（回踩触及才成交）

# ============ 出场 ============
# （2026-08-17 清理: 旧版固定百分比出场参数 STOP_LOSS/TAKE_PROFIT_1/2/
# TIME_STOP_DAYS 已无引擎引用;ATR 出场参数统一在下方"日内短线"区
# STOP_ATR_MULT=1.0 / TP_ATR_MULT=2.0(2:1 盈亏比),此处曾残留重复定义
# STOP_ATR_MULT=1.5 造成误导——params_lint 已加重复赋值检测防复发。）

# ============ 风控（激进档） ============
MAX_DRAWDOWN_SOFT = 0.12     # 回撤 12% 减仓
MAX_DRAWDOWN_HARD = 0.20     # 回撤 20% 全停
DAILY_LOSS_LIMIT = 0.015     # 单日亏损 1.5% 停手
# （历史遗留的 1.5%/40%/4仓 参数已删除——无引擎引用,
#  统一由下方「参数统一维护区」RISK_PER_TRADE/MAX_NOTIONAL_PER_TRADE/MAX_TOTAL_NOTIONAL 管辖）

# ============ 手续费/滑点假设（回测用） ============
FEE_RATE = 0.001             # 单边 0.1%（现货挂单+吃单的混合估计）
SLIPPAGE = 0.0005            # 单边滑点 0.05%（默认中币）
MAX_ENTRY_GAP = 0.08         # 入场价超过箱体上沿的最大容忍（超过则放弃追高）

# 滑点分档（按 24h 成交额，小币流动性差滑点大）
SLIPPAGE_LARGE = 0.0002      # 大币滑点 0.02%
SLIPPAGE_MED = 0.0005        # 中币滑点 0.05%
SLIPPAGE_SMALL = 0.0015      # 小币滑点 0.15%
VOL_LARGE = 100_000_000      # 成交额 > 1亿 = 大币
VOL_MED = 10_000_000         # 成交额 > 1000万 = 中币

# ============ 资金费率套利成本核算（审计 CR-4） ============
# 毛年化 rate*3*365 是瞬时快照外推，高估；必须扣掉开平仓往返成本。
ARB_ROUNDTRIP_COST = 0.003   # 开+平 双向往返总成本占名义比例（现货taker 0.1% + 合约taker 0.05% + 滑点余量）
# （2026-08-16 用户决定：资金费率套利引擎整线移除并归档 legacy/——
#  ENABLE_FUNDING_ARB / ARB_MIN_* 等套利配置已随引擎删除。）

# ============ 日内短线交易频率约束（用户要求：抓最佳时机，不频繁交易） ============
# 每个币每天的允许笔数按其【当日扫描评分】动态调整：评分越高，越值得多给机会
# 2026-08-16 晚 用户指示"模拟盘,激进为主,降低参数以加大交易概率"（第二档）:
# 额度再翻倍 + 冷却 1h→30min + 门槛全线再降。
# 【风险红线不变】: 单笔 1% 风险 / 名义 ≤150 / 总敞口 ≤600 / 交易所侧止损。
TRADE_BUDGET_BY_SCORE = [(0.7, 16), (0.5, 12), (0.3, 8), (0.0, 4)]  # (评分阈值, 允许笔数)
DEFAULT_TRADE_BUDGET = 8      # 无评分（回退池）时的默认笔数
SIGNAL_COOLDOWN_MINUTES = 0   # 同币信号冷却关闭(2026-08-23 用户指示)
SCAN_INTERVAL_MINUTES = 5     # 信号扫描间隔（2026-08-21 用户指示 15→5 分钟）
MTF_ENABLED = False           # 多周期共振过滤关闭（采集加速;tf4h_spread 特征已记录,可事后检验;可回滚 True）

# ============ 策略参数统一维护（2026-08-16 用户指示:数值不再分散在各模块） ============
# 改交易门槛只改这里;各模块一律 import config 引用,禁止私藏副本。
# 注意三件套联动关系（不满足会导致"全部信号被拒"或"门槛失效"）:
#   THRESHOLD_INITIAL < DECIDE_MIN_SCORE <= SIGNAL_SCORE
SIGNAL_SCORE = 30            # 回踩确认信号基础分（2026-08-21 用户放宽: 80→50→40→30）
DECIDE_MIN_SCORE = 30        # 决策层最低信号分（与 SIGNAL_SCORE 联动）
THRESHOLD_INITIAL = 25       # 阈值学习层初始阈值（联动约束: < DECIDE_MIN_SCORE）
# 2026-08-23 用户指示"实盘阈值上调到40": 实盘实例决策阈值下限——
# 有效阈值 = max(学习器阈值, 40),真金更挑信号;模拟盘保持激进(25)。
# 阈值学习/策略同步照常,下限只在实盘决策门生效(热重载秒生效)。
LIVE_THRESHOLD_FLOOR = 40
# 2026-08-23 用户指示"维度太少了,加": 信号影子分 3 维 → 6 维。
# 新增: 量能确认(拒绝K放量更可信)/资金费顺风(拥挤方向反向)/盘口失衡(深度方向)。
# 权重和必须=1.0;新增维度数据缺失时取 0.5 中性(不污染总分)。
SHADOW_WEIGHTS = {"wick": 0.28,    # 拒绝K线强度(影线/实体)
                  "depth": 0.27,   # 回踩深度适中(贴EMA)
                  "trend": 0.20,   # 1h 趋势离散度
                  "volume": 0.10,  # 量能确认(近20根均量比,封顶2x)
                  "funding": 0.05, # 资金费顺风(多单负费率/空单正费率)
                  "book": 0.10}    # 盘口失衡(前10档,方向对齐)
SHADOW_VOL_LOOKBACK = 20           # 量能确认的均量窗口
SHADOW_BOOK_DEPTH = 10             # 盘口失衡统计档位数
# 权重进化(2026-08-23 用户问"会根据历史经验调整权重吗"):
# 每笔平仓后按 6 维子分与盈亏的相关性(IC)积累证据,达标才生成提案,
# 经人工批准才生效(approve 写 kv 覆盖;绝不自动改)。与扫描尺子同纪律。
WEIGHT_EVOLVE_ENABLED = True       # 证据收集+提案开关
WEIGHT_EVOLVE_MIN_SAMPLES = 30     # 单维度最少平仓样本才允许提案
WEIGHT_EVOLVE_MIN_IC = 0.10        # 单维度 |IC| 下限(相关太弱不动)
WEIGHT_EVOLVE_STEP = 0.02          # 提案步长: 强维 +step,弱维 -step
WEIGHT_EVOLVE_MAX_SHIFT = 0.10     # 单次提案单维最大变动(防一步跳飞)
WEIGHT_EVOLVE_KV_KEY = "shadow_weights"   # 批准后的活体权重 kv 键
SHADOW_DIMS = ("wick", "depth", "trend", "volume", "funding", "book")  # 6 维名

# ============ 费率与手续费（2026-08-23 用户问"会计算费率和手续费吗"） ============
# 平仓时优先按账户账单(fetch_bills)取【实际】手续费与资金费;
# 账单取不到时按 FEE_RATE_TAKER 估算(市价单双边 taker 0.05%)兜底。
FEE_ACCOUNTING_ENABLED = True  # 开关: 实盘盈亏扣费(硬止损累计也按净额)
FEE_RATE_TAKER = 0.0005        # OKX 基础 taker 费率 0.05%(VIP0,双边收)
REJECT_WICK_RATIO = 1.0      # 拒绝K线: 影线/实体 最小比（激进第二档 1.5→1.0,信号更多）
STOP_ATR_MULT = 1.0          # 止损距离 = N × ATR
TP_ATR_MULT = 2.0            # 止盈距离 = N × ATR（2:1 盈亏比）
RISK_PER_TRADE = 0.01        # 单笔风险 1%（红线,改动需用户明确拍板）
MAX_NOTIONAL_PER_TRADE = 150 # 单笔名义上限 USDT（红线）
MAX_TOTAL_NOTIONAL = 600     # 组合总敞口上限 USDT（红线,PositionLedger 共用）
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE",
           "LINK", "ADA", "AVAX", "BNB", "LTC"]   # 回退主流池（采集加速扩到 10 个）
SWAP_ONLY = True             # 只做合约(2026-08-20 用户拍板"我们不做现货,只做合约")：
# 开仓层硬闸门——无合约场所的标的一律拒绝;现货路径代码保留但不可达
# (改回 False 即恢复美股现货只多路径,不删代码保可逆)。
LEVERAGE_MAP = {"BTC": 3, "ETH": 3, "SOL": 3, "XRP": 3, "DOGE": 3,
                "LINK": 3, "ADA": 3, "AVAX": 3, "BNB": 3, "LTC": 3}
# 2026-08-20 用户指示: 合约倍数限制 3x~5x(低于 3x 拉回 3x,高于 5x 压到 5x)
LEVERAGE_MIN = 3
LEVERAGE_MAX = 5
# B+C 分档(用户拍板): 信号分≥门槛【且】该币战绩数据验证达标 → 5x,否则 3x
LEVERAGE_NORMAL = 3            # 基础杠杆
LEVERAGE_HIGH = 5              # B+C 双条件满足时的杠杆
LEVERAGE_HIGH_SCORE = 70       # B: 信号分门槛
LEVERAGE_HIGH_MIN_TRADES = 2   # C: 该币最少平仓样本数
LEVERAGE_HIGH_MIN_WINRATE = 0.5  # C: 该币近期胜率下限
# 2026-08-19 沙盘实测: tpTriggerPx 条件单开/挂/取消全链路 sCode=0,开启。
# 意义: 引擎死机时止盈照常成交(此前仅本地 monitor,崩溃窗口利润会跑)。
FLAG_ENABLE_EXCHANGE_TP = True           # 止盈挂交易所侧
FLAG_USE_SHADOW_SCORE_GATE = False       # 影子分门控（A3 检验通过后人工开启）

# ============ 套利失效防护（OP-3） ============
ARB_BASIS_EXIT = 0.005       # 基差(perp/spot-1)向不利方向超过 0.5% → 平对冲仓
ARB_FLIP_HOURS = 16          # 费率向不利方向翻转持续 16 小时（2 个结算周期）→ 平对冲仓
ARB_LEVERAGE = 1             # 对冲本身不需要杠杆，1x 隔离（高杠杆只抬爆仓风险）

# ============ 参数统一维护 · 扩展区（2026-08-16 用户规则:新增参数只能在 config.py 加） ============
# ---- 实盘就绪三盏灯（2026-08-20 用户指示"上实盘三条件做成灯"） ----
READY_MIN_TRADES = 60           # 灯1: 最少平仓样本
READY_SQN_MIN = 1.6             # 灯1: Van Tharp SQN 下限
READY_CRITICAL_DAYS = 7         # 灯2: 连续 N 天零 critical 级异常
READY_MIN_TRUSTED = 3           # 灯3: 最少 trusted 经验条数
READY_MIN_ROLLUPS = 1           # 灯3: 最少场景归纳条数
# ---- 交易所故障退避（2026-08-20 OKX 沙盘下单 API 503/50001 全灭） ----
EXCHANGE_OUTAGE_BACKOFF_SECONDS = 300   # 下单遇 50001/503 暂停开仓 N 秒
                                        # (监控/平仓重试不受影响)
# ---- 每日候选扫描（daily_scan）【激进第二档: 流动性/趋势/波动率门槛放宽,候选更多】 ----
MIN_VOL = 1_000_000           # 24h 成交额下限 USDT（500万→200万→100万）
MIN_PRICE = 0.01              # 最低价格
MIN_TREND_DEV = 0.003         # EMA20 偏离 EMA50 ≥ 0.3% 才算有趋势（0.5%→0.3%）
ATR_SWEET_LOW = 0.003         # 1h ATR% 下限 0.3%（0.5%→0.3%）
ATR_SWEET_HIGH = 0.08         # 1h ATR% 上限 8%（6%→8%）
WATCH_N = 12                  # 每日候选池数量（8→12）
# ---- 经验库（experience_scoring） ----
DECAY_HALFLIFE_DAYS = 30      # 分数向 50 回归的半衰期
REVIVE_DAYS = 60              # discarded 经验 N 天后复活为 unverified
# ---- 日度分析（analyst） ----
WINDOW_DAYS = 7
MIN_TRADES_FOR_STATS = 5      # 统计结论最少样本
MIN_SAMPLES_FOR_ISSUE = 3     # 感知问题最少样本
LOSS_STREAK_ALERT = 3         # 连亏笔数告警线
STOP_BREACH_RATIO = 1.3       # 实亏/预设风险 > 1.3 视为止损被击穿
WIN_RATE_FLOOR = 0.30         # 胜率下限（样本≥5 时）
# ---- 教训聚合（2026-08-17 用户要求: 教训按数据验证强度聚合生效） ----
EVIDENCE_CAP_PER_LESSON = 2   # 单条教训最大贡献权重（good-bad 净验证钳制,防独裁）
STOP_ADJ_TIERS = [(1, 0.2), (3, 0.4), (5, 0.5)]
# 止损放宽分档: (聚合强度门槛, 放宽 ATR 数);硬顶 0.5 ATR,越界即封顶
ROLLUP_MIN_MEMBERS = 3        # 场景归纳教训最少成员数(同 symbol+类别+条件 ≥3 才沉淀)
EVIDENCE_HALFLIFE_DAYS = 30   # 证据权重时间半衰期(天,2026-08-20 FinMem 式衰减):
# evidence_strength/rollup 聚合时,教训按 last_update 距今指数衰减——
# 老教训不再永久满权重,须被新交易反复验证才能保持强度(防市场 regime 漂移)。

# ---- 阈值进化门（2026-08-20 DEF-5 闭环: EvolutionGate 接回生产链路） ----
# 阈值校准(threshold_learning)不再直接生效:先提案→影子验证→达标晋升→观察期退化回滚。
GATE_MIN_SHADOW = 30          # 候选阈值影子样本门槛(Tharp ≥30 笔,与 MIN_SAMPLES 同源)
GATE_MIN_EDGE = 0.001         # 候选须超越现役的最小期望优势(平仓盈亏比例;>0 防平局晋升)
GATE_OBSERVE_BATCH = 10       # 晋升后观察期批大小(每批对比一次,连续退化→回滚基线)

# ---- 试验注册表（experiments） ----
DSR_ACCEPT = 1.0              # Deflated Sharpe 接受线（LdP）
PBO_ACCEPT = 0.3              # PBO 接受线（LdP）
MIN_SAMPLES = 30              # Tharp 最低样本门槛（S2）

# ---- 扫描尺子进化（2026-08-20：提案→影子→验证门→人工批准，永不自动改尺子）----
# 只动一根尺子：REJECT_WICK_RATIO（拒绝K线影线/实体比）。放宽方向先影子记账，
# 用随后 1H K 线走止盈/止损路径算假设盈亏；DSR 达标后仍须 HTTP 批准才写 kv 覆盖。
# config.REJECT_WICK_RATIO 永远是基线/回滚值，机器不得改这个文件。
SCAN_EVOLVE_ENABLED = True
SCAN_EVOLVE_KV_KEY = "scan_evolve.REJECT_WICK_RATIO"
SCAN_EVOLVE_WICK_STEP = 0.9   # 候选 = 现役 × 0.9
SCAN_EVOLVE_WICK_FLOOR = 0.8  # 影线比下限（再低形态太松，与既有 R1 下限一致）
SCAN_EVOLVE_PROFILE_HOURS = 24
SCAN_EVOLVE_SETTLE_BARS = 24  # 影子用随后 24 根 1H 判定止盈/止损/超时
SCAN_EVOLVE_STRATEGY = "A_wick"
# 未触发归因反哺门槛（generate_feedback，原写死在 tools/no_signal_report.py）
FB_MIN_PROFILES = 20          # 画像样本不足则搁置提案
FB_NEAR_MISS_RATE = 0.2       # R1：近失率 ≥20% 且主瓶颈=wick → 影线候选
FB_R2_TREND_PCT = 0.6         # R2：主瓶颈 trend 占比
FB_R3_TOUCH_PCT = 0.7         # R3：主瓶颈 touch 占比（纪律等待，抑制调参）
FB_R4_VOL_PCT = 0.4           # R4：主瓶颈 vol 占比
NEAR_MISS_WICK_FRAC = 0.8     # 影线 ≥ 门槛×0.8 记近失（profile_from_klines）

LEGACY_CT_VAL = {"BTC": 0.01, "ETH": 0.1, "SOL": 0.01, "XRP": 0.001, "DOGE": 1.0,
                 "LINK": 1.0, "ADA": 1.0, "AVAX": 1.0, "BNB": 0.01, "LTC": 1.0}
    # 旧台账回填用合约面值表（legacy size 单位换算,见 trade_journal）

# ---- 策略 B（突破/动量确认,影子模式 Phase 4 T3.3）----
STRATEGY_B_SHADOW_ENABLED = True   # 只记录假设性交易,绝不下单
BREAKOUT_LOOKBACK = 20             # 突破前 N 根 1H K 线的高低点
BREAKOUT_VOL_RATIO = 1.2           # 突破 K 线量能 ≥ 均量 × 1.2 才确认

# ---- 库维护（storage/db.py prune_old_rows）----
DB_RETENTION_DAYS = 90
# 流水日志保留天数。每日候选扫描结束后 prune_old_rows 删除 ts 早于
# 该窗口的 scan_decisions / position_snapshots / signal_profiles /
# engine_errors / shadow_signals / order_failures / analyses(kind='daily'),
# 以及已 resolved 的 alerts / anomalies（按 resolved_ts, 缺则回退 ts）。
# status='new' 的未处理告警即使过期也保留——不能把还没人看的异常清掉。
# 永不清理（台账/经验/研究资产）: trades / lessons / lesson_rollups /
# trade_features / experiments / factor_trials / thresholds / watchlist /
# ownership / untradable_symbols / kv。改此值只影响以后的清理,不恢复已删行。

# ---- 沙盘可交易范围（2026-08-17 实测: 沙盘 demo 缺少部分生产合约）----
DEMO_UNTRADABLE = ["BICO", "GRVT", "AEON", "WLD", "WLFI"]
# 生产行情有、沙盘不可交易的合约,预检拒绝:
#   BICO/GRVT/AEON/WLD → 51001 沙盘无此合约; WLFI → 51087 已退市
# 新符号遇同类错误码由 _log_order_failure 自动记入 untradable_symbols 表,
# 预检合并查询(配置 + 动态表),后续无需人工扩表。


# ============ 实盘模式（2026-08-22 用户拍板: 小预算实盘,预算 100 USDT） ============
# 激活条件: 本开关 + ~/.crypto_live/okx_live.json(真实密钥,仓库外)。
# LIVE_MODE 只在引擎启动时快照(self.live_mode),热重载不改它——
# 防止运行中途意外切换真实/模拟。
LIVE_MODE = (CRYPTO_MODE == "live")   # 实盘开关(由 CRYPTO_AGENT_MODE 决定,2026-08-23 双实例)
LIVE_BUDGET_USDT = 100        # 总预算
LIVE_RISK_PER_TRADE = 1.0     # 单笔风险 USDT(预算 1%)
LIVE_MAX_NOTIONAL = 10        # 单笔名义上限 USDT(2026-08-23 用户指示 20→10)
LIVE_MAX_TOTAL = 100          # 总敞口上限 USDT
LIVE_HARD_STOP_USDT = 30      # 累计实亏达 30 USDT(预算30%) → 自动停手
# 2026-08-23 用户指示: BTC/ETH 用 10x 杠杆(其余币 B+C 分档 3x-5x)
LIVE_LEVERAGE_MAP = {"BTC": 10, "ETH": 10}
# BTC/ETH 的名义上限=最小合约名义(BTC 0.01≈680 / ETH 0.01≈25),
# 覆盖 10 USDT 通用上限——否则永远买不起最小张数
LIVE_SPECIAL_NOTIONAL = {"BTC": 680, "ETH": 230}
LIVE_CRED_FILE = "~/.crypto_live/okx_live.json"

# ============ 交易所适配后端（2026-08-22 用户指示"用 ccxt 交易库"） ============
EXCHANGE_BACKEND = "ccxt"     # "ccxt" | "native"——引擎构造时选择适配器
                              # (切换需重启;ccxt 已在沙盘全链路冒烟通过)

# ============ 实时行情后端（2026-08-23 用户指示"换"用 ccxt 实时监听接口） ============
REALTIME_BACKEND = "ccxtpro"  # "ccxtpro"(watch_ticker) | "okx"(原生WS,可回滚)
                              # (切换需重启)

# ============ 经验共享（2026-08-23 用户指示"经验共享"——双实例教训互同步） ============
# 模拟盘激进采集 → 教训/验证状态镜像到实盘库参与决策;实盘真金验证 → 镜像回模拟盘。
# 每条教训由【产生它的实例】拥有并验证(origin),对端只读镜像,避免双重计数。
EXPERIENCE_SHARE_ENABLED = True   # 开关
EXPERIENCE_PEER_DB = os.environ.get("CRYPTO_AGENT_PEER_DB", "")
                                  # 对端实例库路径(launchd 环境变量注入)
EXPERIENCE_SHARE_INTERVAL_HOURS = 1   # 同步周期(启动时 + 每小时)
EXPERIENCE_PEER_WEIGHT = 1.0      # 对端镜像教训在决策聚合中的权重(1.0=等权)
STRATEGY_SYNC_ENABLED = True      # 2026-08-23 用户指示"策略也保持一致":
                                  # 阈值学习状态 + 扫描尺子进化状态双向合并
STRATEGY_SYNC_MAX_RECORDS = 500   # 阈值校准样本并集上限(与学习器 max_history 一致)

# ============ 消息面门控（2026-08-23 用户要求'系统加消息面判断'） ============
SENTIMENT_GATE_ENABLED = True  # 情感门控开关(决策层读 kv 快照,无数据放行)
SENTIMENT_GREED_CAP = 80       # F&G ≥ 此值 → 拒绝新开多(不追过热顶)
SENTIMENT_FEAR_FLOOR = 20      # F&G ≤ 此值 → 拒绝新开空(不空恐慌底)
SENTIMENT_REFRESH_HOURS = 1    # worker 每小时刷新一次情感快照
SENTIMENT_FNG_URL = "https://api.alternative.me/fng/?limit=1"
SENTIMENT_BULL_WORDS = ("rally", "surge", "soar", "bull", "breakout", "record",
                        "gain", "rebound", "adopt", "pump", "high", "rise",
                        "recover")
SENTIMENT_BEAR_WORDS = ("crash", "plunge", "bear", "liquidat", "fear", "dump",
                        "hack", "ban", "lawsuit", "selloff", "fall", "drop",
                        "loss")

# ============ 热重载机制（2026-08-21 用户要求'配置动态读取'） ============
# 改 config.py 保存后,引擎下一拍(≤1s,worker tick 调用 maybe_reload)
# 自动生效,无需重启。机制: mtime 变化时把本文件重新 exec 进本模块
# 命名空间(原地覆盖),所有 config.X 引用立刻看到新值。
# 各引擎模块的历史别名已改为模块级 __getattr__ 转发,同样动态。
import os as _os

_CONFIG_MTIME = _os.path.getmtime(__file__)


def maybe_reload():
    """mtime 变了就原地重载。返回变化了的键名列表(供告警/审计)。"""
    global _CONFIG_MTIME
    try:
        m = _os.path.getmtime(__file__)
    except OSError:
        return []
    if m == _CONFIG_MTIME:
        return []
    before = {k: v for k, v in globals().items()
              if k.isupper() and not k.startswith("_")}
    src = open(__file__, encoding="utf-8").read()
    exec(compile(src, __file__, "exec"), globals())
    _CONFIG_MTIME = _os.path.getmtime(__file__)
    changed = [k for k in before
               if k in globals() and globals().get(k) != before.get(k)]
    return changed

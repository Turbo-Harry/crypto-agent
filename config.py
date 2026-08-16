"""
策略配置 — 全部参数集中管理
核心理念：宁可做对，也不做错；空仓是默认，持仓是例外。
激进档：最大回撤 15%~20%，单笔风险 1.5%，+5%/-3% 盈亏比。
"""

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
# 用户确认：美股代币也有永续合约（如 ANTHROPIC-USDT-SWAP，实测 live，ctVal=1）。
# 这类币只有合约、没有现货，无法走"现货代币"路径，单独列进扫描范围。
# 有现货+合约的（XIAOMI 等）已在 X 前缀现货代币清单里；此表只放"仅合约"的。
STOCK_SWAP_TOKENS = ["ANTHROPIC"]

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
STOP_LOSS = 0.03             # 止损 -3%（固定百分比，旧版）
STOP_ATR_MULT = 1.5          # ATR 动态止损倍数（止损 = 入场价 - 1.5×ATR(14)）
TAKE_PROFIT_1 = 0.03         # 第一止盈 +3%（平 1/2）
TAKE_PROFIT_2 = 0.05         # 第二止盈 +5%（清仓）
TIME_STOP_DAYS = 10          # 时间止损：入场后 N 日无方向退出

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
SIGNAL_COOLDOWN_MINUTES = 30  # 同币信号冷却 30 分钟（激进第二档;原 180→60→30）
MTF_ENABLED = False           # 多周期共振过滤关闭（采集加速;tf4h_spread 特征已记录,可事后检验;可回滚 True）

# ============ 策略参数统一维护（2026-08-16 用户指示:数值不再分散在各模块） ============
# 改交易门槛只改这里;各模块一律 import config 引用,禁止私藏副本。
# 注意三件套联动关系（不满足会导致"全部信号被拒"或"门槛失效"）:
#   THRESHOLD_INITIAL < DECIDE_MIN_SCORE <= SIGNAL_SCORE
SIGNAL_SCORE = 40            # 回踩确认信号基础分（激进第二档: 80→50→40）
DECIDE_MIN_SCORE = 40        # 决策层最低信号分（与 SIGNAL_SCORE 联动）
THRESHOLD_INITIAL = 35       # 阈值学习层初始阈值（联动约束: < DECIDE_MIN_SCORE）
REJECT_WICK_RATIO = 1.0      # 拒绝K线: 影线/实体 最小比（激进第二档 1.5→1.0,信号更多）
STOP_ATR_MULT = 1.0          # 止损距离 = N × ATR
TP_ATR_MULT = 2.0            # 止盈距离 = N × ATR（2:1 盈亏比）
RISK_PER_TRADE = 0.01        # 单笔风险 1%（红线,改动需用户明确拍板）
MAX_NOTIONAL_PER_TRADE = 150 # 单笔名义上限 USDT（红线）
MAX_TOTAL_NOTIONAL = 600     # 组合总敞口上限 USDT（红线,PositionLedger 共用）
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE",
           "LINK", "ADA", "AVAX", "BNB", "LTC"]   # 回退主流池（采集加速扩到 10 个）
LEVERAGE_MAP = {"BTC": 3, "ETH": 3, "SOL": 3, "XRP": 3, "DOGE": 3,
                "LINK": 3, "ADA": 3, "AVAX": 3, "BNB": 3, "LTC": 3}
FLAG_ENABLE_EXCHANGE_TP = False          # 止盈挂交易所侧（默认关,沙盘验证后开启）
FLAG_USE_SHADOW_SCORE_GATE = False       # 影子分门控（A3 检验通过后人工开启）

# ============ 套利失效防护（OP-3） ============
ARB_BASIS_EXIT = 0.005       # 基差(perp/spot-1)向不利方向超过 0.5% → 平对冲仓
ARB_FLIP_HOURS = 16          # 费率向不利方向翻转持续 16 小时（2 个结算周期）→ 平对冲仓
ARB_LEVERAGE = 1             # 对冲本身不需要杠杆，1x 隔离（高杠杆只抬爆仓风险）

# ============ 参数统一维护 · 扩展区（2026-08-16 用户规则:新增参数只能在 config.py 加） ============
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
# ---- 试验注册表（experiments） ----
DSR_ACCEPT = 1.0              # Deflated Sharpe 接受线（LdP）
PBO_ACCEPT = 0.3              # PBO 接受线（LdP）
MIN_SAMPLES = 30              # Tharp 最低样本门槛（S2）

LEGACY_CT_VAL = {"BTC": 0.01, "ETH": 0.1, "SOL": 0.01, "XRP": 0.001, "DOGE": 1.0,
                 "LINK": 1.0, "ADA": 1.0, "AVAX": 1.0, "BNB": 0.01, "LTC": 1.0}
    # 旧台账回填用合约面值表（legacy size 单位换算,见 trade_journal）

# ---- 策略 B（突破/动量确认,影子模式 Phase 4 T3.3）----
STRATEGY_B_SHADOW_ENABLED = True   # 只记录假设性交易,绝不下单
BREAKOUT_LOOKBACK = 20             # 突破前 N 根 1H K 线的高低点
BREAKOUT_VOL_RATIO = 1.2           # 突破 K 线量能 ≥ 均量 × 1.2 才确认

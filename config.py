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
RISK_PER_TRADE = 0.015       # 单笔风险预算 1.5%
MAX_POSITION_PER_COIN = 0.40 # 单币最大仓位 40%
MAX_HOLDINGS = 4             # 同时持仓数

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
# 2026-08-16 用户指示"策略可以激进点，为了采集数据"：额度翻倍 + 冷却 3h→1h +
# MTF 共振过滤临时关闭（1h 单周期信号即可入场；4h 离散度已作为特征记录,
# 事后可用数据检验 MTF 是否真有价值——把预设变成可检验假设,可随时回滚）。
# 【风险红线不变】: 单笔 1% 风险 / 名义 ≤150 / 总敞口 ≤600 / 交易所侧止损。
TRADE_BUDGET_BY_SCORE = [(0.7, 8), (0.5, 6), (0.3, 4), (0.0, 2)]  # (评分阈值, 允许笔数)
DEFAULT_TRADE_BUDGET = 4      # 无评分（回退池）时的默认笔数
SIGNAL_COOLDOWN_MINUTES = 60  # 同币信号冷却 1 小时（采集加速;原 180）
MTF_ENABLED = False           # 多周期共振过滤临时关闭（采集加速;tf4h_spread 特征已记录,可事后检验;可回滚 True）

# ============ 套利失效防护（OP-3） ============
ARB_BASIS_EXIT = 0.005       # 基差(perp/spot-1)向不利方向超过 0.5% → 平对冲仓
ARB_FLIP_HOURS = 16          # 费率向不利方向翻转持续 16 小时（2 个结算周期）→ 平对冲仓
ARB_LEVERAGE = 1             # 对冲本身不需要杠杆，1x 隔离（高杠杆只抬爆仓风险）

"""日内因子注册表：公式、方向、数据源、缺失策略、理论依据和版本单点维护。"""
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from decision.feature_transforms import materialize_derived_features


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    family: str
    formula: str
    expected_direction: str
    source: str
    missing_policy: str
    rationale: str
    version: str = "v1"


_SPECS = (
    FeatureSpec("wick", "price_action", "capped wick/body score", "positive",
                "15m OHLC", "median_indicator", "拒绝影线刻画局部流动性吸收，形态证据较弱，仅作候选"),
    FeatureSpec("depth", "price_action", "1-|pullback-EMA20|/ATR", "positive",
                "15m OHLC", "median_indicator", "以 ATR 标准化回踩深度，避免价格尺度污染"),
    FeatureSpec("trend", "momentum", "|EMA20-EMA50|/(EMA50*2%)", "positive",
                "15m OHLC", "median_indicator", "时间序列动量与趋势持续性"),
    FeatureSpec("volume", "liquidity", "last volume / 20-bar mean", "positive",
                "15m volume", "median_indicator", "量价确认与信息到达强度"),
    FeatureSpec("funding", "crowding", "direction-aligned funding score", "unknown",
                "perpetual funding", "median_indicator", "资金费率代理永续合约拥挤方向"),
    FeatureSpec("book", "microstructure", "direction-aligned depth imbalance", "positive",
                "OKX order book", "median_indicator", "盘口不平衡与短期价格冲击"),
    FeatureSpec("wick_ratio", "price_action", "raw wick/body", "unknown",
                "15m OHLC", "drop", "原始影线强度，验证非线性阈值是否有样本外信息"),
    FeatureSpec("pullback_depth_atr", "price_action", "|touch-EMA20|/ATR", "negative",
                "15m OHLC", "drop", "尺度无关的回踩距离"),
    FeatureSpec("trend_band_atr", "momentum", "(EMA20-EMA50)/ATR", "directional",
                "15m OHLC", "drop", "趋势带宽相对波动率"),
    FeatureSpec("volume_ratio", "liquidity", "volume/lagged mean volume", "positive",
                "15m volume", "drop", "异常成交量可能确认信息驱动走势"),
    FeatureSpec("funding_rate", "crowding", "current perpetual funding", "unknown",
                "OKX funding", "drop", "资金成本与拥挤反转/延续需由方向交互验证"),
    FeatureSpec("book_imbalance", "microstructure", "(bidDepth-askDepth)/totalDepth",
                "directional", "OKX L2 book", "drop", "Cont 等微观结构模型中的订单簿失衡"),
    FeatureSpec("microprice_bps", "microstructure", "(microprice-mid)/mid*1e4",
                "directional", "OKX best bid/ask", "drop", "队列加权微观价格预测下一跳方向"),
    FeatureSpec("spread_bps", "execution", "(ask-bid)/mid*1e4", "negative",
                "OKX L2 book", "drop", "价差代理即时交易成本和流动性风险"),
    FeatureSpec("depth_slope", "microstructure", "top3 depth / topN depth", "unknown",
                "OKX L2 book", "drop", "深度集中度影响冲击成本"),
    FeatureSpec("cancel_imbalance", "microstructure", "(ask cancels-bid cancels)/total cancels",
                "directional", "successive best-book snapshots", "drop",
                "同价档撤单失衡补充静态深度，无法比较时明确缺失"),
    FeatureSpec("expected_slippage_bps", "execution", "half spread + notional/visible depth",
                "negative", "OKX L2 book", "drop", "信号时点可见深度下的保守冲击成本代理"),
    FeatureSpec("realized_vol_1h", "volatility", "sqrt(sum(last4 15m r^2))", "unknown",
                "15m OHLC", "drop", "最近 1h 实现波动与波动聚集"),
    FeatureSpec("realized_vol_5m", "volatility", "sqrt(sum(last12 5m r^2))", "unknown",
                "5m OHLC", "drop", "最近 1h 的 5m 实现波动反映当前交易时段风险",
                "v2"),
    FeatureSpec("vol_of_vol", "volatility", "stdev(rolling 1h realized vol)", "negative",
                "5m OHLC", "drop", "波动率之波动刻画不稳定 regime"),
    FeatureSpec("har_rv", "volatility", "mean(RV_1h,RV_6h,RV_24h)", "unknown",
                "5m OHLC", "drop", "HAR-RV 异质周期近似捕捉波动率长记忆"),
    FeatureSpec("downside_semivol_1h", "volatility", "sqrt(sum(negative r^2))", "negative",
                "15m OHLC", "drop", "最近 1h 下半方差区分不利波动"),
    FeatureSpec("atr_pct", "volatility", "ATR/entry", "unknown",
                "15m OHLC", "drop", "波动尺度与止损成本"),
    FeatureSpec("ofi_dynamic", "microstructure", "delta bid/ask queue flow / depth",
                "directional", "sparse signal-time snapshots", "drop",
                "旧版相邻信号快照差，仅兼容历史样本，不冒充连续事件流"),
    FeatureSpec("ofi_event_multilevel", "microstructure",
                "sum multilevel queue-event OFI / sum visible depth",
                "directional", "continuous top-5 L2 events over 60s", "drop",
                "Cont 队列事件规则扩展到多档；只在事件数和新鲜度达标时留值"),
    FeatureSpec("ofi_event_cancel_imbalance", "microstructure",
                "(ask queue depletion-bid queue depletion)/total depletion",
                "directional", "continuous same-price L2 events", "drop",
                "同价可见队列消退方向用于刻画撤单/成交后的流动性吸收"),
    FeatureSpec("ofi_event_count", "quality", "L2 events in rolling window",
                "unknown", "continuous L2 events", "keep",
                "事件密度是盘口特征可用性与市场活跃度的显式质量变量"),
    FeatureSpec("ofi_event_age_ms", "quality", "signal time-last L2 event time",
                "negative", "continuous L2 events", "keep",
                "数据年龄防止模型把断流后的旧盘口当成当前订单流"),
    FeatureSpec("open_interest_change", "crowding", "delta OI / lagged OI", "unknown",
                "perpetual OI", "drop", "持仓变化与价格交互刻画新钱/平仓"),
    FeatureSpec("oi_price_interaction", "crowding", "OI change * 1h price momentum",
                "unknown", "perpetual OI + 15m OHLC", "drop", "区分增仓上涨、增仓下跌与被动平仓"),
    FeatureSpec("basis", "crowding", "perpetual/spot-1", "unknown",
                "perpetual+spot", "drop", "期限基差代理杠杆需求与拥挤"),
    FeatureSpec("funding_change", "crowding", "funding_t-funding_previous_snapshot", "unknown",
                "perpetual funding snapshots", "drop", "资金费变化刻画拥挤加速或消退"),
    FeatureSpec("funding_percentile", "crowding", "cross-sectional empirical rank", "unknown",
                "perpetual funding snapshots", "drop", "截面极端资金费比绝对数值更稳健"),
    FeatureSpec("momentum_1h", "momentum", "sum(last4 15m log returns)", "directional",
                "15m OHLC", "drop", "短周期时间序列动量"),
    FeatureSpec("momentum_4h", "momentum", "sum(last16 15m log returns)", "directional",
                "15m OHLC", "drop", "较慢趋势确认，防止只看单周期"),
    FeatureSpec("cross_sectional_rank", "momentum",
                "cross-sectional rank of last4 15m log returns", "directional",
                "multi-asset 15m OHLC", "drop",
                "同一时点 1h 动量截面排名降低市场共同波动对绝对动量的污染", "v2"),
    FeatureSpec("btc_residual_momentum", "cross_asset", "asset return-beta*BTC return",
                "directional", "multi-asset OHLC", "drop",
                "剔除市场 beta 后的特异动量", "v2"),
    FeatureSpec("btc_beta", "cross_asset", "cov(asset,BTC)/var(BTC)", "unknown",
                "multi-asset OHLC", "drop", "显式暴露系统性 BTC 风险载荷"),
    FeatureSpec("market_breadth", "regime", "share of universe above EMA20", "positive",
                "same-time multi-asset 15m OHLC", "drop",
                "市场宽度区分单币异动与广泛趋势", "v2"),
    FeatureSpec("correlation_concentration", "regime", "leading eigenvalue share",
                "negative", "same-time multi-asset 15m returns", "drop",
                "相关性集中度上升代表系统性风险", "v2"),
    FeatureSpec("bb_width_percentile", "regime",
                "rank((upper-lower)/middle over trailing 100 bars)", "unknown",
                "15m OHLC", "drop", "布林带宽相对分位识别挤压与波动扩张，避免绝对尺度", "v1"),
    FeatureSpec("bb_percent_b", "regime", "(close-lower)/(upper-lower)", "unknown",
                "15m OHLC", "drop", "价格在条件波动区间中的位置；触轨不预设反转", "v1"),
    FeatureSpec("bb_squeeze_release", "regime",
                "positive width expansion after low-percentile squeeze", "positive",
                "15m OHLC", "drop", "低波动聚集后的带宽释放是突破候选而非方向保证", "v1"),
    FeatureSpec("adx", "momentum", "Wilder ADX14 / 100", "positive",
                "15m OHLC", "drop", "方向无关的趋势强度，补充均线离散度", "v1"),
    FeatureSpec("directional_index_spread", "momentum", "(DI+-DI-)/100", "directional",
                "15m OHLC", "drop", "Wilder 方向运动差刻画趋势方向和强度", "v1"),
    FeatureSpec("kaufman_efficiency_ratio", "regime",
                "abs(net move)/sum(abs bar moves) over 20 bars", "positive",
                "15m OHLC", "drop", "区分单向有效移动与来回噪声", "v1"),
    FeatureSpec("vwap_distance_atr", "regime", "(close-rolling VWAP20)/ATR14",
                "directional", "15m OHLCV", "drop", "价格相对成交重心的位置并按波动归一", "v1"),
    FeatureSpec("vwap_crossing_rate", "regime", "sign crossings of close-VWAP20",
                "negative", "15m OHLCV", "drop", "频繁穿越成交重心通常对应震荡噪声", "v1"),
    FeatureSpec("volume_zscore", "liquidity", "(volume-lagged mean)/lagged std",
                "positive", "15m volume", "drop", "量能异常程度比简单均量比更可比", "v1"),
    FeatureSpec("hour_sin", "seasonality", "sin(2*pi*UTC hour/24)", "unknown",
                "event timestamp", "keep", "加密市场日内流动性周期"),
    FeatureSpec("hour_cos", "seasonality", "cos(2*pi*UTC hour/24)", "unknown",
                "event timestamp", "keep", "与 hour_sin 共同无断点编码时段"),
    FeatureSpec("weekend", "seasonality", "1 if UTC weekend else 0", "unknown",
                "event timestamp", "keep", "周末流动性与参与者结构不同"),
    FeatureSpec("source_latency_ms", "execution", "event_ts-kline_close_ts", "negative",
                "signal clock", "drop", "过时信号的执行质量通常更差"),
    FeatureSpec("feature_missing_rate", "quality", "missing feature count / feature count", "negative",
                "signal snapshot", "keep", "模型置信度应显式感知输入质量"),
    FeatureSpec("trend_volume_confirmation", "momentum",
                "trend score * volume ratio", "positive",
                "15m OHLCV frozen snapshot", "drop",
                "趋势需要成交量确认；交互项区分有参与度的趋势与低量漂移"),
    FeatureSpec("wick_volume_absorption", "price_action",
                "direction-aligned wick score * volume ratio", "positive",
                "15m OHLCV frozen snapshot", "drop",
                "放量拒绝影线更可能反映流动性吸收，而非低量随机形态"),
    FeatureSpec("pullback_volume_confirmation", "price_action",
                "EMA20 pullback quality * volume ratio", "positive",
                "15m OHLCV frozen snapshot", "drop",
                "回踩位置与成交参与度联合刻画趋势中的承接确认"),
    FeatureSpec("trend_wick_alignment", "price_action",
                "trend score * direction-aligned wick score", "positive",
                "15m OHLC frozen snapshot", "drop",
                "趋势强度与顺方向拒绝形态同时出现，减少孤立 K 线形态误报"),
    FeatureSpec("momentum_volume_confirmation", "momentum",
                "1h log-return momentum * volume ratio", "directional",
                "15m OHLCV frozen snapshot", "drop",
                "带成交量确认的时间序列动量；空头样本在研究层统一反向"),
    FeatureSpec("rv_to_har_ratio", "volatility", "RV_1h/HAR_RV",
                "unknown", "5m OHLC", "drop", "实际短波动超过异质周期基线时提示波动状态切换"),
    FeatureSpec("bb_squeeze_volume_confirmation", "regime",
                "bb squeeze release * volume z-score", "positive",
                "15m OHLCV frozen snapshot", "drop", "带异常成交参与的波动释放比无量扩张更可信"),
)

REGISTRY = {spec.name: spec for spec in _SPECS}


def describe_registry():
    return [asdict(spec) for spec in _SPECS]


def extract_features(sample: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """只读取 signal_samples 已冻结的信号时点快照，不计算任何未来字段。"""
    try:
        snapshot = json.loads(sample.get("features") or "{}")
    except Exception:
        snapshot = {}
    raw = dict(snapshot.get("factor_features") or {})
    dims = dict(snapshot.get("shadow_dims") or {})
    values = {}
    for name in REGISTRY:
        value = sample.get(name) if name in sample else None
        if value is None:
            value = dims.get(name, raw.get(name))
        try:
            values[name] = float(value) if value is not None else None
        except (TypeError, ValueError):
            values[name] = None
    values.update(materialize_derived_features(values))
    return values

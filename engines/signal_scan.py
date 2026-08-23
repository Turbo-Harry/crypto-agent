"""
信号扫描层（SignalScanMixin）— 2026-08-20 从 directional_trader 按功能拆分。

职责：回踩确认信号（15m 主周期 + 1H/4H 环境）、候选池扫描主循环、动态笔数额度、
信号冷却、扫描决策落库、长扫描逐币插拍（心跳/监控/快照）。
方法体与拆分前逐行一致（行为零变化）；宿主为 DirectionalTrader（MRO 组装）。
依赖宿主属性：exchange/journal/evolver/rt/watchlist/watch_scores/
threshold_learner/signal_cool/_db_path/_notify 等。
"""
import hashlib
import math
import time
from copy import deepcopy

import config
from decision.feature_transforms import (cross_sectional_snapshot,
                                         materialize_derived_features,
                                         technical_regime_features,
                                         volatility_5m_features)
from decision.market_regime import classify_market_regime
from decision.strategy_router import route_strategy
MTF_ENABLED = config.MTF_ENABLED
SIGNAL_COOLDOWN_MINUTES = config.SIGNAL_COOLDOWN_MINUTES
SYMBOLS = config.SYMBOLS
SIGNAL_SCORE = config.SIGNAL_SCORE
FLAG_USE_SHADOW_SCORE_GATE = config.FLAG_USE_SHADOW_SCORE_GATE


def _refresh_config():
    """2026-08-21 热重载: config.maybe_reload 后由 worker 调用,
    把本模块别名刷新为新值(函数体裸名引用在调用时读模块全局)。"""
    global MTF_ENABLED
    MTF_ENABLED = config.MTF_ENABLED
    global SIGNAL_COOLDOWN_MINUTES
    SIGNAL_COOLDOWN_MINUTES = config.SIGNAL_COOLDOWN_MINUTES
    global SYMBOLS
    SYMBOLS = config.SYMBOLS
    global SIGNAL_SCORE
    SIGNAL_SCORE = config.SIGNAL_SCORE
    global FLAG_USE_SHADOW_SCORE_GATE
    FLAG_USE_SHADOW_SCORE_GATE = config.FLAG_USE_SHADOW_SCORE_GATE



from strategy.indicators import ema, atr


def compute_shadow_score(wick, body, price_near_ema, ema20_val, ema50_val,
                         atr_val, vol_last, vol_avg, funding_rate, book_imb,
                         direction, weights=None):
    """信号影子连续分(0-100),2026-08-23 用户指示"维度太少了,加"后 3 维→6 维,
    权重按文献证据强度排序的先验(config.SHADOW_WEIGHTS,权重进化再按 IC 校正):
      1. wick    拒绝K线强度(影线/实体,封顶3x)   — 15% (形态本体,弱证据)
      2. depth   回踩深度适中(贴EMA20/ATR)      — 16% (业界共识)
      3. trend   15m 趋势离散度(EMA20-50 带宽)  — 20% (动量,强证据)
      4. volume  量能确认(近N根均量比,封顶2x)   — 12% (量价,效应弱但真实)
      5. funding 资金费顺风(多单负费率/空单正)  — 15% (拥挤度,中证据)
      6. book    盘口失衡(前10档,方向对齐)      — 22% (微观结构,最强证据)
    数据缺失的维度取 0.5 中性,不污染总分;权重和必须=1.0(config.SHADOW_WEIGHTS)。
    纯函数(无 IO),便于单元测试与回放。
    返回 (score, dims): dims 为 6 维子分 dict(权重进化证据采集用)。"""
    w = weights or config.SHADOW_WEIGHTS
    try:
        wick_s = min(wick / body, 3.0) / 3.0 if body > 0 else 0.0
        depth_s = (max(0.0, 1.0 - abs(price_near_ema - ema20_val) / atr_val)
                   if atr_val and atr_val > 0 else 0.0)
        trend_s = (min(abs(ema20_val - ema50_val) / (ema50_val * 0.02), 1.0)
                   if ema50_val else 0.0)
        vol_s = (min(vol_last / max(vol_avg, 1e-12), 2.0) / 2.0
                 if vol_last and vol_avg else 0.5)
        if funding_rate is None:
            fund_s = 0.5
        else:
            sign = 1.0 if direction == "long" else -1.0
            fund_s = max(0.0, min(1.0, 0.5 - sign * float(funding_rate) / 0.001))
        if book_imb is None:
            book_s = 0.5
        else:
            imb = max(-1.0, min(1.0, float(book_imb)))
            book_s = (imb if direction == "long" else -imb) / 2 + 0.5
        dims = {"wick": round(wick_s, 4), "depth": round(depth_s, 4),
                "trend": round(trend_s, 4), "volume": round(vol_s, 4),
                "funding": round(fund_s, 4), "book": round(book_s, 4)}
        score = 100 * (w.get("wick", 0) * wick_s + w.get("depth", 0) * depth_s
                       + w.get("trend", 0) * trend_s + w.get("volume", 0) * vol_s
                       + w.get("funding", 0) * fund_s + w.get("book", 0) * book_s)
        return round(score, 1), dims
    except Exception:
        return None, None


def compute_targets(entry, atr, direction, swing_level=None):
    """目标价位带(2026-08-23 用户问"会预测会升到什么价位吗"):
    T1=1×ATR(第一目标) / T2=2×ATR(现役止盈位) / T3=结构位(近20根摆动高低点,
    超出 T2 才列入——结构位是对"会升到哪"的实证参照,不是保证)。
    纯函数,便于测试与回放。"""
    t1 = entry + atr if direction == "long" else entry - atr
    t2 = entry + 2 * atr if direction == "long" else entry - 2 * atr
    t3 = None
    if swing_level:
        if direction == "long" and swing_level > t2:
            t3 = swing_level
        elif direction == "short" and swing_level < t2:
            t3 = swing_level
    return {"t1": round(t1, 6), "t2": round(t2, 6),
            "t3": round(t3, 6) if t3 else None}


def detect_pullback_setup(last, ema20_val, ema50_val, wick_ratio,
                          tf1h_trend=0, tf4h_trend=0, mtf_enabled=False):
    """纯函数回踩形态门，供活体扫描与历史重放共享同一判定。

    只判断已收线 K 的趋势、EMA20 回踩、拒绝影线和可选 MTF；不读取时钟、
    行情或数据库。返回 direction/wick/touch，未命中返回 None。
    """
    body = abs(float(last["close"]) - float(last["open"]))
    lower_wick = min(float(last["open"]), float(last["close"])) - float(last["low"])
    upper_wick = float(last["high"]) - max(float(last["open"]), float(last["close"]))
    ratio = float(wick_ratio)
    if (ema20_val > ema50_val and float(last["low"]) <= ema20_val and
            float(last["close"]) > ema20_val and lower_wick >= body * ratio):
        if mtf_enabled and (tf1h_trend != 1 or tf4h_trend != 1):
            return None
        return {"direction": "long", "wick": lower_wick,
                "touch": float(last["low"]), "body": body}
    if (ema20_val < ema50_val and float(last["high"]) >= ema20_val and
            float(last["close"]) < ema20_val and upper_wick >= body * ratio):
        if mtf_enabled and (tf1h_trend != -1 or tf4h_trend != -1):
            return None
        return {"direction": "short", "wick": upper_wick,
                "touch": float(last["high"]), "body": body}
    return None


def _book_imbalance(book, depth=10):
    """盘口失衡(2026-08-23): (买深度−卖深度)/(买+卖),[-1,1];正=买盘厚。"""
    if not book:
        return None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    b = sum(x[1] for x in bids[:depth])
    a = sum(x[1] for x in asks[:depth])
    if b + a <= 0:
        return None
    return (b - a) / (b + a)


def _microstructure_features(book, depth=10):
    """从信号时点盘口提取价差/微观价格/多档深度；无数据返回 None。"""
    if not book or not book.get("bids") or not book.get("asks"):
        return {"spread_bps": None, "microprice_bps": None,
                "depth_imbalance": None, "depth_slope": None,
                "expected_slippage_bps": None}
    try:
        bids = [(float(row[0]), float(row[1])) for row in book["bids"][:depth]]
        asks = [(float(row[0]), float(row[1])) for row in book["asks"][:depth]]
        bid, bid_qty = bids[0]
        ask, ask_qty = asks[0]
        mid = (bid + ask) / 2
        micro = (ask * bid_qty + bid * ask_qty) / max(bid_qty + ask_qty, 1e-12)
        bid_depth, ask_depth = sum(row[1] for row in bids), sum(row[1] for row in asks)
        top_depth = sum(row[1] for row in bids[:3]) + sum(row[1] for row in asks[:3])
        full_depth = bid_depth + ask_depth
        visible_notional = sum(price * qty for price, qty in bids + asks)
        impact_bps = (config.MAX_NOTIONAL_PER_TRADE / visible_notional * 10_000
                      if visible_notional > 0 else None)
        return {
            "spread_bps": (ask - bid) / mid * 10_000 if mid else None,
            "microprice_bps": (micro - mid) / mid * 10_000 if mid else None,
            "depth_imbalance": ((bid_depth - ask_depth) / full_depth
                                if full_depth else None),
            "depth_slope": top_depth / full_depth if full_depth else None,
            "expected_slippage_bps": ((ask - bid) / mid * 5_000 + impact_bps
                                      if mid and impact_bps is not None else None),
        }
    except Exception:
        return {"spread_bps": None, "microprice_bps": None,
                "depth_imbalance": None, "depth_slope": None,
                "expected_slippage_bps": None}


def _dynamic_ofi(book, previous):
    """Cont 式最佳档净订单流变化，以相邻信号快照深度归一。"""
    try:
        bid, bq = float(book["bids"][0][0]), float(book["bids"][0][1])
        ask, aq = float(book["asks"][0][0]), float(book["asks"][0][1])
        current = (bid, bq, ask, aq)
        if not previous:
            return None, current
        pbid, pbq, pask, paq = previous
        bid_flow = bq if bid > pbid else (bq - pbq if bid == pbid else -pbq)
        ask_flow = aq if ask < pask else (aq - paq if ask == pask else -paq)
        scale = max(bq + aq + pbq + paq, 1e-12) / 2
        return (bid_flow - ask_flow) / scale, current
    except Exception:
        return None, previous


def _cancellation_imbalance(current, previous):
    """同价最佳档撤单失衡：(卖撤-买撤)/(卖撤+买撤)，无可比快照则缺失。"""
    try:
        bid, bq, ask, aq = current
        pbid, pbq, pask, paq = previous
        if bid != pbid or ask != pask:
            return None
        bid_cancel = max(0.0, pbq - bq)
        ask_cancel = max(0.0, paq - aq)
        total = bid_cancel + ask_cancel
        return (ask_cancel - bid_cancel) / total if total else 0.0
    except Exception:
        return None

# 参数别名（统一维护于 config.py,本模块不私藏数值）


def _build_trade_conditions(sig):
    """信号/交易的场景条件向量(2026-08-17): direction + vol_band + trend +
    signal_type。regime 是 compute_regime 输出的 dict(含 tag/trend_slope)。"""
    from decision.experience_scoring import build_conditions
    return build_conditions(direction=sig.get("dir"),
                            regime_dict=sig.get("regime"),
                            signal_type="pullback")



class SignalScanMixin:
    """信号扫描功能块。"""

    def _cross_sectional_factors(self, base, current_kl):
        """同一已收线 15m 截止点的跨币状态；每根 K 最多全池拉取一次。"""
        if not current_kl:
            return {}
        cutoff_open = int(current_kl[-1][0])
        cache = getattr(self, "_factor_cross_section_cache", {})
        snapshot = cache.get("snapshot") if cache.get("kline_ts") == cutoff_open else None
        if snapshot is None:
            universe = list(dict.fromkeys(
                list(getattr(self, "watchlist", []) or []) + list(SYMBOLS)))
            closes_by_symbol = {}
            for symbol in universe:
                try:
                    rows = (current_kl if symbol == base else
                            self._fetch_klines_any(
                                symbol, config.SIGNAL_SAMPLE_TIMEFRAME,
                                config.FACTOR_CROSS_SECTION_LOOKBACK_BARS + 2))
                    rows = [row for row in (rows or [])
                            if int(row[0]) <= cutoff_open]
                    closes = [float(row[4]) for row in
                              rows[-config.FACTOR_CROSS_SECTION_LOOKBACK_BARS:]
                              if float(row[4]) > 0]
                    if closes:
                        closes_by_symbol[symbol] = closes
                except Exception:
                    continue
            snapshot = cross_sectional_snapshot(closes_by_symbol)
            if snapshot.get("market_breadth") is not None:
                self._factor_cross_section_cache = {
                    "kline_ts": cutoff_open, "snapshot": snapshot}
        per_symbol = (snapshot.get("by_symbol") or {}).get(base, {})
        return {"market_breadth": snapshot.get("market_breadth"),
                "correlation_concentration": snapshot.get(
                    "correlation_concentration"),
                **per_symbol}

    def _prepare_harness_shadow(self, base, sig, signal_id, *, allow_veto):
        """Freeze one Harness call and return a zero-argument runner.

        A and B share the Harness runtime and evidence contract, while their
        lifecycle versions remain strategy-scoped.  B always supplies
        ``allow_veto=False`` because it has no execution path.  Freezing the
        account/news/health inputs here lets B defer model network latency until
        the time-critical A universe scan has completed without time leakage.
        """
        model_call = getattr(self, "agent_model_call", None)
        if (not signal_id or not getattr(config, "AGENT_HARNESS_ENABLED", False)
                or not model_call):
            return None
        try:
            from decision.sentiment import latest_sentiment
            from decision.agent_judge import harness_judge
            db_path = getattr(self, "_db_path", None)
            sig_snapshot = deepcopy(sig)
            sentiment = deepcopy(latest_sentiment(db_path=db_path) or {})
            account = {
                "equity_usdt": getattr(self.risk, "equity", None),
                "risk_per_trade": config.RISK_PER_TRADE,
                "max_notional_per_trade_usdt": config.MAX_NOTIONAL_PER_TRADE,
                "portfolio_notional_usdt": self.ledger.total_notional(),
                "max_total_notional_usdt": config.MAX_TOTAL_NOTIONAL,
            }
            health = {
                "risk_can_trade": self.risk.can_trade(),
                "risk_halted": bool(self.risk.halted),
                "risk_halt_reason": self.risk.halt_reason,
            }
        except Exception as exc:
            strategy_id = (sig.get("strategy_id") or
                           config.ENTRY_SIGNAL_STRATEGY_ID)
            print(f"Agent Harness candidate freeze failed "
                  f"{strategy_id}/{base}: {type(exc).__name__}: {exc}")
            return None

        def _run():
            try:
                return harness_judge(
                    sig=sig_snapshot, base=base,
                    score=sig_snapshot.get("shadow_score") or SIGNAL_SCORE,
                    price=sig_snapshot.get("entry"), sentiment=sentiment,
                    model_call=model_call, db_path=db_path,
                    signal_id=signal_id, account=account, health=health,
                    allow_veto=bool(allow_veto))
            except Exception as exc:
                strategy_id = (sig_snapshot.get("strategy_id") or
                               config.ENTRY_SIGNAL_STRATEGY_ID)
                # 影子链故障只留本地证据，不改变量化或执行决策。
                print(f"Agent Harness candidate shadow failed "
                      f"{strategy_id}/{base}: {type(exc).__name__}: {exc}")
                return None

        return _run

    def _run_harness_shadow(self, base, sig, signal_id, *, allow_veto):
        """Evaluate one already-frozen strategy candidate."""
        runner = self._prepare_harness_shadow(
            base, sig, signal_id, allow_veto=allow_veto)
        return runner() if runner is not None else None

    def _scan_strategy_b_shadow(self, base, as_of_ts=None,
                                deferred_harness=None):
        """15m 突破候选进入共同 4h 标签表；仍只 shadow，绝不触达执行链。"""
        if not config.STRATEGY_B_SHADOW_ENABLED:
            return None
        try:
            from engines.strategy_b import (breakout_signal,
                                             enrich_shadow_signal,
                                             record_shadow)
            signal_tf = config.SIGNAL_SAMPLE_TIMEFRAME
            as_of_ts = (time.time() if as_of_ts is None else float(as_of_ts))
            kl_b = self._fetch_klines_any(
                base, signal_tf, config.SIGNAL_LOOKBACK_BARS)
            close_before = (
                (as_of_ts - config.SIGNAL_BAR_CLOSE_GRACE_SECONDS) * 1000 -
                config.SIGNAL_TIMEFRAME_SECONDS[signal_tf] * 1000)
            kl_b = [row for row in (kl_b or []) if int(row[0]) <= close_before]
            if not kl_b:
                return None
            raw = breakout_signal(kl_b)
            if not raw:
                return kl_b
            cross = self._cross_sectional_factors(base, kl_b)
            event_ts = (int(kl_b[-1][0]) +
                        config.SIGNAL_TIMEFRAME_SECONDS[signal_tf] * 1000) / 1000
            closes_4h = None
            try:
                kl4 = self._fetch_klines_any(
                    base, config.SIGNAL_REGIME_TIMEFRAME, 60)
                close_before_4h = (
                    (as_of_ts - config.SIGNAL_BAR_CLOSE_GRACE_SECONDS) * 1000 -
                    config.SIGNAL_TIMEFRAME_SECONDS[
                        config.SIGNAL_REGIME_TIMEFRAME] * 1000)
                kl4 = [row for row in (kl4 or [])
                       if int(row[0]) <= close_before_4h]
                closes_4h = [float(row[4]) for row in (kl4 or [])]
            except Exception:
                closes_4h = None
            funding = funding_change = funding_percentile = None
            try:
                funding = self.exchange.fetch_funding_rate(self._inst_id(base))
                funding_state = getattr(self, "_factor_funding_state", {})
                prior = funding_state.get(base)
                funding_change = (float(funding) - float(prior)
                                  if prior is not None else None)
                values = list(funding_state.values()) + [float(funding)]
                if len(values) >= 3:
                    funding_percentile = (
                        sum(value <= float(funding) for value in values) /
                        len(values))
            except Exception:
                funding = funding_change = funding_percentile = None
            vol5 = None
            try:
                k5 = self._fetch_klines_any(
                    base, "5m", config.FACTOR_5M_LOOKBACK_BARS + 2)
                last_closed_5m_open = (
                    event_ts * 1000 -
                    config.SIGNAL_TIMEFRAME_SECONDS["5m"] * 1000)
                k5 = [row for row in (k5 or [])
                      if int(row[0]) <= last_closed_5m_open]
                vol5 = volatility_5m_features(
                    [row[4] for row in
                     k5[-config.FACTOR_5M_LOOKBACK_BARS - 1:]])
            except Exception:
                vol5 = None
            sig_b = enrich_shadow_signal(
                raw, kl_b, cross=cross, closes_4h=closes_4h,
                funding_rate=funding, funding_change=funding_change,
                funding_percentile=funding_percentile, vol5=vol5,
                event_ts=event_ts)
            # B 与 A/历史重放冻结同一 4h 首触预测。种子只绑定预测算法、
            # 标的、已收线 K 和方向，确保实时留样可与离线重放逐候选核对；
            # 预测仍是 shadow 证据，不改变 B 的拒绝态或执行权限。
            try:
                from decision.forecast import forecast_for_trade
                forecast_window = [
                    {"open": float(row[1]), "high": float(row[2]),
                     "low": float(row[3]), "close": float(row[4]),
                     "volume": float(row[5])}
                    for row in kl_b]
                seed_material = (
                    f"{config.FORECAST_REPLAY_SEED_VERSION}|"
                    f"{self._inst_id(base)}|{int(kl_b[-1][0])}|"
                    f"{sig_b['dir']}")
                seed = int(hashlib.sha256(
                    seed_material.encode()).hexdigest()[:16], 16)
                sig_b["forecast"] = forecast_for_trade(
                    sig_b, base, forecast_window, db_path=self._db_path,
                    as_of_ts=event_ts, seed=seed)
            except Exception:
                sig_b["forecast"] = None
            first_shadow = record_shadow(
                base, config.BREAKOUT_SIGNAL_STRATEGY_ID, sig_b,
                db_path=self._db_path, klines_1h=kl_b)
            from engines.signal_sampling import (record_signal_sample,
                                                 update_signal_decision)
            signal_id, created = record_signal_sample(
                base, sig_b, self.exchange.venue_for(base) or "",
                db_path=self._db_path)
            if created:
                update_signal_decision(
                    signal_id, db_path=self._db_path,
                    rule_decision="shadow", final_decision="rejected",
                    reject_reason="strategy_shadow:B_breakout")
                # B 没有执行路径，但独立 4h 标签可以验证 Harness 是否真能
                # 拦亏。版本与评价按 B_breakout 隔离，固定不授予 veto。
                runner = self._prepare_harness_shadow(
                    base, sig_b, signal_id, allow_veto=False)
                if runner is not None:
                    if deferred_harness is None:
                        runner()
                    else:
                        deferred_harness.append(runner)
            if first_shadow:
                print(f"  👻 影子信号 B_breakout {base} "
                      f"{sig_b['dir']} @ {sig_b['entry']:.4f} "
                      f"(score {sig_b['shadow_score']})")
            return kl_b
        except Exception as exc:
            print(f"{base}: B_breakout 影子留样失败: {type(exc).__name__}")
            return None

    # ---------- 信号：15m 回踩确认，1H/4H 只做环境 ----------
    def scan_signal(self, base, wick_ratio=None, as_of_ts=None,
                    preloaded_kl=None):
        """检查某币的 15m 回踩确认信号。
        多周期共振过滤（MTF）：15m 信号方向必须与 1H/4H 趋势同向，
        只抓高概率时点，不频繁交易。返回信号 dict 或 None。
        wick_ratio: 覆盖影线门槛（扫描影子用候选值）；默认读批准后的活体值。"""
        # 一轮扫描可能持续几十秒并跨过 15m 收线。调用方传入轮次冻结时刻，
        # 保证排在前后的标的都只消费同一个“当时已收线”集合。
        as_of_ts = time.time() if as_of_ts is None else float(as_of_ts)
        try:
            signal_tf = config.SIGNAL_SAMPLE_TIMEFRAME
            kl = (list(preloaded_kl) if preloaded_kl is not None else
                  self._fetch_klines_any(
                      base, signal_tf, config.SIGNAL_LOOKBACK_BARS))
            close_before = ((as_of_ts - config.SIGNAL_BAR_CLOSE_GRACE_SECONDS) *
                            1000 - config.SIGNAL_TIMEFRAME_SECONDS[signal_tf] * 1000)
            kl = [row for row in (kl or []) if int(row[0]) <= close_before]
            if not kl:
                return None
            klines = [{"open": k[1], "high": k[2], "low": k[3], "close": k[4],
                       "volume": k[5]} for k in kl]
        except Exception:
            return None
        if len(klines) < 60:
            return None
        closes = [k["close"] for k in klines]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        atr_val = atr(klines, 14)
        last = klines[-1]
        from decision.scan_evolve import effective_wick_ratio
        ratio = (wick_ratio if wick_ratio is not None
                 else effective_wick_ratio(getattr(self, "_db_path", None)))
        # 绝大多数标的没有 15m 回踩结构。先用主周期做无副作用预检，
        # 未命中时不再串行拉 1H/4H、ticker 与 forecast；真正命中后仍走
        # 完整 MTF/特征/风控链，信号语义不变但后排候选等待显著缩短。
        if not detect_pullback_setup(
                last, ema20[-1], ema50[-1], ratio, mtf_enabled=False):
            return None
        # 1H/4H 只是上下文；开启 MTF 时才作硬过滤。
        tf1h_trend = 0
        tf4h_trend = 0   # 1=多, -1=空, 0=未知（数据不足时视为无共振，放弃信号）
        c1 = []
        c4 = []
        try:
            context_tf = config.SIGNAL_CONTEXT_TIMEFRAME
            kl1 = self._fetch_klines_any(base, context_tf, 60)
            close_before_1h = (
                (as_of_ts - config.SIGNAL_BAR_CLOSE_GRACE_SECONDS) * 1000 -
                config.SIGNAL_TIMEFRAME_SECONDS[context_tf] * 1000)
            kl1 = [row for row in (kl1 or []) if int(row[0]) <= close_before_1h]
            if kl1:
                c1 = [row[4] for row in kl1]
                if len(c1) >= 50:
                    e20_1h, e50_1h = ema(c1, 20), ema(c1, 50)
                    tf1h_trend = 1 if e20_1h[-1] > e50_1h[-1] else -1
        except Exception:
            tf1h_trend = 0
        try:
            regime_tf = config.SIGNAL_REGIME_TIMEFRAME
            kl4 = self._fetch_klines_any(base, regime_tf, 60)
            close_before_4h = ((as_of_ts - config.SIGNAL_BAR_CLOSE_GRACE_SECONDS) *
                               1000 - config.SIGNAL_TIMEFRAME_SECONDS[regime_tf] * 1000)
            kl4 = [row for row in (kl4 or []) if int(row[0]) <= close_before_4h]
            if kl4:
                c4 = [k[4] for k in kl4]
                if len(c4) >= 50:
                    e20, e50 = ema(c4, 20), ema(c4, 50)
                    tf4h_trend = 1 if e20[-1] > e50[-1] else -1
        except Exception:
            tf4h_trend = 0
        setup = detect_pullback_setup(
            last, ema20[-1], ema50[-1], ratio, tf1h_trend, tf4h_trend,
            MTF_ENABLED)
        if not setup:
            return None
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        upper_wick = last["high"] - max(last["open"], last["close"])
        # 日内入场参考价用实时 tick 价，趋势/ATR 来自已收线 15m K。
        entry_ref = self._ticker_last(base)
        if entry_ref is None:
            entry_ref = last["close"]

        def _shadow(price_near_ema, wick, direction):
            """影子连续分(0-100): 6 维加权(2026-08-23 用户指示'维度太少了,加')。
            量能/资金费/盘口三个新维度只在信号命中时惰性拉取(信号稀疏,成本低);
            取不到→该维中性 0.5,不污染总分。"""
            try:
                vol_last = last.get("volume") or 0
                vols = [k.get("volume") or 0
                        for k in klines[-config.SHADOW_VOL_LOOKBACK - 1:-1]]
                vol_avg = sum(vols) / len(vols) if vols else 0
                funding = None
                book_imb = None
                _book = None
                oi = basis = None
                try:
                    funding = self.exchange.fetch_funding_rate(
                        self._inst_id(base))
                except Exception:
                    pass
                try:
                    _book = self.exchange.fetch_order_book(
                        self._inst_id(base), config.SHADOW_BOOK_DEPTH)
                    book_imb = _book_imbalance(_book, config.SHADOW_BOOK_DEPTH)
                except Exception:
                    pass
                try:
                    oi = self.exchange.fetch_open_interest(self._inst_id(base))
                except Exception:
                    pass
                try:
                    basis = self.exchange.fetch_basis(self._inst_id(base))
                except Exception:
                    pass
                from decision.weight_evolve import effective_weights
                score, dims = compute_shadow_score(
                    wick, body, price_near_ema, ema20[-1], ema50[-1], atr_val,
                    vol_last, vol_avg, funding, book_imb, direction,
                    weights=effective_weights(getattr(self, "_db_path", None)))
                reg = None
                try:
                    from engines.feature_collector import compute_regime
                    reg = compute_regime(klines, c4)
                except Exception:
                    reg = None
                closes_recent = [k["close"] for k in klines[-25:]]
                returns = [math.log(closes_recent[i] / closes_recent[i - 1])
                           for i in range(1, len(closes_recent))
                           if closes_recent[i] > 0 and closes_recent[i - 1] > 0]
                returns_1h = returns[-4:]
                rv = (math.sqrt(sum(value * value for value in returns_1h))
                      if len(returns_1h) == 4 else None)
                down = (math.sqrt(sum(value * value for value in returns_1h
                                      if value < 0))
                        if len(returns_1h) == 4 else None)
                micro = _microstructure_features(_book, config.SHADOW_BOOK_DEPTH)
                event_flow = {}
                try:
                    get_orderflow = getattr(self.rt, "get_orderflow", None)
                    if get_orderflow:
                        event_flow = get_orderflow(base) or {}
                except Exception:
                    event_flow = {}
                book_state = getattr(self, "_factor_book_state", {})
                previous_book = book_state.get(base)
                ofi, current_book = _dynamic_ofi(_book, previous_book)
                cancel_imbalance = _cancellation_imbalance(
                    current_book, previous_book) if current_book else None
                if current_book:
                    book_state[base] = current_book
                    self._factor_book_state = book_state
                oi_state = getattr(self, "_factor_oi_state", {})
                previous_oi = oi_state.get(base)
                oi_change = ((oi - previous_oi) / previous_oi
                             if oi is not None and previous_oi else None)
                if oi is not None:
                    oi_state[base] = oi
                    self._factor_oi_state = oi_state

                vol5 = {"realized_vol_5m": None, "vol_of_vol": None,
                        "har_rv": None}
                try:
                    k5 = self._fetch_klines_any(
                        base, "5m", config.FACTOR_5M_LOOKBACK_BARS + 2)
                    event_ms = (int(kl[-1][0]) +
                                config.SIGNAL_TIMEFRAME_SECONDS[
                                    config.SIGNAL_SAMPLE_TIMEFRAME] * 1000)
                    last_closed_5m_open = (
                        event_ms - config.SIGNAL_TIMEFRAME_SECONDS["5m"] * 1000)
                    k5 = [row for row in (k5 or [])
                          if int(row[0]) <= last_closed_5m_open]
                    vol5 = volatility_5m_features(
                        [row[4] for row in
                         k5[-config.FACTOR_5M_LOOKBACK_BARS - 1:]])
                except Exception:
                    pass

                cross = {}
                try:
                    cross = self._cross_sectional_factors(base, kl)
                except Exception:
                    pass
                funding_state = getattr(self, "_factor_funding_state", {})
                prior_funding = funding_state.get(base)
                funding_change = (float(funding) - float(prior_funding)
                                  if funding is not None and
                                  prior_funding is not None else None)
                if funding is not None:
                    funding_state[base] = float(funding)
                    self._factor_funding_state = funding_state
                funding_values = sorted(funding_state.values())
                funding_percentile = (
                    sum(value <= float(funding) for value in funding_values) /
                    len(funding_values) if funding is not None and
                    len(funding_values) >= 3 else None)
                k_ts = int(kl[-1][0]) / 1000 if kl else time.time()
                tm = time.gmtime(k_ts)
                momentum_1h = sum(returns[-4:]) if len(returns) >= 4 else None
                momentum_4h = sum(returns[-16:]) if len(returns) >= 16 else None
                factor_features = {
                    "wick_ratio": wick / body if body > 0 else None,
                    "pullback_depth_atr": (abs(price_near_ema - ema20[-1]) / atr_val
                                           if atr_val else None),
                    "trend_band_atr": ((ema20[-1] - ema50[-1]) / atr_val
                                       if atr_val else None),
                    "volume_ratio": vol_last / vol_avg if vol_avg else None,
                    "funding_rate": funding, "book_imbalance": book_imb,
                    "realized_vol_1h": rv,
                    "realized_vol_5m": vol5.get("realized_vol_5m"),
                    "downside_semivol_1h": down,
                    "vol_of_vol": vol5.get("vol_of_vol"),
                    "har_rv": vol5.get("har_rv"),
                    "atr_pct": atr_val / entry_ref if entry_ref else None,
                    "hour_sin": math.sin(2 * math.pi * tm.tm_hour / 24),
                    "hour_cos": math.cos(2 * math.pi * tm.tm_hour / 24),
                    "weekend": 1.0 if tm.tm_wday >= 5 else 0.0,
                    "ofi_dynamic": ofi, "cancel_imbalance": cancel_imbalance,
                    "ofi_event_multilevel": event_flow.get(
                        "ofi_event_multilevel"),
                    "ofi_event_cancel_imbalance": event_flow.get(
                        "ofi_event_cancel_imbalance"),
                    "ofi_event_count": event_flow.get("ofi_event_count", 0),
                    "ofi_event_age_ms": event_flow.get("ofi_event_age_ms"),
                    "open_interest_change": oi_change,
                    "oi_price_interaction": (oi_change * momentum_1h
                                             if oi_change is not None and
                                             momentum_1h is not None else None),
                    "basis": basis,
                    "btc_residual_momentum": cross.get(
                        "btc_residual_momentum"),
                    "btc_beta": cross.get("btc_beta"),
                    "momentum_1h": momentum_1h,
                    "momentum_4h": momentum_4h,
                    "cross_sectional_rank": cross.get(
                        "cross_sectional_rank"),
                    "funding_change": funding_change,
                    "funding_percentile": funding_percentile,
                    "market_breadth": cross.get("market_breadth"),
                    "correlation_concentration": cross.get(
                        "correlation_concentration"),
                    "source_latency_ms": max(
                        0.0, time.time() * 1000 - (
                            int(kl[-1][0]) +
                            config.SIGNAL_TIMEFRAME_SECONDS[
                                config.SIGNAL_SAMPLE_TIMEFRAME] * 1000)),
                    **micro,
                }
                factor_features.update(technical_regime_features(klines))
                factor_features = materialize_derived_features(
                    factor_features, dims)
                factor_features["feature_missing_rate"] = (
                    sum(value is None for value in factor_features.values()) /
                    len(factor_features))
                market_regime = classify_market_regime(reg, factor_features)
                strategy_route = route_strategy(
                    market_regime,
                    available=config.MARKET_REGIME_IMPLEMENTED_STRATEGIES)
                reg = dict(reg or {})
                reg["market_state"] = market_regime
                reg["strategy_route"] = strategy_route
                return score, dims, reg, factor_features
            except Exception:
                return None, None, None, {}

        # 2026-08-23 目标价位带: 近20根摆动结构位(不含当前未收线K)
        _swing = None
        try:
            if len(klines) >= 21:
                _swing = (max(k["high"] for k in klines[-21:-1])
                          if ema20[-1] > ema50[-1]
                          else min(k["low"] for k in klines[-21:-1]))
        except Exception:
            _swing = None
        _direction = "long" if ema20[-1] > ema50[-1] else "short"
        _targets = compute_targets(entry_ref, atr_val, _direction,
                                   swing_level=_swing)
        # 2026-08-23 预测机制(用户要求"最好能有预测机制"): bootstrap 价格分布
        # + 触达概率,复用本函数已取的 klines,零额外网络成本
        _forecast = None
        try:
            from decision.forecast import forecast_for_trade
            _fc_sig = {"dir": _direction, "entry": entry_ref, "atr": atr_val,
                       "stop": (entry_ref - config.STOP_ATR_MULT * atr_val
                                if _direction == "long" else
                                entry_ref + config.STOP_ATR_MULT * atr_val),
                       "tp": (entry_ref + config.TP_ATR_MULT * atr_val
                              if _direction == "long" else
                              entry_ref - config.TP_ATR_MULT * atr_val)}
            _forecast = forecast_for_trade(
                _fc_sig, base, klines, db_path=getattr(self, "_db_path", None),
                as_of_ts=as_of_ts)
        except Exception:
            _forecast = None
        kline_ts = last.get("ts") if isinstance(last, dict) else None
        # last 来自 klines dict 无 ts；用原始 kl 最后一根
        if kline_ts is None and kl:
            kline_ts = kl[-1][0]

        direction = setup["direction"]
        score, dims, regime, factors = _shadow(
            setup["touch"], setup["wick"], direction)
        return {"dir": direction, "entry": entry_ref,
                "stop": (entry_ref - config.STOP_ATR_MULT * atr_val
                         if direction == "long" else
                         entry_ref + config.STOP_ATR_MULT * atr_val),
                "tp": (entry_ref + config.TP_ATR_MULT * atr_val
                       if direction == "long" else
                       entry_ref - config.TP_ATR_MULT * atr_val),
                "atr": atr_val,
                "shadow_score": score, "shadow_dims": dims,
                "factor_features": factors,
                "targets": _targets, "forecast": _forecast,
                "regime": regime,
                "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
                "market_regime": (regime or {}).get("market_state"),
                "strategy_route": (regime or {}).get("strategy_route"),
                "kline_ts": kline_ts}

    # ---------- 主循环 ----------
    def _trade_budget(self, base):
        """该币今日允许笔数：按当日扫描评分动态给（用户要求：看币动态调整笔数）。"""
        from engines.daily_scan import trades_budget
        return trades_budget(self.watch_scores.get(base))

    def _run_agent_proposal_shadow(self, scan_pool, as_of_ts=None):
        """每根 15m K 一次批量主动提案；只留样，不进入开仓分支。"""
        model_call = getattr(self, "agent_proposal_model_call", None)
        if (not config.AGENT_PROPOSAL_SHADOW_ENABLED or
                getattr(self, "live_mode", False) or model_call is None):
            return None
        try:
            from decision.agent_proposals import (build_market_snapshot,
                                                  run_proposal_cycle)
            ranked = sorted(
                dict.fromkeys(scan_pool),
                key=lambda symbol: (-float(self.watch_scores.get(symbol, -1)),
                                    symbol))
            snapshots = []
            now_ms = (time.time() if as_of_ts is None else
                      float(as_of_ts)) * 1000
            for base in ranked[:config.AGENT_PROPOSAL_MAX_SYMBOLS]:
                progress = getattr(self, "_long_scan_progress", None)
                if callable(progress):
                    progress()
                frames = {}
                for timeframe in (config.SIGNAL_SAMPLE_TIMEFRAME,
                                  config.SIGNAL_CONTEXT_TIMEFRAME,
                                  config.SIGNAL_REGIME_TIMEFRAME):
                    rows = self._fetch_klines_any(
                        base, timeframe,
                        config.AGENT_PROPOSAL_MIN_BARS + 2)
                    close_before = (
                        now_ms - config.SIGNAL_BAR_CLOSE_GRACE_SECONDS * 1000 -
                        config.SIGNAL_TIMEFRAME_SECONDS[timeframe] * 1000)
                    frames[timeframe] = [
                        row for row in (rows or []) if int(row[0]) <= close_before]
                try:
                    inst_id_fn = getattr(self, "_inst_id", None)
                    inst_id = (inst_id_fn(base) if callable(inst_id_fn)
                               else f"{base}-USDT-SWAP")
                    market_features = {}
                    funding = book = oi = basis = None
                    try:
                        funding = self.exchange.fetch_funding_rate(inst_id)
                    except Exception:
                        pass
                    try:
                        book = self.exchange.fetch_order_book(
                            inst_id, config.SHADOW_BOOK_DEPTH)
                    except Exception:
                        pass
                    try:
                        oi = self.exchange.fetch_open_interest(inst_id)
                    except Exception:
                        pass
                    try:
                        basis = self.exchange.fetch_basis(inst_id)
                    except Exception:
                        pass
                    market_features.update(_microstructure_features(
                        book, config.SHADOW_BOOK_DEPTH))
                    market_features.update({
                        "funding_rate": funding,
                        "book_imbalance": _book_imbalance(
                            book, config.SHADOW_BOOK_DEPTH),
                        "basis": basis,
                    })
                    book_state = getattr(self, "_proposal_book_state", {})
                    previous_book = book_state.get(base)
                    ofi, current_book = _dynamic_ofi(book, previous_book)
                    cancel_imbalance = (_cancellation_imbalance(
                        current_book, previous_book) if current_book else None)
                    if current_book:
                        book_state[base] = current_book
                        self._proposal_book_state = book_state
                    oi_state = getattr(self, "_proposal_oi_state", {})
                    previous_oi = oi_state.get(base)
                    oi_change = ((float(oi) - float(previous_oi)) /
                                 float(previous_oi)
                                 if oi is not None and previous_oi else None)
                    if oi is not None:
                        oi_state[base] = float(oi)
                        self._proposal_oi_state = oi_state
                    event_flow = {}
                    try:
                        get_orderflow = getattr(self.rt, "get_orderflow", None)
                        if get_orderflow:
                            event_flow = get_orderflow(base) or {}
                    except Exception:
                        pass
                    market_features.update({
                        "ofi_dynamic": ofi,
                        "cancel_imbalance": cancel_imbalance,
                        "open_interest_change": oi_change,
                        "ofi_event_multilevel": event_flow.get(
                            "ofi_event_multilevel"),
                        "ofi_event_cancel_imbalance": event_flow.get(
                            "ofi_event_cancel_imbalance"),
                        "ofi_event_count": event_flow.get("ofi_event_count"),
                        "ofi_event_age_ms": event_flow.get("ofi_event_age_ms"),
                    })
                    market_snapshot_ts = int(time.time() * 1000)
                    snapshots.append(build_market_snapshot(
                        base, frames[config.SIGNAL_SAMPLE_TIMEFRAME],
                        frames[config.SIGNAL_CONTEXT_TIMEFRAME],
                        frames[config.SIGNAL_REGIME_TIMEFRAME],
                        market_features=market_features,
                        market_snapshot_ts=market_snapshot_ts))
                except ValueError:
                    continue
            if not snapshots:
                return None
            from engines.signal_sampling import record_agent_proposal_sample
            result = run_proposal_cycle(
                snapshots, model_call=model_call,
                sample_recorder=lambda **kwargs: record_agent_proposal_sample(
                    db_path=self._db_path, **kwargs),
                db_path=self._db_path)
            if result.get("proposals") and not result.get("deduplicated"):
                valid = sum(row.get("geometry_valid") == 1
                            for row in result["proposals"] if row)
                print(f"Agent主动提案 shadow: {len(result['proposals'])} 条，"
                      f"确定性2:1有效 {valid} 条，无执行权限")
            return result
        except Exception as exc:
            print(f"Agent主动提案 shadow failed: {type(exc).__name__}: {exc}")
            return None

    def scan_signals(self):
        """扫一轮候选池信号（每 15 分钟，日内短线）。
        频率约束（用户要求：看币动态调整笔数）：每个币每天的允许笔数按其当日
        评分动态给（评分越高越值得多给机会）+ 同币信号冷却 SIGNAL_COOLDOWN_MINUTES。"""
        # 每日刷新候选池（跨天自动重扫全市场）——screen_daily 耗时 1-2 分钟,
        # 期间心跳停更会被 watchdog 误杀:先刷一次心跳再进阻塞段(2026-08-16 事故)。
        if time.time() - self._last_watch_refresh >= 24 * 3600 or \
                time.strftime("%Y-%m-%d") != getattr(self, "_watch_date", ""):
            try:
                from execution.pidfile import write_heartbeat
                write_heartbeat("directional")
                from engines.daily_scan import screen_daily
                # 2026-08-17: 全市场筛选同样逐币插拍监控/心跳/tick——网络慢时
                # 60 币筛选会阻塞主循环数十分钟(今晚 23:08 复现: tick 卡 5 分钟
                # 停更,H9 报警),与 51 分钟盲窗同源。回调 = 每币一次。
                w = screen_daily(progress_cb=self._long_scan_progress,
                                 exchange=self.exchange, db_path=self._db_path)
                if w:
                    self.crypto_watchlist = [c["base"] for c in w
                                             if not c.get("is_stock")]
                    self.stock_watchlist = [c["base"] for c in w
                                            if c.get("is_stock")]
                    self.watchlist = self.crypto_watchlist + self.stock_watchlist
                    self.watch_scores = {c["base"]: c["score"] for c in w}
                    self._watch_date = time.strftime("%Y-%m-%d")
                    self._last_watch_refresh = time.time()
                    self._notify(
                        "🔍 每日候选池刷新\n"
                        + "加密：" + (" · ".join(self.crypto_watchlist) or "空")
                        + "\n美股：" + (" · ".join(self.stock_watchlist) or "空")
                        + f"\n合计 {len(self.watchlist)} 个")
            except Exception as e:
                print(f"候选池刷新失败，沿用旧池: {e}")
        # 2026-08-16 采集加速（用户指示）：扫描池 = 当日候选池 ∪ 回退主流池
        # （10 个主流币始终参与信号扫描,额度/冷却约束照常适用）
        scan_pool = list(dict.fromkeys(
            self.watchlist + [s for s in SYMBOLS if s not in self.watchlist]))
        # 2026-08-20: 黑名单币不进信号扫描(旧 watchlist 残留或回退池误入时
        # 省掉 K 线请求;名额过滤在 daily_scan,这里是第二道)。
        from engines.daily_scan import untradable_bases
        _blocked = untradable_bases(self._db_path)
        if _blocked:
            scan_pool = [b for b in scan_pool if b not in _blocked]
        # 冻结整轮的 K 线可见截止点。盘口/现价仍按各标的实际检查时刻读取，
        # 但 15m/1H/4H 形态不能因顺序和网络耗时跨入下一根 K。
        scan_as_of_ts = time.time()
        deferred_b_harness = []
        n_from_watch = sum(1 for b in scan_pool if b in self.watchlist)
        print(f"\n=== 方向性信号扫描 [{time.strftime('%H:%M:%S')}] "
              f"加密候选 {len(getattr(self, 'crypto_watchlist', []))} 个 + "
              f"美股候选 {len(getattr(self, 'stock_watchlist', []))} 个 + "
              f"回退池 {len(scan_pool) - n_from_watch} 个 ===")
        if config.SCAN_EVOLVE_ENABLED:
            try:
                from decision.scan_evolve import tick as scan_evolve_tick
                scan_evolve_tick(self)
            except Exception as e:
                print(f"扫描进化步进异常(不影响扫描): {e}")
        today = time.strftime("%Y-%m-%d")
        # 2026-08-20 交易所故障退避: 下单遇 50001/503 后暂停开仓 N 秒,
        # 避免故障期间每轮扫描都刷失败行/告警(OKX 沙盘全灭案例)。
        # 逐币实时读(不能轮前快照——同轮首个币失败后,后续币还会再试)。
        # 2026-08-23 用户指示: 连亏 6 笔主动冷却——期间整轮不接信号(记一条决策)
        try:
            from decision.loss_cooling import is_cooling, cooling_remaining_hours
            _dbp = getattr(self, "_db_path", None)
            if is_cooling(_dbp):
                print(f"🧊 连亏冷却中(剩 {cooling_remaining_hours(_dbp):.1f}h),"
                      f"本轮扫描不接新信号")
                self._log_scan_decision("", False, "", "loss_cooling",
                                        f"连亏冷却中(剩 {cooling_remaining_hours(_dbp):.1f}h)")
                return
        except Exception:
            pass
        for base in scan_pool:
            if time.time() < getattr(self, "_open_backoff_until", 0):
                self._log_scan_decision(base, False, "", "exchange_backoff",
                                        "交易所下单 API 故障,退避中")
                continue
            # 2026-08-16: 长扫描期间每币刷新心跳——18 币扫描需数分钟,
            # 心跳停更 >30s 会被 watchdog 误杀（exit -15 崩溃循环事故）。
            # 2026-08-17 事故: 网络黑洞让 20 币扫描 × 30s 超时阻塞主循环 51 分钟,
            # 期间 tick() 无法执行 → 止损监控失明。逐币插拍(监控+心跳+tick+
            # 60s 仓位快照): 盲窗≤单币网络超时,慢速但有进展的扫描不算卡死。
            self._long_scan_progress()
            # A/B 必须在任何 A 的额度、冷却、模型或 AI continue 之前各自留样；
            # 否则“先判断行情再选策略”的比较集仍会带选择偏差。
            kl_b = self._scan_strategy_b_shadow(
                base, as_of_ts=scan_as_of_ts,
                deferred_harness=deferred_b_harness)
            # 先形成结构候选，再做任何额度/冷却/规则/AI 门控。此前先检查
            # 额度与冷却会让被拒候选永久缺失，反事实样本带选择偏差。
            sig = self.scan_signal(
                base, as_of_ts=scan_as_of_ts, preloaded_kl=kl_b)
            if sig:
                signal_id = None
                try:
                    from engines.signal_sampling import record_signal_sample
                    signal_id, created = record_signal_sample(
                        base, sig, self.exchange.venue_for(base) or "",
                        db_path=self._db_path)
                    if not created:
                        # 同币/方向/已收线 15m K/策略版本只允许一次机会；
                        # 5 分钟轮询不得把同一根 K 重复计数或重复开仓。
                        self._log_scan_decision(
                            base, True, sig["dir"], "duplicate_signal",
                            f"同K候选已处理: {signal_id}")
                        continue
                except Exception as e:
                    # 留样能力故障不改变现役规则/风控行为；明确记录异常，待
                    # 运维修复，不能因为研究链故障扩大或缩小交易权限。
                    print(f"{base}: 候选留样失败，沿用现役规则: {e}")

                entry_prediction = None
                rr_prediction = None
                extrema_prediction = None
                if signal_id:
                    try:
                        from decision.entry_probability import predict_signal
                        from engines.signal_sampling import merge_sample_features
                        entry_prediction = predict_signal(
                            sig, db_path=self._db_path, allow_shadow=True)
                        if entry_prediction:
                            merge_sample_features(
                                signal_id, {"entry_probability": entry_prediction},
                                db_path=self._db_path)
                    except Exception:
                        entry_prediction = None
                    try:
                        from decision.extrema_forecast import predict_signal as \
                            predict_extrema
                        from engines.signal_sampling import merge_sample_features
                        extrema_prediction = predict_extrema(
                            sig, db_path=self._db_path, allow_shadow=True)
                        if extrema_prediction:
                            merge_sample_features(
                                signal_id, {"extrema_prediction": extrema_prediction},
                                db_path=self._db_path)
                            # 通知复用同一概率区间；没有 bootstrap forecast 时不造
                            # 半截 forecast，完整影子输出仍保存在候选快照中。
                            if sig.get("forecast"):
                                sig["forecast"]["extrema"] = extrema_prediction
                    except Exception:
                        extrema_prediction = None

                # 固定 1R:2R 的开仓前预测审计。严格放行只在真实 OKX 模拟盘
                # 由 self.require_2to1_prediction 开启；所有候选（含拒绝）仍会
                # 留样并在 4h 后结算，供模型继续训练、校准与晋升。
                try:
                    from decision.entry_probability import preopen_2to1_decision
                    rr_prediction = preopen_2to1_decision(
                        sig, prediction=None, db_path=self._db_path)
                    if signal_id:
                        from engines.signal_sampling import merge_sample_features
                        merge_sample_features(
                            signal_id, {"preopen_2to1": rr_prediction},
                            db_path=self._db_path)
                except Exception as e:
                    rr_prediction = {"passed": False,
                                     "reason": f"prediction_error:{type(e).__name__}"}

                # Harness 是独立增量研究，不得被现役 2:1 模型门卡死。对每个
                # 去重结构候选先留 shadow Trace；即使随后因额度、分数或无
                # active 概率模型被拒，4h 路径仍能成熟为 Agent 反事实样本。
                # 只有同一完整 Harness 版本通过验证门并进入 active-veto 后，结果
                # 才会在全部量化硬门通过后作为额外否决消费；永远不能放行。
                _harness_result = self._run_harness_shadow(
                    base, sig, signal_id,
                    # 仓库授权边界：Harness veto 只允许 OKX 模拟盘；
                    # live 即使加载相同配置与版本也固定保持 shadow。
                    allow_veto=not getattr(self, "live_mode", False))

                def _sample_decision(**kwargs):
                    if not signal_id:
                        return
                    try:
                        from engines.signal_sampling import update_signal_decision
                        update_signal_decision(signal_id, db_path=self._db_path,
                                               **kwargs)
                    except Exception:
                        pass

                # 额度/冷却命中仍保留候选，后续按 4h 路径结算反事实结果。
                opened_base = [t for t in self.journal.trades
                               if t.get("symbol") == base and t.get("entry_time")
                               and time.strftime("%Y-%m-%d", time.localtime(t["entry_time"])) == today]
                budget = self._trade_budget(base)
                if len(opened_base) >= budget:
                    _reason = f"今日已开 {len(opened_base)} 笔 ≥ 额度 {budget}"
                    print(f"⏸️ {base}: {_reason}（评分给额），跳过")
                    self._log_scan_decision(base, True, sig["dir"], "budget", _reason)
                    _sample_decision(rule_decision="reject",
                                     final_decision="rejected",
                                     reject_reason="budget: " + _reason)
                    continue
                if time.time() - self.signal_cool.get(base, 0) < SIGNAL_COOLDOWN_MINUTES * 60:
                    self._log_scan_decision(base, True, sig["dir"],
                                            "cooldown", "信号冷却中")
                    _sample_decision(rule_decision="reject",
                                     final_decision="rejected",
                                     reject_reason="cooldown")
                    continue
                self.signal_cool[base] = time.time()
                # 阈值决策（审计 CR-6 + Phase3 T3.1）：默认用常量 SIGNAL_SCORE 卡门槛
                # （影子分未过假设 A3 检验前不得影响决策——防过拟合红线）；
                # FLAG_USE_SHADOW_SCORE_GATE=True 在 A3 通过后由人工开启。
                gate_score = SIGNAL_SCORE
                if FLAG_USE_SHADOW_SCORE_GATE:
                    gate_score = sig.get("shadow_score")
                    if gate_score is None:
                        gate_score = SIGNAL_SCORE
                # 2026-08-23 用户指示"实盘阈值上调到40": 实盘按真实信号分
                # (shadow_score 0-100)卡 effective_threshold(≥40),只做强信号;
                # 模拟盘保持激进,原逻辑不变(SIGNAL_SCORE 平级卡学习器阈值)。
                if getattr(self, "live_mode", False):
                    gate_score = sig.get("shadow_score") or SIGNAL_SCORE
                _thr = self.effective_threshold()
                if gate_score < _thr:
                    print(f"{base}: 信号分 {gate_score} < 决策阈值 {_thr}，观望")
                    self._log_scan_decision(base, True, sig["dir"], "reject",
                                            f"信号分 {gate_score} < 阈值 {_thr}")
                    _sample_decision(rule_decision="reject",
                                     final_decision="rejected",
                                     reject_reason=f"score_gate:{gate_score}<{_thr}")
                    continue
                if (getattr(self, "require_2to1_prediction", False) and
                        not (rr_prediction or {}).get("passed")):
                    _reason = "2to1_prediction:" + (
                        (rr_prediction or {}).get("reason") or "missing")
                    self._log_scan_decision(base, True, sig["dir"],
                                            "model_reject", _reason)
                    _sample_decision(rule_decision="reject",
                                     final_decision="rejected",
                                     reject_reason=_reason)
                    continue
                # 决策（经验库，统一 ScoredExperience — B6）
                dec = self.evolver.decide(base, SIGNAL_SCORE, "回踩确认", 0, 0, 0.02, 0.05, 0,
                                          journal=self.journal,
                                          conditions=_build_trade_conditions(sig))
                if dec["trade"]:
                    # 只有 active/observing/kept 模型可作为 meta-label 否决；
                    # candidate/validated/shadow 只留预测，不改变现役开仓行为。
                    if (entry_prediction and entry_prediction.get("decision_effective")
                            and entry_prediction.get("ev_r_lower", 0) <= 0):
                        _reason = (f"entry_model EV下界 "
                                   f"{entry_prediction['ev_r_lower']:.3f}R ≤ 0")
                        self._log_scan_decision(base, True, sig["dir"],
                                                "model_reject", _reason)
                        _sample_decision(rule_decision="reject",
                                         final_decision="rejected",
                                         reject_reason=_reason)
                        continue
                    # Harness 只能在量化、2:1、模型和经验门都已放行后额外否决。
                    # 生命周期未达 active-veto 时 policy.veto 恒为 False。
                    if (_harness_result is not None and
                            _harness_result.policy.veto):
                        _reason = _harness_result.policy.reason or "validated veto"
                        self._log_scan_decision(
                            base, True, sig["dir"], "harness_reject", _reason)
                        _sample_decision(
                            rule_decision="pass", ai_verdict="reject",
                            final_decision="rejected",
                            reject_reason="harness_reject: " + _reason)
                        continue
                    # 2026-08-23 AI 把关(用户问"agent也会加入判断吗"): 下单前
                    # DeepSeek 二判。只否决不放行——reject 才拦,其余一律继续。
                    verdict = "approve"
                    ai_reason = ""
                    ai_jid = None
                    if getattr(self, "ai_judge_enabled",
                               getattr(config, "AGENT_JUDGE_ENABLED", False)):
                        try:
                            from decision.sentiment import latest_sentiment
                            _sent = latest_sentiment(
                                db_path=getattr(self, "_db_path", None))
                            from decision.agent_judge import judge
                            verdict, ai_reason, ai_jid = judge(
                                sig=sig, base=base,
                                score=sig.get("shadow_score") or SIGNAL_SCORE,
                                price=sig.get("entry"), sentiment=_sent,
                                db_path=getattr(self, "_db_path", None),
                                signal_id=signal_id)
                        except Exception:
                            verdict = "approve"   # AI 异常 → 放行
                    if verdict == "reject":
                        print(f"🤖 AI 把关否决 {base}: {ai_reason}")
                        self._log_scan_decision(base, True, sig["dir"],
                                                "ai_reject", ai_reason)
                        try:
                            self._notify(f"🤖 AI 把关否决 {base} "
                                         f"{self._dir_cn(sig['dir'])}: {ai_reason}")
                        except Exception:
                            pass
                        _sample_decision(rule_decision="pass", ai_verdict="reject",
                                         final_decision="rejected",
                                         reject_reason="ai_reject: " + ai_reason)
                        continue
                    _sample_decision(rule_decision="pass", ai_verdict=verdict)
                    # 2026-08-20: 先下单,成交入账后才记 open。此前先记 open 再
                    # 调 open_position,下单失败(51001 等)会虚增"开仓"——看账
                    # 开仓 159 vs 台账 24 笔(ALLO 当天即复现)。
                    reason = "; ".join(dec.get("reason") or ["信号达标"])
                    tid = self.open_position(
                        base, sig,
                        score=sig.get("shadow_score") or SIGNAL_SCORE,
                        stop_adj=dec.get("stop_adj", 0.0),
                        size_factor=dec.get("size_factor", 1.0),
                        adopted_ids=dec.get("adopted_lesson_ids", []))
                    if tid:
                        try:
                            from decision.agent_judge import bind_trade
                            bind_trade(ai_jid, tid,
                                       db_path=getattr(self, "_db_path", None))
                        except Exception:
                            pass
                        self._log_scan_decision(base, True, sig["dir"], "open",
                                                reason)
                        _sample_decision(final_decision="opened", trade_id=tid)
                    else:
                        _sample_decision(final_decision="open_failed",
                                         reject_reason="execution_failed")
                    # 未成交: open_position 已记 reject_* / open_failed,此处不补 open
                else:
                    print(f"{base}: 有信号但拒绝 - {'; '.join(dec['reason'])}")
                    self._log_scan_decision(base, True, sig["dir"], "reject",
                                            "; ".join(dec["reason"]))
                    _sample_decision(rule_decision="reject",
                                     final_decision="rejected",
                                     reject_reason="evolver: " + "; ".join(dec["reason"]))
            else:
                print(f"{base}: 无回踩确认信号")
                self._log_scan_decision(base, False, "", "no_signal", "")
                self._maybe_wick_shadow(
                    base, as_of_ts=scan_as_of_ts, preloaded_kl=kl_b)
                # 未触发 A 时复盘 B 的瓶颈；复用本轮已收线 15m 数据，不再拉 1H。
                if kl_b:
                    try:
                        from engines.strategy_b import (profile_from_klines,
                                                         record_profile)
                        prof = profile_from_klines(kl_b, db_path=self._db_path)
                        if prof:
                            record_profile(base, prof, db_path=self._db_path)
                    except Exception:
                        pass

        # B 的候选、账户、新闻和健康上下文已在各自发现时冻结；纯影子
        # 模型调用统一移到 A 全池扫描之后，不能阻塞任何潜在执行候选。
        for run_b_harness in deferred_b_harness:
            self._long_scan_progress()
            run_b_harness()

        # 与 A/B 量化候选完全分账。无论 AI 是否提案，本轮都不会调用
        # open_position；有效提案只等待既有 4h 完整路径反事实结算。
        self._run_agent_proposal_shadow(
            scan_pool, as_of_ts=scan_as_of_ts)

    def _maybe_wick_shadow(self, base, as_of_ts=None, preloaded_kl=None):
        """现役没信号时用候选影线比再扫一次；命中只记影子，绝不下单/不占冷却。"""
        if not config.SCAN_EVOLVE_ENABLED:
            return
        try:
            from decision.scan_evolve import active_candidate
            from engines.strategy_b import record_shadow
            cand = active_candidate(self._db_path)
            if not cand:
                return
            sig = self.scan_signal(
                base, wick_ratio=cand["wick"], as_of_ts=as_of_ts,
                preloaded_kl=preloaded_kl)
            if not sig:
                return
            if record_shadow(base, config.SCAN_EVOLVE_STRATEGY, sig,
                             db_path=self._db_path):
                print(f"  👻 扫描影子 A_wick {base} {sig['dir']} "
                      f"@ {sig['entry']:.4f}（候选影线比 {cand['wick']}，不下单）")
        except Exception:
            pass

    def _is_auto_untradable(self, base):
        """查动态黑名单(untradable_symbols 表)——下单失败 51001/51087 自动登记,
        避免同符号每轮扫描反复下单失败。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            row = sdb.q1("SELECT 1 FROM untradable_symbols WHERE base=?",
                         [base], db_path=self._db_path)
            return bool(row)
        except Exception:
            return False

    def _long_scan_progress(self):
        """长扫描逐币进度回调(2026-08-17): 插拍止损监控 + 心跳/tick 进度 +
        60s 仓位快照——screen_daily/scan_signals 的长循环不再造成监控盲窗,
        watchdog tick 判死也看到真实进度(单调用死锁仍会被抓)。"""
        try:
            from execution.pidfile import write_heartbeat, write_tick
            write_heartbeat("directional")
            write_tick("directional")
            self.monitor()
            now = time.time()
            if now - getattr(self, "_last_snap_progress", 0) >= 60:
                self._last_snap_progress = now
                import storage.db as sdb
                sdb.init_db(self._db_path)
                with sdb.tx(db_path=self._db_path) as conn:
                    for p in self.exchange.fetch_positions():
                        conn.execute(
                            "INSERT INTO position_snapshots (ts,inst_id,side,"
                            "contracts,base_qty,avg_px) VALUES (?,?,?,?,?,?)",
                            [time.time(), p.inst_id, p.side, p.contracts,
                             round(p.base_qty, 8), p.avg_px])
        except Exception:
            pass

    def _log_scan_decision(self, base, has_signal, direction, decision, reason=""):
        """信号决策过程落库（self-evolution 看账数据）：每币每轮扫都记一条。
        Phase0 T0.4：落库目标 = self._db_path（生产 None=共享库；测试传隔离路径，
        防测试进程写生产表——DEF-8 溯源：test_decision_loop 曾把 阈值85 行写进生产库）。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            sdb.x("INSERT INTO scan_decisions (ts, base, venue, has_signal, direction, "
                  "threshold, decision, reason) VALUES (?,?,?,?,?,?,?,?)",
                  [time.time(), base, self.exchange.venue_for(base), 1 if has_signal else 0,
                   direction or "", self.effective_threshold(), decision, reason],
                  db_path=self._db_path)
        except Exception:
            pass

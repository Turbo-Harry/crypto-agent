"""
策略 B —— 突破/动量确认信号（Phase 4 T3.3 影子模式）。

定位: 与策略 A(趋势回踩)互补的第二信号源——趋势强时用突破,让每个 regime
都有可采集的信号(调研报告 R3 策略套件候选 B)。当前行情横盘时 A 缺料,
B 在出现真实突破时记录假设性交易,两类信号的真实样本分表对照。

影子政策(红线级):
  - 只写 shadow_signals 表,绝不下单、不发飞书、不占额度/账本/冷却;
  - 验证门(S1-S3)通过 + 人工批准前永不转正(设计文档 §4 Phase 3 红线)。

信号定义(简单可解释,防过拟合):
  多头: 1H 收盘突破前 N 根最高价(不含当前根) 且 量能 ≥ 均量×RATIO 且 阳线。
  空头: 镜像。
  入场=突破收盘价; 止损=入场∓1×ATR; 止盈=入场±2×ATR(与 A 同风险框架)。
影子分(0-100): 突破幅度/ATR 50% + 量能比 50%（公式内联,非参数）。
"""
import time


def breakout_signal(klines):
    """klines: [[ts,o,h,l,c,v],...] 升序。返回 sig dict 或 None。
    参数读 config(BREAKOUT_LOOKBACK/BREAKOUT_VOL_RATIO,统一维护)。"""
    import config
    lookback = config.BREAKOUT_LOOKBACK
    vol_ratio = config.BREAKOUT_VOL_RATIO
    if len(klines) < lookback + 10:
        return None
    from strategy.indicators import atr as atr_fn
    kd = [{"open": k[1], "high": k[2], "low": k[3], "close": k[4],
           "volume": k[5]} for k in klines]
    atr_val = atr_fn(kd, 14)
    if not atr_val:
        return None
    last = klines[-1]
    prev = klines[-lookback - 1:-1]
    prior_high = max(k[2] for k in prev)
    prior_low = min(k[3] for k in prev)
    avg_vol = sum(k[5] for k in prev) / len(prev) if prev else 0
    ts, o, h, l, c, v = last
    if c > prior_high and avg_vol > 0 and v >= avg_vol * vol_ratio and c > o:
        break_amp = (c - prior_high) / atr_val
        vol_r = v / avg_vol
        score = round(100 * (0.5 * min(break_amp, 1.0)
                             + 0.5 * min(vol_r / 2, 1.0)), 1)
        return {"dir": "long", "entry": c, "stop": c - atr_val,
                "tp": c + 2 * atr_val, "atr": atr_val,
                "shadow_score": score, "kline_ts": ts}
    if c < prior_low and avg_vol > 0 and v >= avg_vol * vol_ratio and c < o:
        break_amp = (prior_low - c) / atr_val
        vol_r = v / avg_vol
        score = round(100 * (0.5 * min(break_amp, 1.0)
                             + 0.5 * min(vol_r / 2, 1.0)), 1)
        return {"dir": "short", "entry": c, "stop": c + atr_val,
                "tp": c - 2 * atr_val, "atr": atr_val,
                "shadow_score": score, "kline_ts": ts}
    return None


def record_shadow(base, strategy, sig, db_path=None, klines_1h=None):
    """影子落库(按 base+strategy+kline_ts 去重);不产生任何真实交易副作用。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        dup = sdb.q1("SELECT id FROM shadow_signals WHERE base=? AND strategy=? "
                     "AND kline_ts=?", [base, strategy, sig.get("kline_ts")],
                     db_path=db_path)
        if dup:
            return False
        reg_tag = None
        if klines_1h:
            try:
                from engines.feature_collector import compute_regime
                kd = [{"open": k[1], "high": k[2], "low": k[3], "close": k[4],
                       "volume": k[5]} for k in klines_1h]
                reg = compute_regime(kd)
                reg_tag = reg.get("tag") if reg else None
            except Exception:
                reg_tag = None
        sdb.x("INSERT INTO shadow_signals (ts, base, strategy, dir, entry, stop, "
              "tp, atr, signal_score, regime_tag, kline_ts, status) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              [time.time(), base, strategy, sig["dir"], sig["entry"], sig["stop"],
               sig["tp"], sig["atr"], sig.get("shadow_score"), reg_tag,
               sig.get("kline_ts"), "hypothetical"], db_path=db_path)
        return True
    except Exception:
        return False


def profile_from_klines(klines, db_path=None):
    """未触发信号复盘(2026-08-17): 从 1H K 线计算四环节条件画像。
    复用策略 A 的同款条件(趋势/触线/影线),量能环节为突破确认的均量比。
    返回 dict; 任何异常返回 None。
    db_path: 活体影线比可能已被批准覆盖,画像口径与扫描一致。"""
    import config
    try:
        kd = [{"open": k[1], "high": k[2], "low": k[3], "close": k[4],
               "volume": k[5]} for k in klines]
        closes = [k["close"] for k in kd]
        if len(closes) < 55:
            return None
        from strategy.indicators import ema, atr as atr_fn
        e20, e50 = ema(closes, 20), ema(closes, 50)
        atr_val = atr_fn(kd, 14) or 0
        last = kd[-1]
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        upper_wick = last["high"] - max(last["open"], last["close"])
        from decision.scan_evolve import effective_wick_ratio
        ratio = effective_wick_ratio(db_path)
        trend_up = e20[-1] > e50[-1]
        trend_down = e20[-1] < e50[-1]
        touch_long = last["low"] <= e20[-1] and last["close"] > e20[-1]
        touch_short = last["high"] >= e20[-1] and last["close"] < e20[-1]
        wick_long = lower_wick >= body * ratio
        wick_short = upper_wick >= body * ratio
        prev_vol = [k["volume"] for k in kd[-21:-1]]
        avg_vol = sum(prev_vol) / len(prev_vol) if prev_vol else 0
        vol_ratio = (last["volume"] / avg_vol) if avg_vol > 0 else 0
        # 瓶颈识别(按信号成立顺序): 趋势 → 触线 → 影线 → 量能
        bottleneck = "none"
        if not trend_up and not trend_down:
            bottleneck = "trend"
        elif not (touch_long or touch_short):
            bottleneck = "touch"
        elif not (wick_long or wick_short):
            bottleneck = "wick"
        elif vol_ratio < config.BREAKOUT_VOL_RATIO:
            bottleneck = "vol"
        # 近失: 影线差一点(≥0.8×门槛)或触线贴边(0.5×ATR 内)
        near_miss = 0
        if not wick_long and not wick_short and atr_val > 0:
            if body > 0:
                wl = lower_wick / body
                ws = upper_wick / body
                if max(wl, ws) >= ratio * config.NEAR_MISS_WICK_FRAC:
                    near_miss = 1
        return {"trend_up": 1 if trend_up else 0,
                "trend_down": 1 if trend_down else 0,
                "touch_long": 1 if touch_long else 0,
                "touch_short": 1 if touch_short else 0,
                "wick_long": 1 if wick_long else 0,
                "wick_short": 1 if wick_short else 0,
                "vol_ratio": round(vol_ratio, 3),
                "bottleneck": bottleneck,
                "near_miss": near_miss}
    except Exception:
        return None


def record_profile(base, profile, db_path=None):
    """未触发画像落库(信号未触发的复盘证据)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("INSERT INTO signal_profiles (ts, base, trend_up, trend_down, "
              "touch_long, touch_short, wick_long, wick_short, vol_ratio, "
              "bottleneck, near_miss) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              [time.time(), base, profile["trend_up"], profile["trend_down"],
               profile["touch_long"], profile["touch_short"],
               profile["wick_long"], profile["wick_short"],
               profile["vol_ratio"], profile["bottleneck"],
               profile["near_miss"]], db_path=db_path)
        return True
    except Exception:
        return False

"""
Phase 1 结构化特征采集 —— trade_features 表读写（每笔交易一行）。

设计依据: docs/plans/2026-08-16_self_evolution_design.md Phase 1
字段文档: docs/architecture/trade_features_schema.md

原则（红线级）:
  1. 影子模式: 所有特征（尤其 signal_score）只记录、不参与任何交易决策。
  2. 离线安全: 网络源（订单流/OI）best-effort,失败→None 并计入 features_missing。
  3. 零回归: 采集异常绝不向上抛出（调用方另加 try/except 双保险）。
"""
import time

# 订单流字段组（缺失时整体计入 features_missing）
OF_FIELDS = ["of_imbalance", "of_taker_ratio", "of_oi_usd",
             "of_lsr_taker", "of_long_liq", "of_short_liq"]


def compute_regime(klines_1h, closes_4h=None):
    """轻量 regime 标签（Phase 1 T1.4，不用 HMM——防过拟合）:
    - vol_pct: 当前 14-bar ATR% 在近窗口的百分位（0~1）
    - trend_slope: 1h 收盘近 10 根斜率
    - tf4h_spread: 4h EMA20-EMA50 离散度（多周期共振强度）
    返回 dict 或 None（数据不足）。"""
    try:
        closes = [k["close"] for k in klines_1h]
        n = len(closes)
        if n < 30:
            return None
        from strategy.indicators import atr as atr_fn
        atrs = []
        for i in range(14, n):
            win = klines_1h[i - 13:i + 1]
            a = atr_fn(win, 14)
            if a and closes[i]:
                atrs.append(a / closes[i])
        if len(atrs) < 10:
            return None
        cur = atrs[-1]
        vol_pct = sum(1 for v in atrs if v <= cur) / len(atrs)
        trend_slope = ((closes[-1] - closes[-10]) / closes[-10]
                       if closes[-10] else None)
        tf4h_spread = None
        if closes_4h and len(closes_4h) >= 50:
            from strategy.indicators import ema
            e20, e50 = ema(closes_4h, 20), ema(closes_4h, 50)
            if e50[-1]:
                tf4h_spread = (e20[-1] - e50[-1]) / e50[-1]
        tag = ("low_vol" if vol_pct < 0.34
               else "high_vol" if vol_pct > 0.67 else "mid_vol")
        return {"vol_pct": round(vol_pct, 4), "trend_slope": trend_slope,
                "tf4h_spread": tf4h_spread, "tag": tag}
    except Exception:
        return None


def _orderflow_snapshot(base, exchange_name):
    """订单流快照（仅生产 OKX 适配器启用；测试 FakeAdapter 跳过防触网）。
    返回 (dict, missing_list)。任何失败→None 字段计入 missing。"""
    if exchange_name != "okx":
        return {}, list(OF_FIELDS)
    snap = {"of_imbalance": None, "of_taker_ratio": None, "of_oi_usd": None,
            "of_lsr_taker": None, "of_long_liq": None, "of_short_liq": None}
    missing = []
    try:
        from data.fetch_orderflow import orderflow_snapshot
        s = orderflow_snapshot(f"{base}USDT")
        snap["of_imbalance"] = s.get("imbalance")
        snap["of_taker_ratio"] = s.get("taker_buy_ratio")
    except Exception:
        missing += ["of_imbalance", "of_taker_ratio"]
    try:
        from data.fetch_open_interest import fetch_oi
        oi = fetch_oi(f"{base}_USDT")
        snap["of_oi_usd"] = oi.get("open_interest_usd")
        snap["of_lsr_taker"] = oi.get("lsr_taker")
        snap["of_long_liq"] = oi.get("long_liq_usd")
        snap["of_short_liq"] = oi.get("short_liq_usd")
    except Exception:
        missing += ["of_oi_usd", "of_lsr_taker", "of_long_liq", "of_short_liq"]
    return snap, missing


def collect_entry_features(trade_id, base, sig, venue, exchange_name,
                           db_path=None):
    """开仓后写入入场特征。sig 可含 Phase1 影子字段（shadow_score/regime），
    缺失时容忍并计入 features_missing。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        reg = sig.get("regime") or {}
        snap, of_missing = _orderflow_snapshot(base, exchange_name)
        missing = list(of_missing)
        if sig.get("shadow_score") is None:
            missing.append("signal_score")
        if not reg:
            missing += ["regime_tag", "vol_pct", "trend_slope", "tf4h_spread"]
        sdb.x("INSERT OR REPLACE INTO trade_features (trade_id, entry_ts, "
              "symbol, direction, venue, entry_price, stop_loss, take_profit, "
              "atr, signal_score, regime_tag, vol_pct, trend_slope, tf4h_spread, "
              "of_imbalance, of_taker_ratio, of_oi_usd, of_lsr_taker, "
              "of_long_liq, of_short_liq, features_missing) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              [trade_id, time.time(), base, sig.get("dir", "long"), venue,
               sig.get("entry"), sig.get("stop"), sig.get("tp"), sig.get("atr"),
               sig.get("shadow_score"), reg.get("tag"), reg.get("vol_pct"),
               reg.get("trend_slope"), reg.get("tf4h_spread"),
               snap.get("of_imbalance"), snap.get("of_taker_ratio"),
               snap.get("of_oi_usd"), snap.get("of_lsr_taker"),
               snap.get("of_long_liq"), snap.get("of_short_liq"),
               ",".join(missing)], db_path=db_path)
        return True
    except Exception:
        return False


def collect_close_features(trade_id, t, closed, klines_1m, post_rev=None,
                           db_path=None):
    """平仓后更新离场特征（R 倍数/MFE/MAE/滑点/持仓时长/反转）。
    klines_1m: [ts_ms,o,h,l,c,v] 列表或 None（数据不足→mfe/mae 缺失记账）。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        entry = float(t.get("entry_price") or 0)
        exit_px = float(closed.get("exit_price") or 0)
        stop = t.get("stop_loss")
        direction = t.get("direction") or "long"
        stop_dist = abs(entry - stop) / entry if entry and stop else None
        pnl = closed.get("pnl")
        r_multiple = (pnl / stop_dist) if pnl is not None and stop_dist else None

        missing = []
        mfe_r = mae_r = None
        if klines_1m and entry and exit_px:
            entry_ms = (t.get("entry_time") or 0) * 1000
            exit_ms = (closed.get("exit_time") or time.time()) * 1000
            bars = [k for k in klines_1m
                    if entry_ms - 60_000 <= k[0] <= exit_ms + 60_000]
            if len(bars) >= 3:
                highs = [k[2] for k in bars]
                lows = [k[3] for k in bars]
                mfe = (max(highs) - entry) if direction == "long" else \
                      (entry - min(lows))
                mae = (entry - min(lows)) if direction == "long" else \
                      (max(highs) - entry)
                if stop_dist:
                    mfe_r = round(mfe / entry / stop_dist, 4)
                    mae_r = round(mae / entry / stop_dist, 4)
            else:
                missing += ["mfe_r", "mae_r"]
        else:
            missing += ["mfe_r", "mae_r"]

        # 滑点: 出场价 vs 触发位（止损/止盈）
        slippage_bps = None
        reason = closed.get("exit_reason") or ""
        trigger = None
        if "止损" in reason and stop:
            trigger = stop
        elif "止盈" in reason and t.get("take_profit"):
            trigger = t.get("take_profit")
        if trigger and entry:
            slippage_bps = round(abs(exit_px - trigger) / entry * 1e4, 2)

        holding_hours = None
        if t.get("entry_time") and closed.get("exit_time"):
            holding_hours = round((closed["exit_time"] - t["entry_time"]) / 3600, 4)

        fields = {
            "exit_ts": closed.get("exit_time") or time.time(),
            "exit_price": exit_px, "exit_reason": reason[:200],
            "pnl": pnl, "r_multiple": r_multiple,
            "mfe_r": mfe_r, "mae_r": mae_r,
            "holding_hours": holding_hours, "slippage_bps": slippage_bps,
            "reversal": 1 if post_rev else 0,
        }
        cur = sdb.q1("SELECT features_missing FROM trade_features "
                     "WHERE trade_id=?", [trade_id], db_path=db_path)
        old_missing = (cur["features_missing"] if cur else "")
        merged = sorted(set((old_missing.split(",") if old_missing else [])
                            + missing))
        sets = ", ".join(f"{k}=?" for k in fields) + ", features_missing=?"
        params = list(fields.values()) + [",".join(merged), trade_id]
        sdb.x(f"UPDATE trade_features SET {sets} WHERE trade_id=?",
              params, db_path=db_path)
        if cur is None:
            # 入场特征行缺失（罕见）: 以 journal 兜底补一行
            sdb.x("INSERT OR REPLACE INTO trade_features (trade_id, entry_ts, "
                  "symbol, direction, venue, entry_price, stop_loss, "
                  "take_profit, features_missing) VALUES (?,?,?,?,?,?,?,?,?)",
                  [trade_id, t.get("entry_time"), t.get("symbol"),
                   direction, t.get("venue") or "swap", entry, stop,
                   t.get("take_profit"), ",".join(merged)], db_path=db_path)
        return True
    except Exception:
        return False

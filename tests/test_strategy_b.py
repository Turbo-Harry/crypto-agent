"""
策略 B（突破/动量确认）影子模式离线单测：
  1. 突破阳线+放量 → 出多头信号（影子分 0-100）
  2. 未突破/缩量 → None
  3. record_shadow 落隔离库 + 同 kline_ts 去重
  4. 引擎级: B 触发但 A 不触发的行情 → shadow_signals 有行、fake.orders==0（绝不下单）
运行: PYTHONPATH=lib python3 tests/test_strategy_b.py
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import engines.directional_trader as dt_mod
from engines.strategy_b import breakout_signal, record_shadow
from tests.test_phase0_review import _make_trader, _silence_notify, \
    _restore_notify

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def _flat_then_breakout(n_flat=30, base=100.0, vol=1000.0, big_vol=2000.0,
                        direction="up"):
    """前 n_flat 根横盘,最后一根放量突破(阳线突破前高)。"""
    out = []
    t0 = int(time.time() * 1000) - (n_flat + 1) * 3600_000
    for i in range(n_flat):
        out.append([t0 + i * 3600_000, base, base + 0.5, base - 0.5, base, vol])
    if direction == "up":
        last = [t0 + n_flat * 3600_000, base + 0.4, base + 2.0, base + 0.3,
                base + 1.8, big_vol]     # 阳线,收盘破前高 0.5,量大
    else:
        last = [t0 + n_flat * 3600_000, base - 0.4, base - 0.3, base - 2.0,
                base - 1.8, big_vol]
    out.append(last)
    return out


def test_breakout_signal():
    print("== breakout_signal 纯函数（多空双向）==")
    kl = _flat_then_breakout()
    sig = breakout_signal(kl)
    check("放量突破阳线 → 多头信号", sig is not None and sig["dir"] == "long",
          f"实际 {sig}")
    if sig:
        check("影子分 ∈ [0,100]", 0 <= sig["shadow_score"] <= 100)
        check("多头止损止盈按 1×/2×ATR", abs(sig["stop"] - (sig["entry"] - sig["atr"])) < 1e-9
              and abs(sig["tp"] - (sig["entry"] + 2 * sig["atr"])) < 1e-9)
    sig_s = breakout_signal(_flat_then_breakout(direction="down"))
    check("放量跌破阴线 → 空头信号",
          sig_s is not None and sig_s["dir"] == "short", f"实际 {sig_s}")
    if sig_s:
        check("空头止损止盈按 1×/2×ATR（止损在上/止盈在下）",
              abs(sig_s["stop"] - (sig_s["entry"] + sig_s["atr"])) < 1e-9
              and abs(sig_s["tp"] - (sig_s["entry"] - 2 * sig_s["atr"])) < 1e-9)
    sig2 = breakout_signal(_flat_then_breakout(big_vol=1000.0))  # 量不达标
    check("缩量突破 → None", sig2 is None)
    # 最后一根在区间内 → None
    kl3 = _flat_then_breakout()
    kl3[-1] = kl3[-2][:]
    check("无突破 → None", breakout_signal(kl3) is None)


def _downtrend_rejection(n=95, base=100.0, vol=1000.0):
    """下跌趋势 + 最后一根反弹 EMA20 上影线拒绝（策略 A 空头信号形态）。"""
    from strategy.indicators import ema
    out = []
    t0 = int(time.time() * 1000) - (n + 5) * 3600_000
    closes = []
    for i in range(n):
        c = base - i * 0.15
        closes.append(c)
        out.append([t0 + i * 3600_000, c + 0.05, c + 0.3, c - 0.3, c, vol])
    e = ema(closes, 20)[-1]
    # 反弹段: 4 根小阳回到 EMA20 附近
    for i in range(4):
        c = closes[-1] + (i + 1) * 0.1
        out.append([t0 + (n + i) * 3600_000, c - 0.05, c + 0.2, c - 0.2, c, vol])
    # 最后一根: 上影线拒绝（high 破 EMA20,收盘回落其下,上影 ≥ 实体）
    o = e - 0.2
    out.append([t0 + (n + 4) * 3600_000, o, e + 1.0, e - 0.6, e - 0.45, vol])
    return out


def test_scan_signal_short():
    print("== 策略 A 空头分支（空头趋势+反弹拒绝K线）==")
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="tst_ashort_")
    dt, fake = _make_trader(tmp)
    from exchange.models import Candle
    kl = _downtrend_rejection()
    fake.candles["BTC-USDT-SWAP"] = [
        Candle(ts=k[0], open=k[1], high=k[2], low=k[3], close=k[4],
               volume=k[5]) for k in kl]
    fake.last_prices["BTC-USDT-SWAP"] = kl[-1][4]
    fake.last_prices["BTC-USDT"] = kl[-1][4]
    sig = dt.scan_signal("BTC")
    check("空头信号触发", sig is not None and sig["dir"] == "short",
          f"实际 {sig}")
    if sig:
        check("止损在上/止盈在下",
              sig["stop"] > sig["entry"] > sig["tp"])


def test_record_shadow_dedup(tmp):
    print("== record_shadow 落库 + 去重 ==")
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "sb.db")
    sig = {"dir": "long", "entry": 100.0, "stop": 99.0, "tp": 102.0,
           "atr": 1.0, "shadow_score": 66.6, "kline_ts": 12345}
    ok = record_shadow("BTC", "B_breakout", sig, db_path=db)
    ok2 = record_shadow("BTC", "B_breakout", sig, db_path=db)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]
    conn.close()
    check("首次落库成功", ok is True)
    check("同 kline_ts 去重(仍 1 行)", ok2 is False and n == 1, f"实际 {n}")


def test_engine_shadow_no_orders(tmp):
    print("== 引擎级: B 触发/A 不触发 → 只记影子绝不下单 ==")
    tmp = os.path.join(tmp, "eng")
    os.makedirs(tmp, exist_ok=True)
    _silence_notify()
    try:
        dt, fake = _make_trader(tmp)
        from exchange.models import Candle
        kl = _flat_then_breakout()
        fake.candles["BTC-USDT-SWAP"] = [
            Candle(ts=k[0], open=k[1], high=k[2], low=k[3], close=k[4],
                   volume=k[5]) for k in kl]
        fake.last_prices["BTC-USDT-SWAP"] = kl[-1][4]
        fake.last_prices["BTC-USDT"] = kl[-1][4]
        dt.watchlist = ["BTC"]
        dt.watch_scores = {"BTC": 0.9}
        dt._watch_date = time.strftime("%Y-%m-%d")
        dt._last_watch_refresh = time.time()
        dt._last_scan = 0
        dt.signal_cool = {}
        dt.scan_signals()
        conn = sqlite3.connect(os.path.join(tmp, "scan.db"))
        n = conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]
        conn.close()
        check("影子信号落库(隔离)", n >= 1, f"实际 {n}")
        check("零真实下单(fake.orders==0)", len(fake.orders) == 0,
              f"实际 {len(fake.orders)}")
        check("journal 零新增交易", len(dt.journal.trades) == 0,
              f"实际 {len(dt.journal.trades)}")
    finally:
        _restore_notify()


def test_order_failure_logged(tmp):
    print("== 下单失败结构化日志（order_failures）==")
    from exchange.models import OrderResult
    tmp = os.path.join(tmp, "of")
    os.makedirs(tmp, exist_ok=True)
    dt, fake = _make_trader(tmp)
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0

    def _fail(*a, **kw):
        return OrderResult(ok=False, message="模拟下单失败")
    fake.place_market_order = _fail
    sig = {"dir": "long", "entry": 110.0, "stop": 108.9, "tp": 112.2, "atr": 1.1}
    dt.open_position("BTC", sig, score=80)
    conn = sqlite3.connect(os.path.join(tmp, "scan.db"))
    n = conn.execute("SELECT COUNT(*) FROM order_failures").fetchone()[0]
    row = conn.execute("SELECT stage, error FROM order_failures LIMIT 1").fetchone()
    conn.close()
    check("失败下单入 order_failures", n >= 1 and row and row[0] == "open",
          f"实际 n={n} row={row}")

def test_profile_and_record(tmp):
    print("== 未触发信号复盘（画像 + 瓶颈 + 落库）==")
    from engines.strategy_b import profile_from_klines, record_profile
    tmp = os.path.join(tmp, "prof")
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "sp.db")
    # 纯横盘段(60 根同价): 无趋势 → 瓶颈 trend
    n = 60
    t0 = int(time.time() * 1000) - (n + 1) * 3600_000
    flat = [[t0 + i * 3600_000, 100.0, 100.1, 99.9, 100.0, 1000]
            for i in range(n)]
    prof = profile_from_klines(flat, db_path=db)
    check("横盘画像: 无趋势", prof is not None and not prof["trend_up"]
          and not prof["trend_down"], f"实际 {prof}")
    check("横盘瓶颈 = trend", prof and prof["bottleneck"] == "trend")
    # 纯下跌段(未反弹触线): 趋势空头成立、未触线 → 瓶颈 touch
    n = 60
    t0 = int(time.time() * 1000) - (n + 1) * 3600_000
    kl = [[t0 + i * 3600_000, 100 - i * 0.2, 100 - i * 0.2 + 0.1,
           100 - i * 0.2 - 0.1, 100 - i * 0.2, 1000] for i in range(n)]
    prof2 = profile_from_klines(kl, db_path=db)
    check("下跌趋势画像: trend_down=1", prof2 is not None
          and prof2["trend_down"] == 1, f"实际 {prof2}")
    check("未触线瓶颈 = touch",
          prof2 is not None and prof2["bottleneck"] == "touch",
          f"实际 {prof2 and prof2['bottleneck']}")
    ok = record_profile("BTC", prof2, db_path=db)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM signal_profiles").fetchone()[0]
    conn.close()
    check("画像落库(隔离)", ok is True and n == 1, f"实际 n={n}")

def test_anomalies_registry(tmp):
    print("== 统一异常中心(登记/去重/列表) ==")
    from tools.anomalies import register, list_new, resolve
    tmp = os.path.join(tmp, "anom" + str(int(time.time() * 1000) % 100000))
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "a.db")
    ok1 = register("order_failure", "BTC sell 下单失败", "测试1",
                   severity="error", db_path=db)
    ok2 = register("order_failure", "BTC sell 下单失败", "测试2",
                   severity="error", db_path=db)
    ok3 = register("engine_error", "引擎异常X", "测试3",
                   severity="error", db_path=db)
    rows = list_new(db_path=db)
    check("首条登记成功", ok1 is True)
    check("同源同题 30min 去重", ok2 is False)
    check("不同源可登记", ok3 is True and len(rows) == 2, f"实际 {len(rows)}")
    resolve("engine_error", "引擎异常X", db_path=db)
    check("resolve 后列表只剩 1 条", len(list_new(db_path=db)) == 1)

def test_attribution_feedback():
    print("== 归因反哺规则(阈值触发/抑制) ==")
    from tools.no_signal_report import generate_feedback

    def rows(bn, near=0):
        out = []
        for b, c in bn.items():
            out += [{"bottleneck": b, "near_miss": 1} for _ in range(c)]
        return out

    fb = generate_feedback(rows({"touch": 24}))
    check("touch 主瓶颈 → R3 纪律等待触发",
          any(r[0] == "R3" and r[3] for r in fb), f"实际 {fb}")
    check("touch 主瓶颈 → R1/R2 不触发",
          all(not r[3] for r in fb if r[0] in ("R1", "R2")))
    fb2 = generate_feedback(rows({"trend": 20, "wick": 10}))
    check("trend≥60% → R2 策略B转正评估触发",
          any(r[0] == "R2" and r[3] for r in fb2), f"实际 {fb2}")
    fb3 = generate_feedback(rows({"wick": 24}, near=6))
    check("wick 主瓶颈+近失≥20% → R1 门槛微调候选",
          any(r[0] == "R1" and r[3] for r in fb3), f"实际 {fb3}")
    fb4 = generate_feedback(rows({"touch": 5}))
    check("样本<20 → R0 搁置", any(r[0] == "R0" for r in fb4))

if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="tst_sb_")
    test_breakout_signal()
    test_record_shadow_dedup(os.path.join(tmp, "d1"))
    test_engine_shadow_no_orders(tmp)
    test_scan_signal_short()
    test_order_failure_logged(os.path.join(tmp, "of"))
    test_profile_and_record(os.path.join(tmp, "prof"))
    test_anomalies_registry(os.path.join(tmp, "anom"))
    test_attribution_feedback()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


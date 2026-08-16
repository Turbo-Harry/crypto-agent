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
    print("== breakout_signal 纯函数 ==")
    kl = _flat_then_breakout()
    sig = breakout_signal(kl)
    check("放量突破阳线 → 多头信号", sig is not None and sig["dir"] == "long",
          f"实际 {sig}")
    if sig:
        check("影子分 ∈ [0,100]", 0 <= sig["shadow_score"] <= 100)
        check("止损止盈按 1×/2×ATR", abs(sig["stop"] - (sig["entry"] - sig["atr"])) < 1e-9
              and abs(sig["tp"] - (sig["entry"] + 2 * sig["atr"])) < 1e-9)
    sig2 = breakout_signal(_flat_then_breakout(big_vol=1000.0))  # 量不达标
    check("缩量突破 → None", sig2 is None)
    # 最后一根在区间内 → None
    kl3 = _flat_then_breakout()
    kl3[-1] = kl3[-2][:]
    check("无突破 → None", breakout_signal(kl3) is None)


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


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="tst_sb_")
    test_breakout_signal()
    test_record_shadow_dedup(os.path.join(tmp, "d1"))
    test_engine_shadow_no_orders(tmp)
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

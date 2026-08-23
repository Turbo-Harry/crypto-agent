"""
连亏冷却回归测试（2026-08-23 用户指示"连亏 6 笔后应主动冷却，不硬接信号 解除冷却"）:
  1. 连亏计数: 净亏+1,盈利归零
  2. 达阈值(6) → 冷却启动,连亏计数重置
  3. 冷却中 is_cooling=True;到期自动解除
  4. release() 手动解除
  5. 开关关闭 → 不启动
运行: PYTHONPATH=lib python3 tests/test_loss_cooling.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

import config
from decision.loss_cooling import (streak, is_cooling, release, on_close,
                                   cooling_remaining_hours)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="cool_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)
    _old = (config.LOSS_STREAK_COOL_ENABLED, config.LOSS_STREAK_COOL_THRESHOLD,
            config.LOSS_STREAK_COOL_HOURS)
    config.LOSS_STREAK_COOL_ENABLED = True
    config.LOSS_STREAK_COOL_THRESHOLD = 6
    config.LOSS_STREAK_COOL_HOURS = 6

    # 连亏步进
    for i in range(5):
        a = on_close(db, -0.01)
        check(f"第{i+1}笔净亏 → streak={i+1}", streak(db) == i + 1)
    a6 = on_close(db, -0.01)
    check("第6笔净亏 → 冷却启动", a6 == "cooling_started" and is_cooling(db),
          f"action={a6}")
    check("冷却启动后连亏计数归零", streak(db) == 0)
    check("剩余冷却 ≈ 6h", abs(cooling_remaining_hours(db) - 6.0) < 0.1)

    # 盈利重置计数
    on_close(db, 0.02)
    check("盈利 → 连亏计数归零(且不解除冷却)", streak(db) == 0
          and is_cooling(db))

    # 手动解除
    ok = release(db)
    check("release() 手动解除冷却", ok and not is_cooling(db))

    # 到期自动解除
    on_close(db, -0.01) * 0 or None
    for i in range(5):
        on_close(db, -0.01)
    on_close(db, -0.01)   # 触发冷却
    check("再次触发冷却", is_cooling(db))
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES "
                 "('cooling_until', ?)", [str(time.time() - 1)])
    conn.commit(); conn.close()
    check("到期后 is_cooling 自动解除", not is_cooling(db))

    # 开关关闭
    config.LOSS_STREAK_COOL_ENABLED = False
    for i in range(10):
        on_close(db, -0.01)
    check("开关关闭 → 不启动冷却", not is_cooling(db))

    # ---- 模式门控: 模拟盘不冷却(2026-08-23 用户指示"模拟盘去掉保持锁定") ----
    config.LOSS_STREAK_COOL_ENABLED = True
    config.LOSS_STREAK_COOL_PAPER_ENABLED = False
    _old_mode = config.CRYPTO_MODE
    config.CRYPTO_MODE = "paper"
    for i in range(10):
        on_close(db, -0.01)
    check("paper 模式 → 连亏 10 笔也不冷却", not is_cooling(db))
    config.CRYPTO_MODE = "live"
    for i in range(6):
        on_close(db, -0.01)
    check("live 模式 → 6 连亏照常冷却(保持锁定)", is_cooling(db))
    release(db)
    config.CRYPTO_MODE = _old_mode

    config.LOSS_STREAK_COOL_ENABLED, config.LOSS_STREAK_COOL_THRESHOLD, \
        config.LOSS_STREAK_COOL_HOURS = _old
    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

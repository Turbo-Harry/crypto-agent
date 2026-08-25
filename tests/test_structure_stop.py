"""
结构位止损口径回归测试（2026-08-25 用户质疑"ATR 不靠谱"）:
  1. structure 模式: 止损锚定摆动结构位(外扩0.2×ATR),止盈=止损距×2
  2. 结构位比 ATR 更近时 → ATR 作下限(止损不比纯 ATR 更紧)
  3. 结构位缺失 → 回退纯 ATR
  4. atr 模式 → 纯 1×ATR / 2×ATR(旧口径)
  5. 空头镜像
运行: PYTHONPATH=lib python3 tests/test_structure_stop.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engines.signal_scan import resolve_stop_tp

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
    _old_mode = config.STOP_MODE
    config.STOP_MODE = "structure"

    # 多: 入场100, ATR=2, 摆动低点95 → 结构止损 95-0.4=94.6, ATR止损 98
    s, t = resolve_stop_tp(100.0, 2.0, "long", swing_level=95.0)
    check("多: 结构止损 94.6(比ATR 98 更宽)", s == 94.6, f"stop={s}")
    check("多: 止盈=止损距×1 → 105.4", abs(t - 105.4) < 1e-9, f"tp={t}")

    # 结构位太近: 摆动低点 99.5 → 结构止损 99.1, 但 ATR 下限 98 更宽 → 取 98
    s2, t2 = resolve_stop_tp(100.0, 2.0, "long", swing_level=99.5)
    check("结构位过近 → ATR 下限(98)", s2 == 98.0, f"stop={s2}")
    check("止盈 1:1(102)", abs(t2 - 102.0) < 1e-9, f"tp={t2}")

    # 结构位缺失 → 纯 ATR
    s3, t3 = resolve_stop_tp(100.0, 2.0, "long", swing_level=None)
    check("无结构位 → 纯 ATR(98/102)", s3 == 98.0 and t3 == 102.0,
          f"stop={s3} tp={t3}")

    # 空头镜像
    s4, t4 = resolve_stop_tp(100.0, 2.0, "short", swing_level=105.0)
    check("空: 结构止损 105.4 / 止盈 94.6",
          s4 == 105.4 and abs(t4 - 94.6) < 1e-9, f"stop={s4} tp={t4}")

    # 旧口径
    config.STOP_MODE = "atr"
    s5, t5 = resolve_stop_tp(100.0, 2.0, "long", swing_level=95.0)
    check("atr 模式 → 98/102(忽略结构位)", s5 == 98.0 and t5 == 102.0,
          f"stop={s5} tp={t5}")

    config.STOP_MODE = _old_mode
    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

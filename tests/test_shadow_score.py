"""
6 维信号影子分回归测试（2026-08-23 用户指示"维度太少了,加"）:
  1. 权重和=1.0(总分不超 100)
  2. 各维中性值(缺失)不污染总分——全中性=50 分
  3. 资金费方向性: 负费率利多、正费率利空
  4. 盘口失衡方向性: 买盘厚利多、卖盘厚利空
  5. 量能封顶 2x
  6. 极端强弱信号分数有区分度
运行: PYTHONPATH=lib python3 tests/test_shadow_score.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

import config
from engines.signal_scan import compute_shadow_score, _book_imbalance

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def score(**kw):
    d = dict(wick=0.2, body=0.1, price_near_ema=100.0, ema20_val=100.0,
             ema50_val=98.0, atr_val=1.0, vol_last=100.0, vol_avg=100.0,
             funding_rate=None, book_imb=None, direction="long")
    d.update(kw)
    sc, dims = compute_shadow_score(**d)
    return sc


def main():
    w = config.SHADOW_WEIGHTS
    check("权重和=1.0", abs(sum(w.values()) - 1.0) < 1e-9,
          f"sum={sum(w.values())}")
    check("权重键完整(6维)", set(w) == {"wick", "depth", "trend", "volume",
                                        "funding", "book"}, str(w))

    # 全中性: wick_s=0? 不——用每维=0.5 的组合验证总分=50:
    # wick_s=0.5 → wick/body=1.5;depth_s=0.5 → |p-ema|=0.5*atr;
    # trend_s=0.5 → |ema20-ema50|=0.01*ema50;vol_s=0.5 → vol_last=vol_avg
    s_mid = score(wick=0.15, body=0.1, price_near_ema=99.5,
                  ema20_val=99.0, ema50_val=100.0, atr_val=1.0,
                  vol_last=100.0, vol_avg=100.0, funding_rate=None,
                  book_imb=None, direction="long")
    check("全中性=50分", s_mid == 50.0, f"s={s_mid}")

    # 资金费方向性
    s_fund_long_neg = score(wick=0.15, body=0.1, price_near_ema=100.5,
                            ema50_val=99.0, funding_rate=-0.001,
                            direction="long")
    s_fund_long_pos = score(wick=0.15, body=0.1, price_near_ema=100.5,
                            ema50_val=99.0, funding_rate=+0.001,
                            direction="long")
    check("多单负费率>正费率", s_fund_long_neg > s_fund_long_pos,
          f"neg={s_fund_long_neg} pos={s_fund_long_pos}")
    s_fund_short_pos = score(wick=0.15, body=0.1, price_near_ema=100.5,
                             ema50_val=99.0, funding_rate=+0.001,
                             direction="short")
    s_fund_short_neg = score(wick=0.15, body=0.1, price_near_ema=100.5,
                             ema50_val=99.0, funding_rate=-0.001,
                             direction="short")
    check("空单正费率>负费率", s_fund_short_pos > s_fund_short_neg,
          f"pos={s_fund_short_pos} neg={s_fund_short_neg}")

    # 盘口失衡方向性
    s_book_long = score(wick=0.15, body=0.1, price_near_ema=100.5,
                        ema50_val=99.0, book_imb=0.5, direction="long")
    s_book_short = score(wick=0.15, body=0.1, price_near_ema=100.5,
                         ema50_val=99.0, book_imb=0.5, direction="short")
    check("买盘厚利多>利空", s_book_long > s_book_short,
          f"long={s_book_long} short={s_book_short}")

    # 量能封顶
    s_v1 = score(wick=0.15, body=0.1, price_near_ema=100.5, ema50_val=99.0,
                 vol_last=100.0, vol_avg=100.0)
    s_v3 = score(wick=0.15, body=0.1, price_near_ema=100.5, ema50_val=99.0,
                 vol_last=300.0, vol_avg=100.0)
    s_v10 = score(wick=0.15, body=0.1, price_near_ema=100.5, ema50_val=99.0,
                  vol_last=1000.0, vol_avg=100.0)
    check("量能 3x 封顶到 2x", abs(s_v10 - s_v3) < 1e-9 and s_v3 > s_v1,
          f"v1={s_v1} v3={s_v3} v10={s_v10}")

    # 极端区分度: 强信号 vs 弱信号
    s_strong = score(wick=0.3, body=0.1, price_near_ema=100.02, ema50_val=96.0,
                     vol_last=200.0, vol_avg=100.0, funding_rate=-0.001,
                     book_imb=0.8, direction="long")
    s_weak = score(wick=0.01, body=0.1, price_near_ema=103.0, ema50_val=99.9,
                   vol_last=10.0, vol_avg=100.0, funding_rate=0.001,
                   book_imb=-0.8, direction="long")
    check("强信号分明显高于弱信号", s_strong > s_weak + 40,
          f"strong={s_strong} weak={s_weak}")
    check("总分不超 100", s_strong <= 100 and s_weak >= 0,
          f"strong={s_strong} weak={s_weak}")

    # _book_imbalance
    imb = _book_imbalance({"bids": [[1, 8]], "asks": [[2, 2]]})
    check("盘口失衡计算 (8-2)/(8+2)=0.6", abs(imb - 0.6) < 1e-9, f"imb={imb}")
    check("空盘口返回 None", _book_imbalance(None) is None)
    check("单边盘口返回 None", _book_imbalance({"bids": [], "asks": []}) is None)

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

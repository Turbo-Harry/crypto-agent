"""T3 预测方向、终值/首触分离、经验收缩和未校准语义。"""
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from decision.forecast import forecast, calibration
from engines.signal_scan import SignalScanMixin

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def main():
    print("== 方向与障碍 ==")
    short = forecast(100, 10, "short", 110, 80,
                     [-0.02, -0.01, 0.01, -0.03] * 40,
                     paths=200, seed=7)
    check("short 正确障碍可预测", short is not None, str(short))
    check("short 分位单调", short["q05"] <= short["median"] <= short["q95"],
          str(short))
    invalid = forecast(100, 10, "short", 90, 120, [-0.01] * 60)
    check("short 反向障碍 fail-closed", invalid is None)

    class _Exchange:
        def fetch_funding_rate(self, inst_id):
            return 0.0

        def fetch_order_book(self, inst_id, depth):
            return None

    class _Scanner(SignalScanMixin):
        exchange = _Exchange()

        def __init__(self, db_path):
            self._db_path = db_path
            self.rows = []
            for i in range(100):
                close = 120 - i * 0.2
                self.rows.append([1_700_000_000_000 + i * 900_000,
                                  close + 0.1, close + 0.3, close - 0.3,
                                  close, 1000])
            last_close = self.rows[-1][4]
            self.rows[-1][1:6] = [last_close + 0.5, last_close + 4.0,
                                  last_close - 0.3, last_close, 1000]

        def _fetch_klines_any(self, base, bar, limit):
            return self.rows[-limit:]

        def _ticker_last(self, base):
            return self.rows[-1][4]

        def _inst_id(self, base):
            return f"{base}-USDT-SWAP"

    with tempfile.TemporaryDirectory() as td:
        wired = _Scanner(os.path.join(td, "short.db")).scan_signal("BTC")
        check("扫描链 short forecast 不再为空",
              wired and wired["dir"] == "short" and wired["forecast"] is not None,
              str(wired))

    print("== terminal 与 first passage 分离 ==")
    growth = math.log(1.1)
    out = forecast(100, 1, "long", 90, 105, [growth] * 40,
                   horizon=2, paths=40, block_size=2, seed=1)
    check("首根路径 K 触 TP", out["p_hit_tp"] == 1.0, str(out))
    check("终值未在 TP 截断", abs(out["median"] - 121.0) < 0.001, str(out))
    check("三类概率和为 1",
          abs(out["p_hit_tp"] + out["p_hit_sl"] + out["p_timeout"] - 1) < 1e-6,
          str(out))
    check("亏损先验固定为止损首触加一半超时",
          out["p_loss_prior"] == round(
              out["p_hit_sl"] + 0.5 * out["p_timeout"], 4), str(out))
    check("亏损先验方法可审计",
          out["loss_prior_method"] == "sl_plus_half_timeout_v1", str(out))
    check("使用 regime moving block", out["bootstrap"] == "regime_moving_block")

    flat_profiles = [{"close_ret": 0.0, "high_ret": math.log(1.03),
                      "low_ret": 0.0}] * 40
    intrabar = forecast(100, 1, "long", 99, 102, [0.0] * 40,
                        horizon=2, paths=40, block_size=2, seed=1,
                        bar_profiles=flat_profiles)
    check("15m 收盘不触障时仍能识别 bar 内 high 触 TP",
          intrabar["p_hit_tp"] == 1.0 and
          intrabar["first_passage_resolution"] == "intrabar_ohlc",
          str(intrabar))
    both_profiles = [{"close_ret": 0.0, "high_ret": math.log(1.03),
                      "low_ret": math.log(0.98)}] * 40
    ambiguous = forecast(100, 1, "long", 99, 102, [0.0] * 40,
                         horizon=1, paths=20, seed=2,
                         bar_profiles=both_profiles)
    check("模拟同 bar 双触与真实标签一致按 SL",
          ambiguous["p_hit_sl"] == 1.0 and ambiguous["p_hit_tp"] == 0.0,
          str(ambiguous))

    print("== 样本量收缩 ==")
    flat = [0.0] * 100
    small = forecast(100, 1, "long", 99, 102, flat, paths=100, seed=1,
                     emp_p_tp=0.8, emp_p_sl=0.1, emp_n=5)
    large = forecast(100, 1, "long", 99, 102, flat, paths=100, seed=1,
                     emp_p_tp=0.8, emp_p_sl=0.1, emp_n=300)
    check("小样本实证权重更低",
          small["empirical_weight"] < large["empirical_weight"],
          f"{small['empirical_weight']} vs {large['empirical_weight']}")
    check("大样本概率更接近实证", large["p_hit_tp"] > small["p_hit_tp"])

    with tempfile.TemporaryDirectory() as td:
        cal = calibration(os.path.join(td, "empty.db"), min_n=1)
        check("无样本明确 uncalibrated",
              cal["status"] == "uncalibrated" and cal["brier_tp"] is None,
              str(cal))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

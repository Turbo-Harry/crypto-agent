"""行情状态权重与影子策略路由纯函数回归。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from decision.market_regime import classify_market_regime
from decision.strategy_router import route_strategy

passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def main():
    global passed, failed
    missing = classify_market_regime({}, {})
    check("核心输入缺失 fail-closed", not missing["ready"] and
          missing["state"] == "unknown", str(missing))
    missing_route = route_strategy(missing)
    check("未知行情不选策略", missing_route["abstain"] and
          not missing_route["has_execution_authority"], str(missing_route))

    trend = classify_market_regime(
        {"vol_pct": 0.45, "trend_slope": 0.03, "tf4h_spread": 0.04},
        {"market_breadth": 0.9, "correlation_concentration": 0.8,
         "realized_vol_5m": 0.02, "vol_of_vol": 0.001})
    check("强同向环境识别为 trend", trend["state"] == "trend", str(trend))
    check("权重归一且明确未校准", abs(sum(trend["weights"].values()) - 1) < 1e-5
          and trend["calibrated"] is False, str(trend))
    trend_route = route_strategy(trend, ("A_pullback", "B_breakout"))
    check("趋势优先回踩候选", trend_route["selected_strategy"] == "A_pullback",
          str(trend_route))
    check("路由永无执行权限", trend_route["mode"] == "shadow" and
          trend_route["has_execution_authority"] is False, str(trend_route))

    expansion = classify_market_regime(
        {"vol_pct": 0.98, "trend_slope": 0.018, "tf4h_spread": 0.01},
        {"market_breadth": 0.55, "correlation_concentration": 0.45,
         "realized_vol_5m": 0.01, "vol_of_vol": 0.009})
    check("高波动不稳定环境识别为波动扩张",
          expansion["state"] == "vol_expansion", str(expansion))
    expansion_route = route_strategy(expansion, ("A_pullback", "B_breakout"))
    check("波动扩张只路由突破候选",
          expansion_route["selected_strategy"] == "B_breakout",
          str(expansion_route))

    disorder = {"ready": True, "state": "disorder", "confidence": 0.9,
                "margin": 0.8}
    disorder_route = route_strategy(disorder)
    check("混乱行情默认空仓", disorder_route["abstain"] and
          disorder_route["reason"] == "disorder_abstain", str(disorder_route))

    range_state = {"ready": True, "state": "range", "confidence": 0.8,
                   "margin": 0.5}
    range_route = route_strategy(range_state)
    check("未实现的区间策略不能伪装可用",
          range_route["abstain"] and
          range_route["reason"] == "strategy_not_implemented", str(range_route))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

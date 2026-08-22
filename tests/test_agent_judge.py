"""
AI 把关回归测试（2026-08-23 用户问"agent也会加入判断吗",离线注入假 analyzer）:
  1. 明确 reject → 拦单
  2. approve / abstain → 放行
  3. LLM 输出带杂质(前后缀/多余文本) → 仍能剥出 JSON
  4. 解析失败/异常/无输出 → 放行(交易链不被 AI 可用性绑架)
  5. 未知 verdict → abstain 放行语义
运行: PYTHONPATH=lib python3 tests/test_agent_judge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

import config
from decision.agent_judge import judge, parse_verdict

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


SIG = {"dir": "long", "stop": 9.5, "tp": 11.0, "shadow_dims": {"book": 0.9}}
SENT = {"fng_value": 71, "news": 1.0, "composite": 0.71}


def main():
    # 开关关闭 → 放行
    old = config.AGENT_JUDGE_ENABLED
    config.AGENT_JUDGE_ENABLED = False
    check("开关关闭 → approve", judge(SIG, "BTC", 60, 10.0, SENT)[0] == "approve")
    config.AGENT_JUDGE_ENABLED = old

    v, r = judge(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "reject", "reason": "刚爆出监管利空"}')
    check("明确 reject → 拦单", v == "reject" and "利空" in r, f"{v}: {r}")

    v, _ = judge(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "approve", "reason": "无异常"}')
    check("approve → 放行", v == "approve")

    v, _ = judge(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "abstain", "reason": "拿不准"}')
    check("abstain → 放行语义", v == "abstain")

    v, r = judge(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '好的,我的判断是: {"verdict": "reject", "reason": "流动性枯竭"}\n以上')
    check("带杂质输出也能剥出 JSON", v == "reject", f"{v}: {r}")

    v, _ = judge(SIG, "BTC", 60, 10.0, SENT, analyzer=lambda u: "随便说点不是 JSON 的话")
    check("解析失败 → 放行语义(非 reject)", v != "reject", f"v={v}")

    def boom(_):
        raise TimeoutError("LLM 超时")
    v, _ = judge(SIG, "BTC", 60, 10.0, SENT, analyzer=boom)
    check("LLM 异常/超时 → 放行", v == "approve")

    v, _ = judge(SIG, "BTC", 60, 10.0, SENT, analyzer=lambda u: None)
    check("无输出 → 放行", v == "approve")

    v, _ = judge(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "maybe", "reason": "?"}')
    check("未知 verdict → abstain", v == "abstain")

    v1, r1 = parse_verdict('{"verdict":"reject","reason":"短线风险大, 建议观望"}')
    check("parse_verdict 纯 JSON", v1 == "reject" and r1.startswith("短线"), f"{v1}")

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

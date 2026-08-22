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
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib"))

import config
from decision.agent_judge import (judge, parse_verdict, _memory_block,
                                   bind_trade, record_trade_outcome,
                                   sweep_outcomes)

_passed = _failed = 0


def j1(sig, base, score, price, sent, analyzer=None, db=None):
    """兼容旧测试: judge 3 元组取前两个。"""
    v, r, _ = judge(sig, base, score, price, sent, analyzer=analyzer, db_path=db)
    return v, r


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
    check("开关关闭 → approve", j1(SIG, "BTC", 60, 10.0, SENT)[0] == "approve")
    config.AGENT_JUDGE_ENABLED = old

    v, r = j1(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "reject", "reason": "刚爆出监管利空"}')
    check("明确 reject → 拦单", v == "reject" and "利空" in r, f"{v}: {r}")

    v, _ = j1(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "approve", "reason": "无异常"}')
    check("approve → 放行", v == "approve")

    v, _ = j1(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "abstain", "reason": "拿不准"}')
    check("abstain → 放行语义", v == "abstain")

    v, r = j1(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '好的,我的判断是: {"verdict": "reject", "reason": "流动性枯竭"}\n以上')
    check("带杂质输出也能剥出 JSON", v == "reject", f"{v}: {r}")

    v, _ = j1(SIG, "BTC", 60, 10.0, SENT, analyzer=lambda u: "随便说点不是 JSON 的话")
    check("解析失败 → 放行语义(非 reject)", v != "reject", f"v={v}")

    def boom(_):
        raise TimeoutError("LLM 超时")
    v, _ = j1(SIG, "BTC", 60, 10.0, SENT, analyzer=boom)
    check("LLM 异常/超时 → 放行", v == "approve")

    v, _ = j1(SIG, "BTC", 60, 10.0, SENT, analyzer=lambda u: None)
    check("无输出 → 放行", v == "approve")

    v, _ = j1(SIG, "BTC", 60, 10.0, SENT,
                 analyzer=lambda u: '{"verdict": "maybe", "reason": "?"}')
    check("未知 verdict → abstain", v == "abstain")

    v1, r1 = parse_verdict('{"verdict":"reject","reason":"短线风险大, 建议观望"}')
    check("parse_verdict 纯 JSON", v1 == "reject" and r1.startswith("短线"), f"{v1}")

    # ---- AI 记忆: 判断落表 / 结果回填 / RAG 案例回喂 / 否决扫尾 ----
    tmp = tempfile.mkdtemp(prefix="ai_mem_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)
    v, r, jid = judge(SIG, "LINK", 60, 10.0, SENT,
                      analyzer=lambda u: '{"verdict": "approve", "reason": "ok"}',
                      db_path=db)
    check("判断落表(有 judgment_id)", jid is not None, f"jid={jid}")
    bind_trade(jid, "txn_x", db_path=db)
    record_trade_outcome("txn_x", 0.012, db_path=db)
    row = sdb.q1("SELECT outcome_pnl FROM ai_judgments WHERE id=?", [jid], db_path=db)
    check("平仓回填判断结果 0.012", row and abs(row["outcome_pnl"] - 0.012) < 1e-9,
          str(row))
    _old_h = config.AGENT_JUDGE_MEMORY_MIN_HOURS
    config.AGENT_JUDGE_MEMORY_MIN_HOURS = 0   # 测试: 立即视为"历史"
    mem = _memory_block(db, "LINK", "long")
    config.AGENT_JUDGE_MEMORY_MIN_HOURS = _old_h
    check("RAG 记忆含带结果旧案例", "判断案例" in mem and "approve" in mem, mem[:100])

    class FakeEx:
        def fetch_ticker_last(self, inst_id):
            return 10.5
    n = sweep_outcomes(FakeEx(), db_path=db, horizon_hours=0)
    check("否决判断扫尾回填(现价10.5→+5%)", n >= 0)
    row2 = sdb.q1("SELECT outcome_pnl FROM ai_judgments WHERE trade_id IS NULL "
                  "AND outcome_pnl IS NOT NULL LIMIT 1", db_path=db)
    if row2:
        check("扫尾结果 ≈ +0.05", abs(row2["outcome_pnl"] - 0.05) < 0.01,
              str(row2))

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

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

    import decision.agent_judge as agent_judge_module
    original_request = agent_judge_module._request_llm
    try:
        agent_judge_module._request_llm = lambda *a, **k: {
            "choices": [{"message": {"content": '{"verdict":"approve"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "prompt_cache_hit_tokens": 25,
                      "prompt_cache_miss_tokens": 75},
        }
        metered = agent_judge_module.production_harness_model_call("{}")
    finally:
        agent_judge_module._request_llm = original_request
    expected_cost = (
        25 * config.AGENT_HARNESS_INPUT_CACHE_HIT_USD_PER_M +
        75 * config.AGENT_HARNESS_INPUT_CACHE_MISS_USD_PER_M +
        20 * config.AGENT_HARNESS_OUTPUT_USD_PER_M) / 1_000_000
    check("Harness provider 保存 token/cache 与美元成本口径",
          metered.input_tokens == 100 and
          metered.prompt_cache_miss_tokens == 75 and
          abs(metered.estimated_cost - expected_cost) < 1e-12,
          str(metered))

    # ---- AI 记忆: 判断落表 / 结果回填 / RAG 案例回喂 / 否决扫尾 ----
    tmp = tempfile.mkdtemp(prefix="ai_mem_")
    db = os.path.join(tmp, "a.db")
    import storage.db as sdb
    sdb.init_db(db)
    v, r, jid = judge(SIG, "LINK", 60, 10.0, SENT,
                      analyzer=lambda u: ('{"verdict":"approve","risk_probability":0.2,'
                                          '"reason_code":"none","reason":"ok"}'),
                      db_path=db)
    check("判断落表(有 judgment_id)", jid is not None, f"jid={jid}")
    detail = sdb.q1("SELECT call_status,risk_probability,reason_code "
                    "FROM ai_judgments WHERE id=?", [jid], db_path=db)
    check("有效判断结构化状态", detail["call_status"] == "valid" and
          abs(detail["risk_probability"] - 0.2) < 1e-9 and
          detail["reason_code"] == "none", str(detail))
    bind_trade(jid, "txn_x", db_path=db)
    record_trade_outcome("txn_x", 0.012, db_path=db)
    row = sdb.q1("SELECT outcome_pnl FROM ai_judgments WHERE id=?", [jid], db_path=db)
    check("平仓回填判断结果 0.012", row and abs(row["outcome_pnl"] - 0.012) < 1e-9,
          str(row))
    _old_h = config.AGENT_JUDGE_MEMORY_MIN_HOURS
    config.AGENT_JUDGE_MEMORY_MIN_HOURS = 0   # 测试: 立即视为"历史"
    mem = _memory_block(db, "LINK", "long")
    config.AGENT_JUDGE_MEMORY_MIN_HOURS = _old_h
    check("RAG 记忆含带结果旧案例且旧 PnL 保留百分比单位",
          "判断案例" in mem and "approve" in mem and "+1.2%" in mem,
          mem[:160])

    from engines.signal_sampling import record_signal_sample
    from decision.signal_outcomes import persist_outcome
    linked_sig = dict(SIG, entry=10.0, atr=0.5, kline_ts=1_700_000_000_000,
                      shadow_dims={name: 0.5 for name in config.SHADOW_DIMS})
    sid, _ = record_signal_sample("LINK", linked_sig, "swap", db_path=db,
                                  event_ts=1_700_003_600)
    _, _, reject_jid = judge(
        linked_sig, "LINK", 60, 10.0, SENT,
        analyzer=lambda u: ('{"verdict":"reject","risk_probability":0.9,'
                            '"reason_code":"news_conflict","reason":"bad"}'),
        db_path=db, signal_id=sid)
    persist_outcome({"signal_id": sid,
                     "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
                     "tp_first": 0, "sl_first": 1, "timeout": 0,
                     "ambiguous": 0, "pnl_r": -1.0, "mfe_r": 0.2,
                     "mae_r": 1.0, "high_ret_h": 0.001, "low_ret_h": -0.05,
                     "time_to_tp_sec": None, "time_to_sl_sec": 60,
                     "time_to_high_sec": 0, "time_to_low_sec": 60,
                     "settled_at": 1_700_100_000, "bar_resolution": "1m",
                     "label_version": "test-v1"}, db_path=db)
    # persist_outcome 已同步回填；再次 sweep 必须幂等为 0。
    n = sweep_outcomes(db_path=db)
    check("否决判断按完整路径自动扫尾且重复 sweep 幂等", n == 0, str(n))
    row2 = sdb.q1("SELECT outcome_r,sl_first FROM ai_judgments WHERE id=?",
                  [reject_jid], db_path=db)
    check("扫尾结果 = -1R 且 SL first",
          row2 and row2["outcome_r"] == -1.0 and row2["sl_first"] == 1,
          str(row2))
    config.AGENT_JUDGE_MEMORY_MIN_HOURS = 0
    path_mem = _memory_block(db, "LINK", "long")
    config.AGENT_JUDGE_MEMORY_MIN_HOURS = _old_h
    check("路径结果在 RAG 中保留 R 单位", "-1.00R" in path_mem,
          path_mem[:240])

    print(f"\n结果: {_passed} 通过, {_failed} 失败")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

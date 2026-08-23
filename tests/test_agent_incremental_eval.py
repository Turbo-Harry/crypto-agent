"""T8 Agent 有效判断/失败拆分与反事实净增量。"""
import os
import json
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from decision.agent_evaluation import (evaluate_agent, evaluate_harness,
                                       sync_harness_lifecycle)
from interfaces.agent import stable_hash

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
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "agent.db")
        import storage.db as sdb
        import config
        from engines.signal_sampling import record_signal_sample
        sdb.init_db(db)
        for i in range(120):
            reject = i < 40
            outcome = (-1.0 if i < 30 else 2.0) if reject else (0.4 if i % 2 else -0.2)
            direction = "long" if i % 2 else "short"
            signal = {
                "dir": direction, "entry": 100.0,
                "stop": 99.0 if direction == "long" else 101.0,
                "tp": 102.0 if direction == "long" else 98.0,
                "atr": 1.0, "kline_ts": 1_700_000_000_000 + i * 900_000,
                "regime": {"tag": "high_vol" if i % 2 else "mid_vol"},
                "shadow_dims": {name: 0.5 for name in config.SHADOW_DIMS}}
            signal_id, _ = record_signal_sample(
                ("BTC", "ETH", "SOL")[i % 3], signal, "swap", db_path=db,
                event_ts=1_700_000_000 + i * 900)
            sdb.x(
                "INSERT INTO ai_judgments (ts,base,direction,verdict,reason,"
                "call_status,risk_probability,reason_code,outcome_r,outcome_ts,"
                "signal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [time.time() + i, ("BTC", "ETH", "SOL")[i % 3],
                 direction, "reject" if reject else "approve",
                 "test", "valid", 0.8 if reject else 0.2,
                 "news_conflict" if reject else "none", outcome, time.time(),
                 signal_id],
                db_path=db)
        for status in ("no_key", "timeout", "parse_error", "api_error"):
            sdb.x("INSERT INTO ai_judgments (ts,base,direction,verdict,call_status) "
                  "VALUES (?,?,?,?,?)", [time.time(), "BTC", "long", "approve", status],
                  db_path=db)
        result = evaluate_agent(db)
        check("达到 100 有效/30 reject 后才评价", result["status"] == "evaluated",
              str(result))
        check("拦亏精确率 30/40", abs(result["blocked_loss_precision"] - 0.75) < 1e-9,
              str(result))
        check("机会成本与避免损失按交易成本后 R 分开",
              abs(result["opportunity_cost_r"] - 18.0) < 1e-9 and
              abs(result["avoided_loss_r"] - 36.0) < 1e-9, str(result))
        check("Agent 相对量化基线净增量为正", result["incremental_ev_r"] > 0,
              str(result))
        check("失败状态不混入有效样本",
              result["valid_n"] == 120 and result["call_status_counts"]["timeout"] == 1,
              str(result))
        check("分方向/币种/reason 稳定性已报告",
              "direction:long" in result["stability"] and
              "reason:news_conflict" in result["stability"], str(result))

        # Harness 使用同一批固定 2:1 路径，持久化概率/reason 后自动评价；
        # 只可推进到 validated，不能自动获得 veto 权限。
        for i in range(120):
            signal_id = sdb.q1(
                "SELECT signal_id FROM signal_samples ORDER BY event_ts LIMIT 1 OFFSET ?",
                [i], db_path=db)["signal_id"]
            reject = i < 40
            pnl_r = -1.0 if i < 30 else (2.0 if reject else
                                         (0.4 if i % 2 else -0.2))
            run_id = f"harness-{i}"
            input_snapshot = {"account": {}, "signal": {}}
            sdb.x(
                "INSERT INTO agent_runs (run_id,signal_id,idempotency_key,created_ts,"
                "runtime_status,final_action,model_verdict,prompt_version,model_version,"
                "context_version,schema_version,retrieval_version,risk_probability,"
                "reason_codes,evidence_ids,input_snapshot,input_hash,estimated_cost) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, signal_id, run_id, time.time() + i, "completed",
                 "shadow_reject" if reject else "baseline_pass",
                 "reject" if reject else "approve", "p1", "m1", "c1", "s1", "r1",
                 0.75 if reject else 0.5,
                 '["liquidity_failure"]' if reject else "[]",
                 '["market:test"]' if reject else "[]",
                 json.dumps(input_snapshot), stable_hash(input_snapshot), 0.0],
                db_path=db)
            sdb.x(
                "INSERT INTO agent_evaluations (run_id,lifecycle_status,pnl_r) "
                "VALUES (?,?,?)", [run_id, "mature", pnl_r], db_path=db)
        harness = evaluate_harness(db)
        check("Harness 100/30 门按费用后路径自动评价",
              harness["status"] == "evaluated" and harness["n"] == 120 and
              harness["reject_n"] == 40 and
              harness["incremental_ev_lower_bound"] > 0, str(harness))
        check("Harness 风险概率与标准 reason code 可审计",
              harness["brier"] is not None and
              harness["reason_counts"].get("liquidity_failure") == 40 and
              "regime:high_vol" in harness["stability"],
              str(harness))
        original_init = sdb.init_db
        try:
            sdb.init_db = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("只读评价不得触发 schema 初始化/迁移"))
            readonly_ok = (evaluate_agent(db)["valid_n"] == 120 and
                           evaluate_harness(db)["n"] == 120)
        finally:
            sdb.init_db = original_init
        check("GET 消费的 Agent 评价保持只读且不暗含 schema 迁移",
              readonly_ok)
        state = sync_harness_lifecycle(db)
        check("有增量证据自动 validated 但不自动开启 veto",
              state["status"] == "validated", str(state))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

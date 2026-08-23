"""T1 候选全量留样、同 K 去重与版本身份。"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
import storage.db as sdb
from engines.signal_sampling import (config_identity, record_signal_sample,
                                     update_signal_decision)
import json

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}: {detail}")


def signal(direction="long", kline_ts=1_700_000_000_000):
    if direction == "long":
        stop, tp = 99.0, 102.0
    else:
        stop, tp = 101.0, 98.0
    return {
        "dir": direction, "entry": 100.0, "stop": stop, "tp": tp,
        "atr": 1.0, "kline_ts": kline_ts, "shadow_score": 61.0,
        "shadow_dims": {name: 0.5 + i * 0.01
                        for i, name in enumerate(config.SHADOW_DIMS)},
        "regime": "trend",
    }


def main():
    global passed, failed
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "sampling.db")
        sdb.init_db(db)
        print("== 同一根 K 幂等 ==")
        results = [record_signal_sample("BTC", signal(), "swap", db_path=db,
                                        event_ts=1_700_003_610 + i)
                   for i in range(12)]
        rows = sdb.q("SELECT * FROM signal_samples", db_path=db)
        check("12 次扫描只写 1 行", len(rows) == 1, str(len(rows)))
        check("只有首次 created=True",
              [created for _, created in results].count(True) == 1, str(results))
        check("signal_id 稳定", len({sid for sid, _ in results}) == 1)
        check("六维完整无缺失", rows[0]["missing_features"] == "[]",
              rows[0]["missing_features"])
        check("15m 候选与 4h 标签窗口已固化",
              rows[0]["timeframe"] == "15m" and
              rows[0]["horizon_hours"] == 4, str(rows[0]))
        frozen = json.loads(rows[0]["features"])
        check("候选明确冻结默认策略身份",
              frozen.get("strategy_id") == "A_pullback", str(frozen))

        print("== 身份边界 ==")
        _, short_created = record_signal_sample(
            "BTC", signal("short"), "swap", db_path=db,
            event_ts=1_700_003_700)
        _, next_k_created = record_signal_sample(
            "BTC", signal(kline_ts=1_700_003_600_000), "swap", db_path=db,
            event_ts=1_700_007_210)
        check("方向变化生成新样本", short_created)
        check("跨 K 生成新样本", next_k_created)
        old_score = config.SIGNAL_SCORE
        try:
            config.SIGNAL_SCORE = old_score + 1
            _, version_created = record_signal_sample(
                "BTC", signal(), "swap", db_path=db,
                event_ts=1_700_003_800)
        finally:
            config.SIGNAL_SCORE = old_score
        check("策略参数版本变化生成新样本", version_created)
        breakout = signal()
        breakout["strategy_id"] = config.BREAKOUT_SIGNAL_STRATEGY_ID
        breakout_sid, breakout_created = record_signal_sample(
            "BTC", breakout, "swap", db_path=db,
            event_ts=1_700_003_900)
        strategies = sdb.q(
            "SELECT strategy_id,strategy_version FROM signal_samples "
            "WHERE symbol='BTC' AND direction='long' AND kline_ts=?",
            [1_700_000_000_000], db_path=db)
        check("同币同方向同 K 的 A/B 候选可并存",
              breakout_created and len(strategies) == 3 and
              {row["strategy_id"] for row in strategies} ==
              {config.ENTRY_SIGNAL_STRATEGY_ID,
               config.BREAKOUT_SIGNAL_STRATEGY_ID}, str(strategies))
        canonical = sdb.q(
            "SELECT strategy_id,strategy_version FROM signal_samples_canonical "
            "WHERE symbol='BTC' AND direction='long' AND kline_ts=?",
            [1_700_000_000_000], db_path=db)
        check("同一自然机会的多配置快照只计最新一条",
              len(canonical) == 2 and
              {row["strategy_id"] for row in canonical} ==
              {config.ENTRY_SIGNAL_STRATEGY_ID,
               config.BREAKOUT_SIGNAL_STRATEGY_ID}, str(canonical))
        check("A/B signal_id 明确隔离", breakout_sid != results[0][0])
        b_version = config_identity(config.BREAKOUT_SIGNAL_STRATEGY_ID)[0]
        a_version = config_identity(config.ENTRY_SIGNAL_STRATEGY_ID)[0]
        old_lookback = config.BREAKOUT_LOOKBACK
        try:
            config.BREAKOUT_LOOKBACK = old_lookback + 1
            b_changed = config_identity(config.BREAKOUT_SIGNAL_STRATEGY_ID)[0]
            a_unchanged = config_identity(config.ENTRY_SIGNAL_STRATEGY_ID)[0]
        finally:
            config.BREAKOUT_LOOKBACK = old_lookback
        check("B 突破参数只改变 B 候选身份",
              b_version != b_changed and a_version == a_unchanged)
        c_version = config_identity(config.AGENT_PROPOSAL_STRATEGY_ID)[0]
        old_impl = config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION
        try:
            config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION = old_impl + "-fixture"
            c_changed = config_identity(config.AGENT_PROPOSAL_STRATEGY_ID)[0]
            a_unchanged = config_identity(config.ENTRY_SIGNAL_STRATEGY_ID)[0]
        finally:
            config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION = old_impl
        check("C 实现版本只改变 Agent 提案候选身份",
              c_version != c_changed and a_version == a_unchanged)

        sid = results[0][0]
        update_signal_decision(sid, db_path=db, rule_decision="pass",
                               ai_verdict="reject", final_decision="rejected",
                               reject_reason="ai_reject:test")
        row = sdb.q1("SELECT * FROM signal_samples WHERE signal_id=?", [sid], db)
        check("拒绝轨迹完整保留",
              row["rule_decision"] == "pass" and
              row["ai_verdict"] == "reject" and
              row["final_decision"] == "rejected",
              str(row))
        columns = {row["name"] for row in
                   sdb.q("PRAGMA table_info(factor_trials)", db_path=db)}
        trade_columns = {row["name"] for row in
                         sdb.q("PRAGMA table_info(trades)", db_path=db)}
        signal_columns = {row["name"] for row in
                          sdb.q("PRAGMA table_info(signal_samples)", db_path=db)}
        model_columns = {row["name"] for row in
                         sdb.q("PRAGMA table_info(model_artifacts)", db_path=db)}
        agent_version_columns = {row["name"] for row in
                                 sdb.q("PRAGMA table_info(agent_versions)",
                                       db_path=db)}
        canonical_view = sdb.q1(
            "SELECT name FROM sqlite_master WHERE type='view' "
            "AND name='signal_samples_canonical'", db_path=db)
        check("数据库 v31 包含策略隔离与自然机会 canonical 视图",
              sdb.SCHEMA_VERSION >= 31 and canonical_view is not None and
              "strategy_id" in signal_columns and
              "strategy_id" in model_columns and
              "strategy_id" in trade_columns and
              "strategy_id" in agent_version_columns and
              {"strategy_id", "timeframe", "horizon_hours"} <= columns,
              str(sdb.SCHEMA_VERSION))
        check("时间退出字段已迁移",
              {"strategy_timeframe", "max_hold_hours"} <= trade_columns,
              str(trade_columns))

    print(f"\n结果: {passed} 通过, {failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

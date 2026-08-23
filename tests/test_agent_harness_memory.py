import tempfile
import unittest

from decision.agent_memory import retrieve_for_input
from decision.agent_contracts import (AgentInput, FinalAction, HarnessRun,
                                      LifecycleStatus, RuntimeStatus, Verdict)
from engines.signal_sampling import record_signal_sample
from storage import agent_harness, agent_memory, db


class AgentHarnessMemoryTest(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = f.name
        f.close()
        db.init_db(self.path)

    def tearDown(self):
        import os
        os.unlink(self.path)

    def test_pending_is_excluded_and_retrieval_is_diverse(self):
        agent_memory.upsert_memory(memory_type="episodic", source_id="pending", content="pending",
                                   status="pending", base="BTC", direction="long", db_path=self.path)
        agent_memory.upsert_memory(memory_type="episodic", source_id="a", content="a", status="mature",
                                   evidence_strength=0.8, base="BTC", direction="long", regime="trend",
                                   db_path=self.path)
        agent_memory.upsert_memory(memory_type="episodic", source_id="b", content="b", status="mature",
                                   evidence_strength=0.7, base="BTC", direction="long", regime="trend",
                                   db_path=self.path)
        agent_memory.upsert_memory(memory_type="episodic", source_id="c", content="c", status="mature",
                                   evidence_strength=0.6, base="ETH", direction="long", regime="trend",
                                   db_path=self.path)
        rows = agent_memory.retrieve({"base": "BTC", "direction": "long", "regime": "trend"},
                                     limit=5, db_path=self.path)
        ids = {r["evidence_id"] for r in rows}
        self.assertNotIn(agent_memory._evidence_id("episodic", "pending"), ids)
        self.assertLessEqual(sum(r.get("base") == "BTC" for r in rows), 1)
        self.assertTrue(any(r["memory_type"] == "procedural" for r in rows))

    def test_legacy_only_imports_settled_rows(self):
        db.x("INSERT INTO ai_judgments (ts,base,direction,verdict,reason,outcome_pnl) VALUES (?,?,?,?,?,?)",
             [1, "BTC", "long", "reject", "bad", -0.1], db_path=self.path)
        db.x("INSERT INTO ai_judgments (ts,base,direction,verdict,reason) VALUES (?,?,?,?,?)",
             [1, "ETH", "long", "approve", "pending"], db_path=self.path)
        self.assertEqual(agent_memory.promote_mature_legacy_memories(db_path=self.path, now_ts=100000,
                                                                       min_age_hours=0), 1)
        rows = db.q("SELECT * FROM agent_memories WHERE memory_type='episodic'", db_path=self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(agent_memory.promote_mature_legacy_memories(
            db_path=self.path, now_ts=100000, min_age_hours=0), 0)

    def test_path_r_memory_inherits_signal_scope(self):
        signal_id, _ = record_signal_sample(
            "BTC", {"dir": "long", "kline_ts": 1_000,
                    "entry": 100, "stop": 99, "tp": 102, "atr": 1,
                    "shadow_dims": {}, "regime": "trend"}, "swap",
            db_path=self.path, event_ts=1)
        db.x("INSERT INTO ai_judgments "
             "(ts,base,direction,verdict,reason,signal_id,outcome_r,outcome_ts) "
             "VALUES (?,?,?,?,?,?,?,?)",
             [1, "BTC", "long", "reject", "path risk", signal_id, -1.0, 2],
             db_path=self.path)
        self.assertEqual(agent_memory.promote_mature_legacy_memories(
            db_path=self.path, now_ts=100000, min_age_hours=0), 1)
        row = db.q1("SELECT * FROM agent_memories WHERE signal_id=?",
                    [signal_id], db_path=self.path)
        sample = db.q1("SELECT strategy_version,timeframe FROM signal_samples "
                       "WHERE signal_id=?", [signal_id], db_path=self.path)
        self.assertEqual(row["outcome_r"], -1.0)
        self.assertIsNone(row["outcome_pnl"])
        self.assertEqual(row["strategy_version"], sample["strategy_version"])
        self.assertEqual(row["timeframe"], "15m")
        self.assertIn('"outcome_unit": "R"', row["metadata_json"])

    def test_mature_harness_evaluation_becomes_scoped_memory(self):
        signal_id, _ = record_signal_sample(
            "ETH", {"dir": "short", "kline_ts": 1_000,
                    "entry": 100, "stop": 101, "tp": 98, "atr": 1,
                    "shadow_dims": {}, "regime": "range"}, "swap",
            db_path=self.path, event_ts=1)
        agent_input = AgentInput(
            run_id="run-memory", signal_id=signal_id, event_ts="1",
            kline_ts="1000", strategy_version="strategy-v1",
            prompt_version="judge-v1", model_version="model-v1",
            context_version="context-v1", schema_version="schema-v1",
            retrieval_version="retrieval-v1")
        run = HarnessRun(
            run_id="run-memory", signal_id=signal_id,
            runtime_status=RuntimeStatus.COMPLETED,
            final_action=FinalAction.SHADOW_REJECT,
            model_verdict=Verdict.REJECT)
        agent_harness.record_run(run, agent_input, created_ts=1,
                                 db_path=self.path)
        agent_harness.record_evaluation(
            run.run_id, lifecycle_status=LifecycleStatus.MATURE,
            label="sl_first", settle_ts=2, pnl_r=-1.0, mfe_r=.1, mae_r=1.0,
            db_path=self.path)
        self.assertEqual(agent_memory.promote_mature_harness_memories(
            db_path=self.path, now_ts=100000, min_age_hours=0), 1)
        row = db.q1("SELECT * FROM agent_memories WHERE run_id=?",
                    [run.run_id], db_path=self.path)
        sample = db.q1("SELECT strategy_version,timeframe FROM signal_samples "
                       "WHERE signal_id=?", [signal_id], db_path=self.path)
        self.assertEqual(row["outcome_r"], -1.0)
        self.assertEqual(row["strategy_version"], sample["strategy_version"])
        self.assertEqual(row["timeframe"], "15m")
        self.assertEqual(agent_memory.promote_mature_harness_memories(
            db_path=self.path, now_ts=100000, min_age_hours=0), 0)

    def test_decay_demotes_without_deleting_and_retrieval_excludes_stale(self):
        evidence_id = agent_memory.upsert_memory(
            memory_type="episodic", source_id="old", content="old lesson", status="mature",
            created_ts=1, mature_ts=1, evidence_strength=.9, base="BTC", direction="long",
            db_path=self.path)
        changed = agent_memory.decay_memories(
            episodic_ttl_days=1, semantic_ttl_days=180, min_strength=.2,
            now_ts=3 * 86400, db_path=self.path)
        self.assertEqual(changed, 1)
        row = db.q1("SELECT status,content FROM agent_memories WHERE evidence_id=?",
                    [evidence_id], db_path=self.path)
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["content"], "old lesson")
        self.assertFalse(any(x["evidence_id"] == evidence_id for x in agent_memory.retrieve(
            {"base": "BTC", "direction": "long"}, db_path=self.path)))

    def test_stale_legacy_evidence_cannot_be_reactivated_by_reimport(self):
        db.x("INSERT INTO ai_judgments (ts,base,direction,verdict,reason,outcome_pnl) VALUES (?,?,?,?,?,?)",
             [1, "BTC", "long", "reject", "old", -0.1], db_path=self.path)
        evidence_id = agent_memory._evidence_id("episodic", "ai_judgment:1")
        agent_memory.upsert_memory(memory_type="episodic", source_id="ai_judgment:1",
                                   content="old", status="stale", created_ts=1,
                                   evidence_strength=.9, db_path=self.path)
        self.assertEqual(agent_memory.promote_mature_legacy_memories(
            db_path=self.path, now_ts=100000, min_age_hours=0), 0)
        self.assertEqual(db.q1("SELECT status FROM agent_memories WHERE evidence_id=?",
                               [evidence_id], db_path=self.path)["status"], "stale")


if __name__ == "__main__":
    unittest.main()

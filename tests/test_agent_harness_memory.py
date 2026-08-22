import tempfile
import unittest

from decision.agent_memory import retrieve_for_input
from decision.agent_contracts import AgentInput
from storage import agent_memory, db


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


if __name__ == "__main__":
    unittest.main()

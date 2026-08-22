import tempfile
import unittest

from decision import agent_lifecycle
from storage import db


class AgentHarnessLifecycleTest(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = f.name
        f.close()
        db.init_db(self.path)

    def tearDown(self):
        import os
        os.unlink(self.path)

    def test_conservative_state_machine(self):
        agent_lifecycle.register("h2", db_path=self.path)
        self.assertEqual(agent_lifecycle.get("h2", db_path=self.path)["status"], "candidate")
        from storage.agent_lifecycle import transition
        transition("h2", "shadow", db_path=self.path)
        metrics = {"n": 100, "reject_n": 30, "incremental_ev_lower_bound": .1,
                   "max_segment_share": .5}
        agent_lifecycle.validate("h2", metrics, db_path=self.path)
        agent_lifecycle.activate("h2", db_path=self.path)
        self.assertEqual(agent_lifecycle.get("h2", db_path=self.path)["status"], "active-veto")
        agent_lifecycle.observe("h2", {"incremental_ev": -1}, db_path=self.path)
        self.assertEqual(agent_lifecycle.get("h2", db_path=self.path)["status"], "rolled-back")

    def test_sample_gate_and_illegal_transition(self):
        ok, reason = agent_lifecycle.promotion_ready({"n": 99, "reject_n": 30,
                                                       "incremental_ev_lower_bound": .1})
        self.assertFalse(ok)
        self.assertIn("n<100", reason)
        agent_lifecycle.register("h3", db_path=self.path)
        with self.assertRaises(ValueError):
            agent_lifecycle.activate("h3", db_path=self.path)


if __name__ == "__main__":
    unittest.main()

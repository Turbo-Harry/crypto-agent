import tempfile
import unittest

import config
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
        metrics = {
            "n": 100, "reject_n": 30, "incremental_ev_lower_bound": .1,
            "max_segment_share": .5, "model_cost_data_complete": True,
            "trace_coverage": 1.0, "probability_coverage": 1.0,
            "reject_evidence_coverage": 1.0, "brier_skill": .1,
            "saved_loss": 2.0, "missed_profit": .5, "model_cost_r": .01,
        }
        agent_lifecycle.validate("h2", metrics, db_path=self.path)
        agent_lifecycle.activate("h2", db_path=self.path)
        active = agent_lifecycle.get("h2", db_path=self.path)
        self.assertEqual(active["status"], "active-veto")
        self.assertEqual(__import__("json").loads(active["metrics_json"]), metrics)
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

    def test_promotion_requires_cost_trace_calibration_and_evidence(self):
        base = {
            "n": 100, "reject_n": 30, "incremental_ev_lower_bound": .1,
            "max_segment_share": .5, "trace_coverage": 1.0,
            "probability_coverage": 1.0, "reject_evidence_coverage": 1.0,
            "brier_skill": .1, "saved_loss": 2.0,
            "missed_profit": .5, "model_cost_r": .01,
        }
        ok, reason = agent_lifecycle.promotion_ready(base)
        self.assertFalse(ok)
        self.assertEqual(reason, "model_cost_incomplete")
        costed = dict(base, model_cost_data_complete=True)
        bad_brier = dict(costed, brier_skill=-.01)
        self.assertEqual(agent_lifecycle.promotion_ready(bad_brier)[1],
                         "brier_worse_than_frequency_baseline")
        bad_evidence = dict(costed, reject_evidence_coverage=.9)
        self.assertEqual(agent_lifecycle.promotion_ready(bad_evidence)[1],
                         "reject_evidence_coverage<1")

    def test_versions_are_strategy_scoped(self):
        agent_lifecycle.register(
            "b-harness", strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID,
            db_path=self.path)
        self.assertIsNone(agent_lifecycle.get("b-harness", db_path=self.path))
        breakout = agent_lifecycle.get(
            "b-harness", strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID,
            db_path=self.path)
        self.assertEqual(breakout["strategy_id"],
                         config.BREAKOUT_SIGNAL_STRATEGY_ID)

    def test_veto_effective_only_for_promoted_matching_version(self):
        metrics = {
            "n": 100, "reject_n": 30, "incremental_ev_lower_bound": .1,
            "max_segment_share": .5, "model_cost_data_complete": True,
            "trace_coverage": 1.0, "probability_coverage": 1.0,
            "reject_evidence_coverage": 1.0, "brier_skill": .1,
            "saved_loss": 2.0, "missed_profit": .5, "model_cost_r": .01,
        }
        agent_lifecycle.register("ready", db_path=self.path)
        from storage.agent_lifecycle import transition
        transition("ready", "shadow", db_path=self.path)
        self.assertFalse(agent_lifecycle.veto_effective(
            "ready", db_path=self.path))
        agent_lifecycle.validate("ready", metrics, db_path=self.path)
        self.assertFalse(agent_lifecycle.veto_effective(
            "ready", db_path=self.path))
        agent_lifecycle.activate("ready", db_path=self.path)
        self.assertTrue(agent_lifecycle.veto_effective(
            "ready", db_path=self.path))
        self.assertFalse(agent_lifecycle.veto_effective(
            "different", db_path=self.path))


if __name__ == "__main__":
    unittest.main()

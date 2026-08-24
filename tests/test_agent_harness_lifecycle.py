import tempfile
import unittest
from unittest.mock import patch

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
            "probability_std": .1,
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
            "probability_std": .1,
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
        constant = dict(costed, probability_std=0.0)
        self.assertEqual(agent_lifecycle.promotion_ready(constant)[1],
                         "probability_resolution_too_low")

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
            "probability_std": .1,
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

    def test_shadow_metrics_refresh_without_granting_authority(self):
        from decision.agent_evaluation import sync_harness_lifecycle

        first = {
            "status": "insufficient_data", "version": "growing-v1",
            "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
            "n": 5, "reject_n": 0,
        }
        grown = dict(first, n=34)
        with patch("decision.agent_evaluation.evaluate_harness",
                   side_effect=[first, grown]):
            initial = sync_harness_lifecycle(db_path=self.path)
            refreshed = sync_harness_lifecycle(db_path=self.path)

        stored = agent_lifecycle.get("growing-v1", db_path=self.path)
        metrics = __import__("json").loads(stored["metrics_json"])
        self.assertEqual(initial["status"], "shadow")
        self.assertEqual(refreshed["status"], "shadow")
        self.assertEqual(metrics["n"], 34)
        self.assertEqual(stored["reason"], "shadow_metrics_refresh")
        self.assertFalse(agent_lifecycle.veto_effective(
            "growing-v1", db_path=self.path))

    def test_validated_version_rolls_back_when_new_metrics_degrade(self):
        from decision.agent_evaluation import sync_harness_lifecycle

        ready = {
            "status": "evaluated", "version": "validated-v1",
            "strategy_id": config.ENTRY_SIGNAL_STRATEGY_ID,
            "n": 100, "reject_n": 30,
            "model_cost_data_complete": True, "trace_coverage": 1.0,
            "probability_coverage": 1.0, "probability_std": .1,
            "reject_evidence_coverage": 1.0, "brier_skill": .1,
            "saved_loss": 2.0, "missed_profit": .5,
            "model_cost_r": .01, "incremental_ev_lower_bound": .1,
            "max_segment_share": .5,
        }
        degraded = dict(ready, brier_skill=-.01)
        with patch.object(config, "AGENT_HARNESS_VETO_ENABLED", False), \
                patch("decision.agent_evaluation.evaluate_harness",
                      side_effect=[ready, degraded]):
            validated = sync_harness_lifecycle(db_path=self.path)
            rolled = sync_harness_lifecycle(db_path=self.path)

        stored = agent_lifecycle.get("validated-v1", db_path=self.path)
        metrics = __import__("json").loads(stored["metrics_json"])
        self.assertEqual(validated["status"], "validated")
        self.assertEqual(rolled["status"], "rolled-back")
        self.assertEqual(metrics["brier_skill"], -.01)
        self.assertEqual(stored["reason"],
                         "brier_worse_than_frequency_baseline")

    def test_breakout_lifecycle_can_validate_but_never_auto_activate(self):
        from decision.agent_evaluation import sync_harness_lifecycle

        ready = {
            "status": "evaluated", "version": "breakout-ready-v1",
            "strategy_id": config.BREAKOUT_SIGNAL_STRATEGY_ID,
            "n": 100, "reject_n": 30,
            "model_cost_data_complete": True, "trace_coverage": 1.0,
            "probability_coverage": 1.0, "probability_std": .1,
            "reject_evidence_coverage": 1.0, "brier_skill": .1,
            "saved_loss": 2.0, "missed_profit": .5,
            "model_cost_r": .01, "incremental_ev_lower_bound": .1,
            "max_segment_share": .5,
        }
        with patch("decision.agent_evaluation.evaluate_harness",
                   return_value=ready):
            state = sync_harness_lifecycle(
                db_path=self.path,
                strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID,
                allow_activation=False)

        stored = agent_lifecycle.get(
            "breakout-ready-v1",
            strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID,
            db_path=self.path)
        self.assertEqual(state["status"], "validated")
        self.assertEqual(stored["status"], "validated")
        self.assertFalse(agent_lifecycle.veto_effective(
            "breakout-ready-v1",
            strategy_id=config.BREAKOUT_SIGNAL_STRATEGY_ID,
            db_path=self.path))

    def test_batch_sync_includes_a_and_b_with_strategy_scoped_authority(self):
        from decision.agent_evaluation import sync_harness_lifecycles

        def _result(*, db_path, strategy_id, allow_activation):
            return {"status": "shadow", "version": strategy_id,
                    "allow_activation": allow_activation}

        with patch("decision.agent_evaluation.sync_harness_lifecycle",
                   side_effect=_result) as sync_one:
            states = sync_harness_lifecycles(db_path=self.path)

        self.assertEqual(set(states), {
            config.ENTRY_SIGNAL_STRATEGY_ID,
            config.BREAKOUT_SIGNAL_STRATEGY_ID})
        self.assertTrue(states[config.ENTRY_SIGNAL_STRATEGY_ID]
                        ["allow_activation"])
        self.assertFalse(states[config.BREAKOUT_SIGNAL_STRATEGY_ID]
                         ["allow_activation"])
        self.assertEqual(sync_one.call_count, 2)


if __name__ == "__main__":
    unittest.main()

import unittest

from decision.agent_evaluation import (
    brier_score, compare_same_inputs, evaluate_path, incremental_ev, summarize,
)


class AgentHarnessEvaluationTest(unittest.TestCase):
    def test_first_touch_and_ambiguous_paths(self):
        tp = evaluate_path(entry=100, stop=95, target=110, direction="long",
                           path=[(1, 102), (2, 111), (3, 90)])
        self.assertEqual(tp.label, "tp_first")
        sl = evaluate_path(entry=100, stop=95, target=110, direction="long",
                           path=[(1, 97), (2, 94), (3, 111)])
        self.assertEqual(sl.label, "sl_first")
        amb = evaluate_path(entry=100, stop=95, target=110, direction="long",
                            path=[(1, 110)])
        self.assertEqual(amb.label, "tp_first")

    def test_metrics_and_opportunity_cost(self):
        rows = [
            {"verdict": "reject", "pnl_r": -1, "base": "BTC", "direction": "long", "regime": "trend", "risk_probability": .8},
            {"verdict": "reject", "pnl_r": 2, "base": "ETH", "direction": "long", "regime": "trend", "risk_probability": .2},
            {"verdict": "approve", "pnl_r": -1, "base": "SOL", "direction": "short", "regime": "range", "risk_probability": .7},
        ]
        metrics = summarize(rows, model_cost=.1)
        self.assertEqual(metrics.reject_n, 2)
        self.assertAlmostEqual(metrics.incremental_ev, -1.1)
        self.assertAlmostEqual(incremental_ev(saved_loss=1, missed_profit=2, model_cost=.1), -1.1)
        self.assertIsNotNone(brier_score([.0, 1.0], [False, True]))

    def test_challenger_uses_same_inputs_only(self):
        result = compare_same_inputs(
            [{"input_hash": "a", "verdict": "approve"}, {"input_hash": "unpaired", "verdict": "reject"}],
            [{"input_hash": "a", "verdict": "reject"}, {"input_hash": "b", "verdict": "approve"}],
        )
        self.assertEqual(result["paired_n"], 1)
        self.assertEqual(result["disagreements"], 1)


if __name__ == "__main__":
    unittest.main()

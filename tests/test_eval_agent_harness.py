import json
import tempfile
import unittest

from tools.eval_agent_harness import build_report, main


class EvalAgentHarnessTest(unittest.TestCase):
    def test_report_contains_metrics_and_pairing(self):
        rows = [{"input_hash": "a", "verdict": "reject", "pnl_r": -1,
                 "base": "BTC", "direction": "long", "regime": "trend",
                 "risk_probability": .8}]
        report = build_report(rows, model_cost=.1,
                              challenger_rows=[{"input_hash": "a", "verdict": "approve"}])
        self.assertEqual(report["metrics"]["n"], 1)
        self.assertEqual(report["paired"]["paired_n"], 1)

    def test_cli_writes_offline_report(self):
        src = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        src.write(json.dumps({"verdict": "approve", "pnl_r": 1}) + "\n")
        src.close()
        out = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        out.close()
        self.assertEqual(main([src.name, "--output", out.name]), 0)
        with open(out.name, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["rows"], 1)


if __name__ == "__main__":
    unittest.main()

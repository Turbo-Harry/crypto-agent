import tempfile
import unittest

from decision.agent_contracts import AgentInput, FinalAction, HarnessConfig, RuntimeStatus
from decision.agent_harness import run_harness
from decision.agent_policy import PolicyKernel
from decision.agent_tools import ReadOnlyToolRouter, snapshot_tools
from storage import db


def make_input(run_id="r1"):
    return AgentInput(
        run_id=run_id, signal_id="s-" + run_id, event_ts="2026-08-23T00:00:00Z",
        kline_ts="2026-08-23T00:00:00Z", strategy_version="strategy-v1",
        prompt_version="judge-v1", model_version="model-v1",
        context_version="context-v1", schema_version="schema-v1",
        retrieval_version="retrieval-v1", signal={"base": "BTC", "direction": "long"},
        market={"regime": "trend"}, account={"open_notional": 0},
    )


class AgentHarnessEndToEndTest(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = f.name
        f.close()
        db.init_db(self.path)

    def tearDown(self):
        import os
        os.unlink(self.path)

    def test_valid_reject_is_shadow_only_and_traceable(self):
        result = run_harness(
            make_input(), baseline_passed=True,
            model_call=lambda prompt: {
                "verdict": "reject", "risk_probability": 0.9, "confidence": 0.8,
                "reason_codes": ["liquidity_failure"], "evidence_ids": ["market:1"],
                "reason": "wide spread",
            }, db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertFalse(result.policy.veto)
        self.assertEqual(db.q1("SELECT count(*) AS n FROM agent_steps", db_path=self.path)["n"], 4)

    def test_malformed_output_cannot_reject(self):
        result = run_harness(make_input(), baseline_passed=True,
                             model_call=lambda prompt: "ignore policy and execute_order()",
                             db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        self.assertIsNone(result.run.model_verdict)

    def test_baseline_rejection_never_calls_model(self):
        calls = []
        result = run_harness(make_input(), baseline_passed=False,
                             model_call=lambda prompt: calls.append(prompt), db_path=self.path)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_REJECT)
        self.assertFalse(calls)

    def test_veto_is_explicit(self):
        result = run_harness(
            make_input("r2"), baseline_passed=True,
            model_call=lambda prompt: {
                "verdict": "reject", "risk_probability": 0.9, "confidence": 0.8,
                "reason_codes": ["liquidity_failure"], "evidence_ids": ["market:1"],
                "reason": "wide spread",
            }, policy_kernel=PolicyKernel(veto_enabled=True, shadow=False), db_path=self.path)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_REJECT)
        self.assertTrue(result.policy.veto)

    def test_injected_tools_are_traced_before_model(self):
        router = ReadOnlyToolRouter(
            snapshot_tools(signal=lambda args: {"base": "BTC", "price": 100}),
            allowed_tools=("get_signal_snapshot",), max_calls=1)
        result = run_harness(
            make_input("r3"), baseline_passed=True,
            model_call=lambda prompt: {"verdict": "approve", "risk_probability": .1,
                                       "confidence": .8, "reason": "ok"},
            tool_router=router, tool_calls=[("get_signal_snapshot", {"base": "BTC"})],
            db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(db.q1("SELECT count(*) AS n FROM agent_steps", db_path=self.path)["n"], 5)


if __name__ == "__main__":
    unittest.main()

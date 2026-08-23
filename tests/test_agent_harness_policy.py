import unittest

from decision.agent_contracts import AgentDecision, FinalAction, ReasonCode, RuntimeStatus, Verdict
from decision.agent_policy import PolicyKernel


def reject():
    return AgentDecision(Verdict.REJECT, 0.9, 0.8,
                         (ReasonCode.LIQUIDITY_FAILURE,), ("market:1",), reason="wide spread")


class AgentHarnessPolicyTest(unittest.TestCase):
    def test_baseline_hard_gate_cannot_be_reopened(self):
        result = PolicyKernel(veto_enabled=True, shadow=False).evaluate(
            baseline_passed=False, runtime_status=RuntimeStatus.COMPLETED, decision=reject())
        self.assertEqual(result.final_action, FinalAction.BASELINE_REJECT)
        self.assertFalse(result.veto)

    def test_reject_is_shadow_only_by_default(self):
        result = PolicyKernel().evaluate(
            baseline_passed=True, runtime_status=RuntimeStatus.COMPLETED, decision=reject())
        self.assertEqual(result.final_action, FinalAction.SHADOW_REJECT)
        self.assertFalse(result.veto)

    def test_veto_requires_explicit_enablement(self):
        result = PolicyKernel(veto_enabled=True, shadow=False).evaluate(
            baseline_passed=True, runtime_status=RuntimeStatus.COMPLETED, decision=reject())
        self.assertEqual(result.final_action, FinalAction.AGENT_REJECT)
        self.assertTrue(result.veto)

    def test_veto_requires_calibrated_risk_and_confidence(self):
        weak = AgentDecision(
            Verdict.REJECT, .9, .6, (ReasonCode.LIQUIDITY_FAILURE,),
            ("market:1",), reason="weak confidence")
        result = PolicyKernel(
            veto_enabled=True, shadow=True,
            min_reject_risk=.7, min_reject_confidence=.7).evaluate(
                baseline_passed=True, runtime_status=RuntimeStatus.COMPLETED,
                decision=weak)
        self.assertFalse(result.veto)
        self.assertEqual(result.final_action, FinalAction.SHADOW_REJECT)

    def test_runtime_failure_is_baseline_pass_not_model_approve(self):
        result = PolicyKernel(veto_enabled=True).evaluate(
            baseline_passed=True, runtime_status=RuntimeStatus.TIMEOUT, decision=None)
        self.assertEqual(result.final_action, FinalAction.BASELINE_PASS)
        self.assertFalse(result.veto)


if __name__ == "__main__":
    unittest.main()

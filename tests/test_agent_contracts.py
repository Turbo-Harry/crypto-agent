import json
import unittest

from decision.agent_contracts import (
    AgentDecision,
    AgentInput,
    FinalAction,
    PolicyContext,
    ReasonCode,
    RuntimeStatus,
    Verdict,
    apply_policy,
    idempotency_key,
    strict_parse_model_output,
)


class AgentContractsTest(unittest.TestCase):
    def test_input_hash_is_order_independent(self):
        left = AgentInput(
            run_id="r1", signal_id="s1", event_ts="1", kline_ts="1",
            strategy_version="s", prompt_version="p", model_version="m",
            context_version="c", schema_version="v", retrieval_version="q",
            signal={"b": 2, "a": 1},
        )
        right = AgentInput(
            run_id="r1", signal_id="s1", event_ts="1", kline_ts="1",
            strategy_version="s", prompt_version="p", model_version="m",
            context_version="c", schema_version="v", retrieval_version="q",
            signal={"a": 1, "b": 2},
        )
        self.assertEqual(left.input_hash, right.input_hash)
        self.assertEqual(idempotency_key("s1", "h1"), idempotency_key("s1", "h1"))

    def test_reject_requires_reason_and_evidence(self):
        with self.assertRaises(ValueError):
            AgentDecision(Verdict.REJECT, 0.8, 0.9)

    def test_strict_parser_accepts_valid_json(self):
        decision = strict_parse_model_output(json.dumps({
            "verdict": "reject",
            "risk_probability": 0.8,
            "confidence": 0.9,
            "reason_codes": [ReasonCode.LIQUIDITY_FAILURE.value],
            "evidence_ids": ["market:1"],
            "reason": "spread is too wide",
        }))
        self.assertEqual(decision.verdict, Verdict.REJECT)

    def test_policy_never_turns_runtime_failure_into_model_approval(self):
        action = apply_policy(PolicyContext(
            baseline_passed=True,
            model_enabled=True,
            model_verdict=None,
            runtime_status=RuntimeStatus.TIMEOUT,
        ))
        self.assertEqual(action, FinalAction.BASELINE_PASS)

    def test_baseline_reject_wins(self):
        action = apply_policy(PolicyContext(
            baseline_passed=False,
            model_enabled=True,
            model_verdict=Verdict.APPROVE,
            runtime_status=RuntimeStatus.COMPLETED,
        ))
        self.assertEqual(action, FinalAction.BASELINE_REJECT)


if __name__ == "__main__":
    unittest.main()

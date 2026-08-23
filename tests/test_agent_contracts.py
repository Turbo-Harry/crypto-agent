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

    def test_evidence_hash_pairs_versions_but_input_hash_keeps_identity(self):
        base = dict(
            run_id="r1", signal_id="s1", event_ts="1", kline_ts="1",
            strategy_version="strategy-v1", prompt_version="p1",
            model_version="m1", context_version="c1",
            schema_version="s1", retrieval_version="r1",
            signal={"base": "BTC", "direction": "long"})
        champion = AgentInput(**base)
        challenger = AgentInput(**dict(
            base, run_id="r2", prompt_version="p2", model_version="m2"))
        self.assertNotEqual(champion.input_hash, challenger.input_hash)
        self.assertEqual(champion.evidence_hash, challenger.evidence_hash)
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

    def test_insufficient_evidence_requires_concrete_missing_market_data(self):
        with self.assertRaisesRegex(
                ValueError, "requires concrete missing_information"):
            strict_parse_model_output({
                "verdict": "abstain", "risk_probability": .55,
                "confidence": .6,
                "reason_codes": [ReasonCode.INSUFFICIENT_EVIDENCE.value],
                "missing_information": [],
                "abstain_reason": "not enough evidence",
            })

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

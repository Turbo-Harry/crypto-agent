"""Single LangGraph/LangChain Harness runtime and safety boundary tests."""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from decision.agent_contracts import (
    AgentInput, FinalAction, HarnessConfig, ModelCallResult, RuntimeStatus,
)
from decision.agent_graph import build_harness_graph, run_graph_harness
from decision.agent_harness import run_harness
from decision.agent_policy import PolicyKernel
from decision.agent_tools import ReadOnlyToolRouter, snapshot_tools
from storage import db


def make_input(run_id: str) -> AgentInput:
    return AgentInput(
        run_id=run_id, signal_id="signal-" + run_id,
        event_ts="2026-08-23T00:00:00Z",
        kline_ts="2026-08-23T00:00:00Z",
        strategy_version="strategy-v1", prompt_version="prompt-v1",
        model_version="model-v1", context_version="context-v2-langgraph",
        schema_version="schema-v1", retrieval_version="retrieval-v1",
        signal={"base": "BTC", "direction": "long"},
        market={"regime": "trend"})


def approve(_prompt: str):
    return {"verdict": "approve", "risk_probability": 0.1,
            "confidence": 0.8, "reason": "evidence aligned"}


class AgentLangGraphRuntimeTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = handle.name
        handle.close()
        db.init_db(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_graph_has_explicit_deterministic_nodes(self):
        graph = build_harness_graph(
            model_call=approve, config=HarnessConfig(),
            policy_kernel=PolicyKernel(), enabled=True, db_path=self.path,
            memory_limit=5, tool_router=None, tool_calls=None)
        names = set(graph.get_graph().nodes)
        self.assertTrue({"context", "retrieve", "tools", "model", "validate",
                         "policy", "record"}.issubset(names))

    def test_compatibility_entry_has_one_runtime_for_paper_and_live(self):
        source = inspect.getsource(__import__(
            "decision.agent_harness", fromlist=["run_harness"]))
        self.assertNotIn("LIVE_MODE", source)
        self.assertNotIn("CRYPTO_AGENT_MODE", source)
        self.assertIn("run_graph_harness", source)
        result = run_harness(
            make_input("shared-runtime"), baseline_passed=True,
            model_call=approve, db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)

    def test_langchain_structured_output_remains_fail_closed(self):
        result = run_graph_harness(
            make_input("bad-json"), baseline_passed=True,
            model_call=lambda _prompt: "```json\n{\"verdict\":\"reject\"}\n```",
            db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        self.assertIsNone(result.decision)

    def test_semantic_violation_gets_one_traced_repair(self):
        calls = []

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                content = {
                    "verdict": "abstain", "risk_probability": .55,
                    "confidence": .6,
                    "reason_codes": ["insufficient_evidence"],
                    "missing_information": [],
                    "abstain_reason": "预测未校准且无已验证模型",
                    "reason": "not enough evidence",
                }
            else:
                content = {
                    "verdict": "abstain", "risk_probability": .62,
                    "confidence": .52,
                    "reason_codes": ["insufficient_evidence"],
                    "missing_information": ["current order-book depth"],
                    "abstain_reason": "frozen liquidity evidence is incomplete",
                    "reason": "liquidity loss risk cannot be resolved",
                }
            return ModelCallResult(
                content=content, input_tokens=10, output_tokens=2,
                estimated_cost=.001, pricing_version="price-v1")

        result = run_graph_harness(
            make_input("semantic-repair"), baseline_passed=True,
            model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn('"semantic_repair"', calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)
        self.assertEqual(result.run.risk_probability, .62)
        self.assertEqual(result.run.input_tokens, 20)
        self.assertEqual(result.run.output_tokens, 4)
        self.assertAlmostEqual(result.run.estimated_cost, .002)
        model_steps = db.q(
            "SELECT status,retry_count,error_type FROM agent_steps "
            "WHERE run_id=? AND step_type='model' ORDER BY step_no",
            [result.run.run_id], db_path=self.path)
        self.assertEqual(
            [(row["status"], row["retry_count"]) for row in model_steps],
            [("failed", 0), ("completed", 1)])
        self.assertIn("AgentSemanticError", model_steps[0]["error_type"])

    def test_semantic_retry_exhaustion_is_schema_error(self):
        calls = []

        def invalid(prompt):
            calls.append(prompt)
            return {
                "verdict": "abstain", "risk_probability": .55,
                "confidence": .6,
                "reason_codes": ["insufficient_evidence"],
                "missing_information": [],
                "abstain_reason": "缺少已验证模型",
                "reason": "not enough evidence",
            }

        result = run_graph_harness(
            make_input("semantic-exhausted"), baseline_passed=True,
            model_call=invalid, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        self.assertIsNone(result.decision)

    def test_v6_high_risk_high_confidence_abstain_repairs_to_reject(self):
        calls = []
        inp = replace(
            make_input("v6-verdict-threshold"),
            prompt_version="harness-risk-v6-outcome-first-evidence-update",
            field_provenance={"market": "signal:v6-verdict-threshold:market"})

        def model(prompt):
            calls.append(prompt)
            verdict = "abstain" if len(calls) == 1 else "reject"
            return {
                "verdict": verdict, "risk_probability": .8,
                "confidence": .8,
                "reason_codes": (["signal_inconsistency"]
                                 if verdict == "reject" else []),
                "evidence_ids": (["signal:v6-verdict-threshold:market"]
                                 if verdict == "reject" else []),
                "missing_information": [],
                "abstain_reason": ("mixed market evidence"
                                   if verdict == "abstain" else None),
                "reason": "frozen market evidence implies high loss risk",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("high-risk high-confidence", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v6_governance_wording_variant_cannot_justify_abstain(self):
        calls = []

        def invalid(_prompt):
            calls.append(1)
            return {
                "verdict": "abstain", "risk_probability": .55,
                "confidence": .5, "reason_codes": ["insufficient_evidence"],
                "evidence_ids": [],
                "missing_information": ["缺乏已验证的入场模型正期望证据"],
                "abstain_reason": "缺乏已验证的入场概率模型",
                "reason": "governance state is not market evidence",
            }

        result = run_graph_harness(
            replace(make_input("v6-governance"),
                    prompt_version="harness-risk-v6-outcome-first-evidence-update"),
            baseline_passed=True, model_call=invalid, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)

    def test_structural_error_gets_one_bounded_repair(self):
        calls = []

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return '{"verdict":"abstain"'
            return {
                "verdict": "approve", "risk_probability": .3,
                "confidence": .8, "reason_codes": [], "evidence_ids": [],
                "missing_information": [], "abstain_reason": None,
                "reason": "frozen evidence aligned",
            }

        result = run_graph_harness(
            make_input("structural-repair"), baseline_passed=True,
            model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("ValueError", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        rows = db.q(
            "SELECT status,retry_count,error_type FROM agent_steps "
            "WHERE run_id=? AND step_type='model' ORDER BY step_no",
            [result.run.run_id], db_path=self.path)
        self.assertEqual(
            [(row["status"], row["retry_count"]) for row in rows],
            [("failed", 0), ("completed", 1)])
        self.assertIn("ValueError", rows[0]["error_type"])

    def test_reject_evidence_is_repaired_to_declared_anchor(self):
        calls = []
        inp = replace(
            make_input("evidence-anchor"),
            field_provenance={"market": "signal:evidence-anchor:market"})

        def model(prompt):
            calls.append(prompt)
            evidence_id = ("market:1" if len(calls) == 1 else
                           "signal:evidence-anchor:market")
            return {
                "verdict": "reject", "risk_probability": .8,
                "confidence": .8, "reason_codes": ["liquidity_failure"],
                "evidence_ids": [evidence_id], "reason": "thin market",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertEqual(result.run.evidence_ids,
                         ("signal:evidence-anchor:market",))

    def test_read_only_tool_trace_and_single_model_call(self):
        calls = []
        router = ReadOnlyToolRouter(
            snapshot_tools(signal=lambda args: {"base": args["base"]}),
            allowed_tools=("get_signal_snapshot",), max_calls=1)

        def model(prompt):
            calls.append(prompt)
            return approve(prompt)

        result = run_graph_harness(
            make_input("tool"), baseline_passed=True, model_call=model,
            tool_router=router,
            tool_calls=[("get_signal_snapshot", {"base": "BTC"})],
            db_path=self.path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        rows = db.q("SELECT step_type,status FROM agent_steps ORDER BY step_no",
                    db_path=self.path)
        self.assertEqual([row["step_type"] for row in rows],
                         ["context", "retrieve", "tool", "model", "policy"])

    def test_retry_returns_durable_run_without_second_model_call(self):
        calls = []

        def model(prompt):
            calls.append(prompt)
            return approve(prompt)

        first = run_graph_harness(
            make_input("idempotent"), baseline_passed=True,
            model_call=model, db_path=self.path)
        second = run_graph_harness(
            make_input("idempotent"), baseline_passed=True,
            model_call=model, db_path=self.path)
        self.assertEqual(first.run.run_id, second.run.run_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(db.q1(
            "SELECT COUNT(*) n FROM agent_runs", db_path=self.path)["n"], 1)

    def test_provider_usage_and_structured_evidence_are_persisted(self):
        def metered(_prompt):
            return ModelCallResult(
                content={"verdict": "reject", "risk_probability": .8,
                         "confidence": .9,
                         "reason_codes": ["liquidity_failure"],
                         "evidence_ids": ["market:1"], "reason": "thin"},
                input_tokens=100, output_tokens=20,
                prompt_cache_hit_tokens=10,
                prompt_cache_miss_tokens=90,
                estimated_cost=.00002, pricing_version="price-v1")

        result = run_graph_harness(
            make_input("metered"), baseline_passed=True,
            model_call=metered, db_path=self.path)
        self.assertEqual(result.run.input_tokens, 100)
        row = db.q1("SELECT * FROM agent_runs WHERE run_id=?",
                    [result.run.run_id], db_path=self.path)
        self.assertEqual(row["evidence_ids"], '["market:1"]')
        self.assertEqual(row["prompt_cache_miss_tokens"], 90)
        self.assertEqual(row["pricing_version"], "price-v1")
        self.assertTrue(row["input_snapshot"])

    def test_trace_failure_is_visible_and_cannot_apply_veto(self):
        def reject(_prompt):
            return {"verdict": "reject", "risk_probability": .9,
                    "confidence": .9,
                    "reason_codes": ["liquidity_failure"],
                    "evidence_ids": ["market:1"], "reason": "thin"}

        with patch("decision.agent_graph.trace_store.record_run",
                   side_effect=OSError("disk unavailable")), \
                patch("builtins.print") as output:
            result = run_graph_harness(
                make_input("trace-failure"), baseline_passed=True,
                model_call=reject,
                policy_kernel=PolicyKernel(veto_enabled=True, shadow=False),
                db_path=self.path)

        self.assertFalse(result.policy.veto)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.TOOL_ERROR)
        self.assertEqual(result.run.error_type,
                         "TracePersistenceError:OSError")
        self.assertIn("trace persistence failed", output.call_args.args[0])

    def test_runtime_failures_keep_distinct_statuses(self):
        no_key = run_graph_harness(
            make_input("no-key"), baseline_passed=True,
            model_call=None, db_path=self.path)

        def timeout(_prompt):
            raise TimeoutError("slow provider")

        timed_out = run_graph_harness(
            make_input("timeout"), baseline_passed=True,
            model_call=timeout, db_path=self.path)

        def invalid(_prompt):
            raise ValueError("invalid provider payload")

        invalid_payload = run_graph_harness(
            make_input("provider-value-error"), baseline_passed=True,
            model_call=invalid, db_path=self.path)
        self.assertEqual(no_key.run.runtime_status, RuntimeStatus.NO_KEY)
        self.assertEqual(timed_out.run.runtime_status, RuntimeStatus.TIMEOUT)
        self.assertEqual(invalid_payload.run.runtime_status,
                         RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(no_key.run.final_action, FinalAction.BASELINE_PASS)
        self.assertEqual(timed_out.run.final_action, FinalAction.BASELINE_PASS)
        self.assertEqual(invalid_payload.run.final_action,
                         FinalAction.BASELINE_PASS)

    def test_baseline_reject_never_reaches_model(self):
        calls = []
        result = run_graph_harness(
            make_input("baseline-reject"), baseline_passed=False,
            model_call=lambda prompt: calls.append(prompt), db_path=self.path)
        self.assertFalse(calls)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_REJECT)
        self.assertEqual(db.q1(
            "SELECT COUNT(*) n FROM agent_steps", db_path=self.path)["n"], 0)

    def test_baseline_reject_overrides_existing_durable_agent_result(self):
        calls = []

        def model(prompt):
            calls.append(prompt)
            return approve(prompt)

        inp = make_input("baseline-authority")
        run_graph_harness(inp, baseline_passed=True, model_call=model,
                          db_path=self.path)
        rejected = run_graph_harness(
            inp, baseline_passed=False, model_call=model, db_path=self.path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rejected.run.final_action,
                         FinalAction.BASELINE_REJECT)

    def test_veto_requires_explicit_policy_kernel(self):
        def reject(_prompt):
            return {"verdict": "reject", "risk_probability": 0.9,
                    "confidence": 0.8,
                    "reason_codes": ["liquidity_failure"],
                    "evidence_ids": ["market:1"], "reason": "spread"}

        shadow = run_graph_harness(
            make_input("shadow"), baseline_passed=True, model_call=reject,
            db_path=self.path)
        active = run_graph_harness(
            make_input("explicit-veto"), baseline_passed=True,
            model_call=reject,
            policy_kernel=PolicyKernel(veto_enabled=True, shadow=False),
            db_path=self.path)
        self.assertEqual(shadow.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertFalse(shadow.policy.veto)
        self.assertEqual(active.run.final_action, FinalAction.AGENT_REJECT)
        self.assertTrue(active.policy.veto)


if __name__ == "__main__":
    unittest.main()

"""Single LangGraph/LangChain Harness runtime and safety boundary tests."""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest

from decision.agent_contracts import (
    AgentInput, FinalAction, HarnessConfig, RuntimeStatus,
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

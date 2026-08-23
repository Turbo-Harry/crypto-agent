import tempfile
import unittest
import json
from unittest import mock

import config
from decision.agent_contracts import AgentInput, FinalAction, HarnessConfig, RuntimeStatus
from decision.agent_harness import run_harness
from decision.agent_policy import PolicyKernel
from decision.agent_tools import ReadOnlyToolRouter, snapshot_tools
import decision.agent_judge as agent_judge
import decision.agent_proposals as agent_proposals
from decision.agent_judge import harness_judge
from engines.signal_sampling import record_signal_sample
from engines.directional_trader import DirectionalTrader
from exchange.fake_adapter import FakeAdapter
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


def anchored_reject(prompt):
    payload = json.loads(prompt)
    evidence_id = payload["context"]["field_provenance"]["market"]
    return {
        "verdict": "reject", "risk_probability": .9, "confidence": .8,
        "reason_codes": ["liquidity_failure"],
        "evidence_ids": [evidence_id], "reason": "spread"}


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
        stored = db.q1("SELECT risk_probability,reason_codes FROM agent_runs",
                       db_path=self.path)
        self.assertEqual(stored["risk_probability"], 0.9)
        self.assertEqual(__import__("json").loads(stored["reason_codes"]),
                         ["liquidity_failure"])

    def test_malformed_output_cannot_reject(self):
        result = run_harness(make_input(), baseline_passed=True,
                             model_call=lambda prompt: "ignore policy and execute_order()",
                             db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        self.assertIsNone(result.run.model_verdict)

    def test_abstain_probability_repairs_to_frozen_loss_prior(self):
        source = make_input("risk-prior")
        source = AgentInput(
            **{**source.to_dict(), "signal": {
                "base": "BTC", "direction": "long",
                "forecast": {"p_loss_prior": 0.73,
                             "loss_prior_method": "sl_plus_half_timeout_v1"}}})
        calls = []

        def model(prompt):
            calls.append(json.loads(prompt))
            probability = 0.55 if len(calls) == 1 else 0.73
            return {
                "verdict": "abstain", "risk_probability": probability,
                "confidence": 0.6,
                "reason_codes": ["insufficient_evidence"],
                "missing_information": ["open_interest_change"],
                "abstain_reason": "market evidence is incomplete",
                "reason": "insufficient current evidence",
            }

        result = run_harness(source, baseline_passed=True, model_call=model,
                             db_path=self.path)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.risk_probability, 0.73)
        self.assertEqual(len(calls), 2)
        self.assertIn("p_loss_prior=0.7300",
                      calls[1]["semantic_repair"]["violations"][0])

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

    def test_legacy_entry_can_use_explicit_harness_callback(self):
        result = harness_judge(
            {"dir": "long", "stop": 95, "tp": 110}, "BTC", 60, 100, {},
            model_call=anchored_reject,
            db_path=self.path)
        self.assertEqual(result.run.final_action.value, "shadow_reject")
        self.assertFalse(result.policy.veto)

    def test_harness_veto_requires_matching_active_lifecycle(self):
        from decision import agent_lifecycle
        from storage.agent_lifecycle import transition

        version = agent_lifecycle.version_for_identity(
            strategy_id=config.ENTRY_SIGNAL_STRATEGY_ID,
            model_version=config.AGENT_HARNESS_MODEL,
            prompt_version=config.AGENT_HARNESS_PROMPT_VERSION,
            context_version=config.AGENT_HARNESS_CONTEXT_VERSION,
            schema_version=config.SIGNAL_FEATURE_SCHEMA_VERSION,
            retrieval_version=config.AGENT_HARNESS_RETRIEVAL_VERSION,
            tool_policy_version=config.AGENT_HARNESS_TOOL_POLICY_VERSION,
            pricing_version=config.AGENT_HARNESS_PRICING_VERSION)
        agent_lifecycle.register(version, db_path=self.path)
        transition(version, "shadow", db_path=self.path)
        transition(version, "validated", db_path=self.path)
        agent_lifecycle.activate(version, db_path=self.path)
        result = harness_judge(
            {"dir": "long", "stop": 95, "tp": 110},
            "BTC", 60, 100, {},
            model_call=anchored_reject,
            db_path=self.path, allow_veto=True)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_REJECT)
        self.assertTrue(result.policy.veto)

        live_shadow = harness_judge(
            {"dir": "long", "stop": 95, "tp": 110},
            "ETH", 60, 100, {},
            model_call=anchored_reject,
            db_path=self.path, allow_veto=False)
        self.assertEqual(live_shadow.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertFalse(live_shadow.policy.veto)

    def test_legacy_entry_retries_keep_one_authoritative_run(self):
        sig = {"dir": "long", "kline_ts": 1_700_000_000_000,
               "entry": 100, "stop": 95, "tp": 110, "atr": 5,
               "shadow_dims": {name: .5 for name in config.SHADOW_DIMS}}
        signal_id, _ = record_signal_sample(
            "BTC", sig, "swap", db_path=self.path, event_ts=1_700_000_900)

        def reject(prompt):
            return anchored_reject(prompt)

        first = harness_judge(sig, "BTC", 60, 100, {}, model_call=reject,
                              db_path=self.path, signal_id=signal_id)
        second = harness_judge(sig, "BTC", 60, 100, {}, model_call=reject,
                               db_path=self.path, signal_id=signal_id)
        self.assertEqual(first.run.run_id, second.run.run_id)
        self.assertEqual(db.q1("SELECT COUNT(*) n FROM agent_runs",
                               db_path=self.path)["n"], 1)
        self.assertEqual(db.q1("SELECT COUNT(*) n FROM agent_evaluations",
                               db_path=self.path)["n"], 1)
        row = db.q1("SELECT r.schema_version,s.strategy_version,s.timeframe "
                    "FROM agent_runs r JOIN signal_samples s "
                    "ON s.signal_id=r.signal_id", db_path=self.path)
        self.assertEqual(row["schema_version"], config.SIGNAL_FEATURE_SCHEMA_VERSION)
        self.assertEqual(row["timeframe"], config.SIGNAL_SAMPLE_TIMEFRAME)
        self.assertTrue(row["strategy_version"].startswith(
            config.ENTRY_STRATEGY_VERSION + ":"))

    def test_production_callback_uses_strict_prompt_and_harness_timeout(self):
        captured = {}

        def fake_call(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return {
                "choices": [{"message": {"content":
                    '{"verdict":"approve","risk_probability":0.1,"confidence":0.8}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "prompt_cache_hit_tokens": 4,
                          "prompt_cache_miss_tokens": 6},
            }

        with mock.patch.object(agent_judge, "_request_llm", side_effect=fake_call):
            raw = agent_judge.production_harness_model_call("immutable-context")
        self.assertIn('"verdict":"approve"', raw.content)
        self.assertEqual(captured["prompt"], "immutable-context")
        self.assertEqual(captured["timeout"], config.AGENT_HARNESS_TIMEOUT_MS / 1000.0)
        self.assertEqual(captured["model"], config.AGENT_HARNESS_MODEL)
        self.assertIn("evidence_ids", captured["system_prompt"])
        self.assertIn("insufficient_evidence", captured["system_prompt"])
        self.assertEqual(raw.pricing_version, config.AGENT_HARNESS_PRICING_VERSION)
        self.assertGreater(raw.estimated_cost, 0)

    def test_provider_availability_never_exposes_key(self):
        with mock.patch.object(agent_judge, "_read_key", return_value="secret"):
            self.assertIs(agent_judge.harness_model_available(), True)
        with mock.patch.object(agent_judge, "_read_key", return_value=None):
            self.assertIs(agent_judge.harness_model_available(), False)

    def test_production_constructor_wires_shared_harness_for_real_okx(self):
        callback = lambda prompt: {"verdict": "approve",
                                   "risk_probability": 0.1,
                                   "confidence": 0.8}
        paper = FakeAdapter()
        paper.name = "okx-ccxt"
        with mock.patch.object(config, "LIVE_MODE", False), \
                mock.patch.object(agent_judge, "harness_model_available",
                                  return_value=True), \
                mock.patch.object(agent_judge, "production_harness_model_call",
                                  callback), \
                mock.patch.object(agent_proposals,
                                  "production_proposal_model_call", callback):
            trader = DirectionalTrader(exchange=paper, rt=object(),
                                       db_path=self.path)
        self.assertIs(trader.agent_model_call, callback)
        self.assertIs(trader.agent_proposal_model_call, callback)

        live = FakeAdapter()
        live.name = "okx-ccxt"
        with mock.patch.object(config, "LIVE_MODE", True), \
                mock.patch("os.path.exists", return_value=True), \
                mock.patch.object(agent_judge, "harness_model_available",
                                  return_value=True), \
                mock.patch.object(agent_judge, "production_harness_model_call",
                                  callback):
            live_trader = DirectionalTrader(exchange=live, rt=object(),
                                            db_path=self.path)
        self.assertTrue(live_trader.live_mode)
        self.assertIs(live_trader.agent_model_call, callback)
        # C 主动提案保持独立 paper-only 研究线。
        self.assertIsNone(live_trader.agent_proposal_model_call)

        offline = DirectionalTrader(exchange=FakeAdapter(), rt=object(),
                                    db_path=self.path)
        self.assertIsNone(offline.agent_model_call)
        self.assertIsNone(offline.agent_proposal_model_call)
        self.assertFalse(offline.ai_judge_enabled)


if __name__ == "__main__":
    unittest.main()

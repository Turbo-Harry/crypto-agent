"""Single LangGraph/LangChain Harness runtime and safety boundary tests."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import time
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

    def test_v7_repairs_short_direction_and_favorable_funding_misread(self):
        calls = []
        inp = replace(
            make_input("v7-short-direction"),
            prompt_version="harness-risk-v7-direction-evidence-consistency",
            signal={"base": "ADA", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -0.01, "momentum_4h": -0.02,
                "funding_rate": 0.0001, "spread_bps": 9.1,
            }}},
            field_provenance={"market": "signal:v7-short-direction:market"},
        )

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .78,
                    "confidence": .72,
                    "reason_codes": ["signal_inconsistency", "liquidity_failure"],
                    "evidence_ids": ["signal:v7-short-direction:market"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "negative momentum and positive funding conflict with short",
                }
            return {
                "verdict": "abstain", "risk_probability": .58,
                "confidence": .55, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "only one qualified liquidity risk family",
                "reason": "short momentum is aligned; liquidity risk alone is insufficient",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("direction-aligned", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v7_reject_needs_two_distinct_ordinary_risk_families(self):
        calls = []
        inp = replace(
            make_input("v7-one-family"),
            prompt_version="harness-risk-v7-direction-evidence-consistency",
            field_provenance={"market": "signal:v7-one-family:market"},
        )

        def model(prompt):
            calls.append(prompt)
            return {
                "verdict": "reject", "risk_probability": .8,
                "confidence": .8, "reason_codes": ["liquidity_failure"],
                "evidence_ids": ["signal:v7-one-family:market"],
                "missing_information": [], "abstain_reason": None,
                "reason": "wide spread is the only current risk family",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("two distinct ordinary risk families", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)

    def test_v7_position_risk_code_requires_actual_frozen_conflict(self):
        calls = []
        inp = replace(
            make_input("v7-position-code"),
            prompt_version="harness-risk-v7-direction-evidence-consistency",
            signal={"base": "AAVE", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -0.01, "momentum_4h": 0.02,
                "funding_rate": 0.0001,
            }}},
            account={"portfolio_notional_usdt": 0,
                     "max_total_notional_usdt": 600},
            health={"risk_halted": False, "risk_can_trade": True},
            field_provenance={
                "market": "signal:v7-position-code:market",
                "health": "signal:v7-position-code:health",
            },
        )

        def model(prompt):
            calls.append(prompt)
            return {
                "verdict": "reject", "risk_probability": .75,
                "confidence": .75,
                "reason_codes": ["signal_inconsistency", "position_risk_conflict"],
                "evidence_ids": ["signal:v7-position-code:market",
                                 "signal:v7-position-code:health"],
                "missing_information": [], "abstain_reason": None,
                "reason": "negative 1H momentum plus volatility conflict",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("position_risk_conflict lacks", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.SCHEMA_ERROR)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)

    def test_v7_single_extreme_event_remains_a_valid_reject(self):
        inp = replace(
            make_input("v7-extreme"),
            prompt_version="harness-risk-v7-direction-evidence-consistency",
            field_provenance={"news": "signal:v7-extreme:news"},
        )
        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .9,
                "confidence": .9, "reason_codes": ["extreme_market_event"],
                "evidence_ids": ["signal:v7-extreme:news"],
                "missing_information": [], "abstain_reason": None,
                "reason": "verified exchange-wide severe event",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v8_imbalance_cannot_masquerade_as_liquidity_failure(self):
        calls = []
        inp = replace(
            make_input("v8-imbalance-not-liquidity"),
            prompt_version="harness-risk-v8-liquidity-field-semantics",
            signal={"base": "SOL", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.004, "momentum_4h": .005,
                "spread_bps": 1.063, "expected_slippage_bps": 1.739,
                "depth": .8496, "book": .9473,
                "depth_imbalance": -.8946, "book_imbalance": -.8946,
            }}},
            field_provenance={"market": "signal:v8-imbalance-not-liquidity:market"},
        )

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .78,
                    "confidence": .75,
                    "reason_codes": ["signal_inconsistency", "liquidity_failure"],
                    "evidence_ids": ["signal:v8-imbalance-not-liquidity:market"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "positive 4H momentum conflicts with short; negative book imbalance means liquidity failure",
                }
            return {
                "verdict": "abstain", "risk_probability": .6,
                "confidence": .6, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "only directional inconsistency is qualified",
                "reason": "spread and expected slippage are not severe",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("severe frozen spread or expected slippage", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v8_severe_expected_slippage_is_valid_liquidity_evidence(self):
        inp = replace(
            make_input("v8-severe-slippage"),
            prompt_version="harness-risk-v8-liquidity-field-semantics",
            signal={"base": "LTC", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.001, "momentum_4h": .008,
                "spread_bps": 7.136, "expected_slippage_bps": 18.152,
            }}},
            field_provenance={"market": "signal:v8-severe-slippage:market"},
        )
        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .75,
                "confidence": .75,
                "reason_codes": ["signal_inconsistency", "liquidity_failure"],
                "evidence_ids": ["signal:v8-severe-slippage:market"],
                "missing_information": [], "abstain_reason": None,
                "reason": "negative 1H momentum conflicts with long and expected slippage is severe",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

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
        repair = json.loads(calls[1])["semantic_repair"]
        self.assertIn(
            "signal:evidence-anchor:market",
            repair["allowed_evidence_ids"])
        self.assertNotIn("market:1", repair["allowed_evidence_ids"])
        self.assertEqual(
            repair["allowed_evidence_ids"],
            sorted(repair["allowed_evidence_ids"]))
        self.assertIn("only from allowed_evidence_ids", repair["instruction"])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertEqual(result.run.evidence_ids,
                         ("signal:evidence-anchor:market",))

    def test_v6_initial_request_exposes_validator_owned_contract(self):
        calls = []
        inp = replace(
            make_input("initial-contract"),
            prompt_version="harness-risk-v8-liquidity-field-semantics",
            tool_policy_version="tool-policy-v6-initial-decision-contract",
            signal={"base": "BNB", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.009, "momentum_4h": -.006,
                "spread_bps": 1.0, "expected_slippage_bps": 2.0,
                "funding_rate": .0001,
            }}},
            account={"portfolio_notional_usdt": 0,
                     "max_total_notional_usdt": 600},
            health={"risk_halted": False, "risk_can_trade": True},
            field_provenance={
                "signal": "signal:initial-contract",
                "market": "signal:initial-contract:market",
            })

        def model(prompt):
            calls.append(json.loads(prompt))
            return {
                "verdict": "abstain", "risk_probability": .55,
                "confidence": .6, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "no two qualified current risk families",
                "reason": "deterministic qualifiers do not support reject",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 1)
        contract = calls[0]["decision_contract"]
        self.assertIn("signal:initial-contract",
                      contract["allowed_evidence_ids"])
        self.assertIn("signal:initial-contract:market",
                      contract["allowed_evidence_ids"])
        self.assertEqual(contract["allowed_evidence_ids"],
                         sorted(contract["allowed_evidence_ids"]))
        self.assertEqual(contract["deterministic_qualifiers"], {
            "directional_momentum_conflict": False,
            "funding_is_adverse_cost": False,
            "liquidity_failure_qualified": False,
            "position_risk_conflict_qualified": False,
        })
        self.assertEqual(
            contract["reject_thresholds"]["minimum_ordinary_risk_families"], 2)
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_old_tool_policy_keeps_original_initial_payload_shape(self):
        calls = []
        inp = replace(
            make_input("old-initial-shape"),
            field_provenance={"market": "signal:old-initial-shape:market"})

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda prompt: calls.append(json.loads(prompt)) or
            approve(prompt), db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertNotIn("decision_contract", calls[0])

    def test_v6_initial_contract_reports_qualified_frozen_risks(self):
        calls = []
        inp = replace(
            make_input("qualified-contract"),
            tool_policy_version="tool-policy-v6-initial-decision-contract",
            signal={"base": "LTC", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.001, "momentum_4h": .008,
                "spread_bps": 7.0, "expected_slippage_bps": 18.0,
                "funding_rate": .0001,
            }}},
            account={"portfolio_notional_usdt": 600,
                     "max_total_notional_usdt": 600},
            health={"risk_halted": True, "risk_can_trade": False})

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda prompt: calls.append(json.loads(prompt)) or
            approve(prompt), db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(
            calls[0]["decision_contract"]["deterministic_qualifiers"], {
                "directional_momentum_conflict": True,
                "funding_is_adverse_cost": True,
                "liquidity_failure_qualified": True,
                "position_risk_conflict_qualified": True,
            })

    def test_v7_contract_uses_news_sign_and_explicit_extreme_flag(self):
        calls = []
        inp = replace(
            make_input("v7-news-contract"),
            prompt_version="harness-risk-v9-news-extreme-event-semantics",
            tool_policy_version="tool-policy-v7-news-extreme-event-contract",
            signal={"base": "GRASS", "direction": "long"},
            news={"news_score": .5714, "composite": .5157,
                  "news_bull": 11, "news_bear": 3},
            field_provenance={"news": "signal:v7-news-contract:news"})

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda prompt: calls.append(json.loads(prompt)) or
            approve(prompt), db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        qualifiers = calls[0]["decision_contract"]["deterministic_qualifiers"]
        self.assertFalse(qualifiers["news_direction_conflict_qualified"])
        self.assertFalse(qualifiers["extreme_market_event_qualified"])

    def test_v8_contract_exposes_signed_signal_consistency(self):
        calls = []
        inp = replace(
            make_input("v8-signal-contract"),
            prompt_version="harness-risk-v10-signal-consistency-semantics",
            tool_policy_version="tool-policy-v8-signal-consistency-contract",
            signal={"base": "HOOD", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.000187,
                "momentum_4h": -.003827,
                "trend_band_atr": -1.7486,
                "directional_index_spread": -.12,
            }}},
            field_provenance={
                "market": "signal:v8-signal-contract:market",
            })

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda prompt: calls.append(json.loads(prompt)) or
            approve(prompt), db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        qualifiers = calls[0]["decision_contract"]["deterministic_qualifiers"]
        self.assertFalse(qualifiers["signal_inconsistency_qualified"])
        self.assertFalse(qualifiers["news_direction_conflict_qualified"])
        self.assertFalse(qualifiers["extreme_market_event_qualified"])

    def test_v9_contract_exposes_exact_conflicting_factors(self):
        calls = []
        inp = replace(
            make_input("v9-factor-contract"),
            prompt_version="harness-risk-v11-factor-specific-signal-evidence",
            tool_policy_version="tool-policy-v9-factor-specific-signal-contract",
            signal={"base": "GRASS", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.054, "momentum_4h": -.038,
                "trend_band_atr": 1.014,
                "directional_index_spread": -.208,
            }}})

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda prompt: calls.append(json.loads(prompt)) or
            approve(prompt), db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        contract = calls[0]["decision_contract"]
        self.assertTrue(contract["deterministic_qualifiers"]
                        ["signal_inconsistency_qualified"])
        self.assertEqual(contract["signal_inconsistency_conflicting_factors"],
                         ["trend_band_atr"])

    def test_v10_contract_exposes_exact_qualified_family_floor(self):
        calls = []
        inp = replace(
            make_input("v10-family-floor"),
            prompt_version="harness-risk-v12-qualified-family-floor",
            tool_policy_version="tool-policy-v10-qualified-family-floor",
            signal={"base": "INJ", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .001, "momentum_4h": .004,
                "trend_band_atr": .67,
                "directional_index_spread": .064,
                "spread_bps": 7.4, "expected_slippage_bps": 33.2,
            }}},
            news={"news_score": .54},
            account={"portfolio_notional_usdt": 0,
                     "max_total_notional_usdt": 600},
            health={"risk_halted": False, "risk_can_trade": True})

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda prompt: calls.append(json.loads(prompt)) or
            approve(prompt), db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        contract = calls[0]["decision_contract"]
        self.assertEqual(contract["qualified_ordinary_risk_families"],
                         ["liquidity_failure"])
        self.assertFalse(contract["reject_evidence_floor_satisfied"])

    def test_v12_repairs_reject_when_only_one_family_is_qualified(self):
        calls = []
        inp = replace(
            make_input("v12-one-family"),
            prompt_version="harness-risk-v12-qualified-family-floor",
            tool_policy_version="tool-policy-v10-qualified-family-floor",
            signal={"base": "INJ", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .001, "momentum_4h": .004,
                "trend_band_atr": .67,
                "directional_index_spread": .064,
                "expected_slippage_bps": 33.2,
            }}},
            news={"news_score": .54},
            field_provenance={
                "market": "signal:v12-one-family:market",
                "signal": "signal:v12-one-family",
            })

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .75,
                    "confidence": .75,
                    "reason_codes": ["liquidity_failure",
                                     "signal_inconsistency"],
                    "evidence_ids": ["signal:v12-one-family:market",
                                     "signal:v12-one-family"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "slippage and momentum conflict support reject",
                }
            return {
                "verdict": "abstain", "risk_probability": .66,
                "confidence": .65, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "only liquidity_failure is qualified",
                "reason": "reject evidence floor is not satisfied",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("unqualified ordinary risk families", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v12_replay_still_repairs_high_risk_high_confidence_abstain(self):
        calls = []
        inp = replace(
            make_input("v12-high-risk-abstain-replay"),
            prompt_version="harness-risk-v12-qualified-family-floor",
            tool_policy_version="tool-policy-v10-qualified-family-floor",
            signal={"base": "INJ", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .001, "momentum_4h": .004,
                "trend_band_atr": .67,
                "directional_index_spread": .064,
                "expected_slippage_bps": 33.2,
            }}})

        def model(prompt):
            calls.append(prompt)
            return {
                "verdict": "abstain", "risk_probability": .8,
                "confidence": .8 if len(calls) == 1 else .69,
                "reason_codes": ["liquidity_failure"], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "only one ordinary risk family is qualified",
                "reason": "loss risk is high but reject evidence is incomplete",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("high-risk high-confidence", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)
        self.assertEqual(result.run.confidence, .69)

    def test_v13_one_family_allows_honest_high_risk_abstain_first_call(self):
        calls = []
        inp = replace(
            make_input("v13-high-risk-one-family"),
            prompt_version="harness-risk-v13-evidence-gated-abstain",
            tool_policy_version="tool-policy-v11-evidence-gated-abstain",
            signal={"base": "INJ", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .001, "momentum_4h": .004,
                "trend_band_atr": .67,
                "directional_index_spread": .064,
                "expected_slippage_bps": 33.2,
            }}},
            field_provenance={
                "market": "signal:v13-high-risk-one-family:market",
            })

        def model(prompt):
            calls.append(json.loads(prompt))
            return {
                "verdict": "abstain", "risk_probability": .8,
                "confidence": .8, "reason_codes": ["liquidity_failure"],
                "evidence_ids": ["signal:v13-high-risk-one-family:market"],
                "missing_information": [],
                "abstain_reason": "only one ordinary risk family is qualified",
                "reason": "loss risk is high but reject evidence floor is false",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 1)
        contract = calls[0]["decision_contract"]
        self.assertEqual(contract["qualified_ordinary_risk_families"],
                         ["liquidity_failure"])
        self.assertFalse(contract["reject_evidence_floor_satisfied"])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)
        self.assertEqual(result.run.risk_probability, .8)
        self.assertEqual(result.run.confidence, .8)

    def test_v13_two_families_still_repairs_high_risk_abstain_to_reject(self):
        calls = []
        inp = replace(
            make_input("v13-high-risk-two-families"),
            prompt_version="harness-risk-v13-evidence-gated-abstain",
            tool_policy_version="tool-policy-v11-evidence-gated-abstain",
            signal={"base": "BTC", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.001, "momentum_4h": .002,
                "trend_band_atr": .4,
                "directional_index_spread": .05,
                "expected_slippage_bps": 14.2,
            }}},
            field_provenance={
                "market": "signal:v13-high-risk-two-families:market",
                "signal": "signal:v13-high-risk-two-families",
            })

        def model(prompt):
            calls.append(prompt)
            verdict = "abstain" if len(calls) == 1 else "reject"
            return {
                "verdict": verdict, "risk_probability": .8,
                "confidence": .8,
                "reason_codes": ["liquidity_failure",
                                 "signal_inconsistency"],
                "evidence_ids": ["signal:v13-high-risk-two-families:market",
                                 "signal:v13-high-risk-two-families"],
                "missing_information": [],
                "abstain_reason": ("mixed high-risk evidence"
                                   if verdict == "abstain" else None),
                "reason": "severe slippage and negative momentum_1h oppose long",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("satisfied evidence floor", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v12_two_qualified_families_reject_on_first_call(self):
        calls = []
        inp = replace(
            make_input("v12-two-families"),
            prompt_version="harness-risk-v12-qualified-family-floor",
            tool_policy_version="tool-policy-v10-qualified-family-floor",
            signal={"base": "BTC", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.001, "momentum_4h": .002,
                "trend_band_atr": .4,
                "directional_index_spread": .05,
                "expected_slippage_bps": 14.2,
            }}},
            field_provenance={
                "market": "signal:v12-two-families:market",
                "signal": "signal:v12-two-families",
            })

        def model(prompt):
            calls.append(json.loads(prompt))
            return {
                "verdict": "reject", "risk_probability": .72,
                "confidence": .75,
                "reason_codes": ["liquidity_failure",
                                 "signal_inconsistency"],
                "evidence_ids": ["signal:v12-two-families:market",
                                 "signal:v12-two-families"],
                "missing_information": [], "abstain_reason": None,
                "reason": "slippage is severe and momentum_1h opposes long",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 1)
        contract = calls[0]["decision_contract"]
        self.assertEqual(contract["qualified_ordinary_risk_families"],
                         ["liquidity_failure", "signal_inconsistency"])
        self.assertTrue(contract["reject_evidence_floor_satisfied"])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v12_missing_data_cannot_be_a_reject_family(self):
        calls = []
        inp = replace(
            make_input("v12-missing-family"),
            prompt_version="harness-risk-v12-qualified-family-floor",
            tool_policy_version="tool-policy-v10-qualified-family-floor",
            signal={"base": "INJ", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .001, "momentum_4h": .004,
                "trend_band_atr": .67,
                "directional_index_spread": .064,
                "expected_slippage_bps": 33.2,
            }}},
            field_provenance={
                "market": "signal:v12-missing-family:market",
                "signal": "signal:v12-missing-family",
            })

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .75,
                    "confidence": .75,
                    "reason_codes": ["liquidity_failure",
                                     "stale_or_missing_data"],
                    "evidence_ids": ["signal:v12-missing-family:market",
                                     "signal:v12-missing-family"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "slippage and missing fields support reject",
                }
            return {
                "verdict": "abstain", "risk_probability": .66,
                "confidence": .6, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "missing data lowers confidence only",
                "reason": "only one qualified ordinary family",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("cannot support reject", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v11_replay_preserves_missing_data_family_semantics(self):
        inp = replace(
            make_input("v11-missing-replay"),
            prompt_version="harness-risk-v11-factor-specific-signal-evidence",
            tool_policy_version="tool-policy-v9-factor-specific-signal-contract",
            signal={"base": "INJ", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "expected_slippage_bps": 33.2,
            }}},
            field_provenance={
                "market": "signal:v11-missing-replay:market",
                "signal": "signal:v11-missing-replay",
            })

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .75,
                "confidence": .75,
                "reason_codes": ["liquidity_failure",
                                 "stale_or_missing_data"],
                "evidence_ids": ["signal:v11-missing-replay:market",
                                 "signal:v11-missing-replay"],
                "missing_information": [], "abstain_reason": None,
                "reason": "slippage and missing fields support reject",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v10_repairs_regime_misread_as_signal_inconsistency(self):
        calls = []
        inp = replace(
            make_input("v10-aligned-hood"),
            prompt_version="harness-risk-v10-signal-consistency-semantics",
            tool_policy_version="tool-policy-v8-signal-consistency-contract",
            signal={"base": "HOOD", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.000187,
                "momentum_4h": -.003827,
                "trend_band_atr": -1.7486,
                "directional_index_spread": -.12,
                "vol_of_vol": .02,
            }}, "regime": {"state": "disorder"},
                     "strategy_route": "abstain"},
            news={"news_score": .6667, "composite": .5633},
            field_provenance={
                "market": "signal:v10-aligned-hood:market",
                "news": "signal:v10-aligned-hood:news",
            })

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .75,
                    "confidence": .75,
                    "reason_codes": ["news_direction_conflict",
                                     "signal_inconsistency"],
                    "evidence_ids": ["signal:v10-aligned-hood:news",
                                     "signal:v10-aligned-hood:market"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "bullish news conflicts; disorder is not a clean short",
                }
            return {
                "verdict": "abstain", "risk_probability": .62,
                "confidence": .65, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "only one qualified ordinary risk family",
                "reason": "all signed technical factors align with short",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("opposite-sign frozen factor", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v10_allows_opposite_signed_dmi_as_signal_family(self):
        inp = replace(
            make_input("v10-opposite-dmi"),
            prompt_version="harness-risk-v10-signal-consistency-semantics",
            tool_policy_version="tool-policy-v8-signal-consistency-contract",
            signal={"base": "ADA", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.01, "momentum_4h": -.02,
                "trend_band_atr": -.8, "directional_index_spread": .15,
            }}},
            news={"news_score": .4},
            field_provenance={
                "market": "signal:v10-opposite-dmi:market",
                "news": "signal:v10-opposite-dmi:news",
            })

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .78,
                "confidence": .76,
                "reason_codes": ["signal_inconsistency",
                                 "news_direction_conflict"],
                "evidence_ids": ["signal:v10-opposite-dmi:market",
                                 "signal:v10-opposite-dmi:news"],
                "missing_information": [], "abstain_reason": None,
                "reason": "positive DMI spread and bullish news oppose the short",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v11_repairs_aligned_momentum_claim_with_valid_trend_conflict(self):
        calls = []
        inp = replace(
            make_input("v11-factor-specific"),
            prompt_version="harness-risk-v11-factor-specific-signal-evidence",
            tool_policy_version="tool-policy-v9-factor-specific-signal-contract",
            signal={"base": "GRASS", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.054, "momentum_4h": -.038,
                "trend_band_atr": 1.014,
                "directional_index_spread": -.208,
            }}},
            news={"news_score": .5385, "composite": .4992},
            field_provenance={
                "market": "signal:v11-factor-specific:market",
                "news": "signal:v11-factor-specific:news",
            })

        def model(prompt):
            calls.append(prompt)
            reason = ("positive 1H/4H momentum contradicts short"
                      if len(calls) == 1 else
                      "positive trend_band_atr contradicts short")
            return {
                "verdict": "reject", "risk_probability": .82,
                "confidence": .75,
                "reason_codes": ["news_direction_conflict",
                                 "signal_inconsistency"],
                "evidence_ids": ["signal:v11-factor-specific:news",
                                 "signal:v11-factor-specific:market"],
                "missing_information": [], "abstain_reason": None,
                "reason": reason,
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("direction-aligned factor family: momentum", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertIn("trend_band_atr", result.run.decision_reason)

    def test_v10_replay_preserves_aggregate_factor_semantics(self):
        inp = replace(
            make_input("v10-factor-replay"),
            prompt_version="harness-risk-v10-signal-consistency-semantics",
            tool_policy_version="tool-policy-v8-signal-consistency-contract",
            signal={"base": "GRASS", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.054, "momentum_4h": -.038,
                "trend_band_atr": 1.014,
                "directional_index_spread": -.208,
            }}},
            news={"news_score": .5385},
            field_provenance={
                "market": "signal:v10-factor-replay:market",
                "news": "signal:v10-factor-replay:news",
            })

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .82,
                "confidence": .75,
                "reason_codes": ["news_direction_conflict",
                                 "signal_inconsistency"],
                "evidence_ids": ["signal:v10-factor-replay:news",
                                 "signal:v10-factor-replay:market"],
                "missing_information": [], "abstain_reason": None,
                "reason": "positive 1H/4H momentum contradicts short",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v9_replay_preserves_pre_v10_signal_semantics(self):
        inp = replace(
            make_input("v9-signal-replay"),
            prompt_version="harness-risk-v9-news-extreme-event-semantics",
            tool_policy_version="tool-policy-v7-news-extreme-event-contract",
            signal={"base": "HOOD", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.01, "momentum_4h": -.02,
                "trend_band_atr": -.8,
            }}, "regime": {"state": "disorder"}},
            news={"news_score": .4},
            field_provenance={
                "market": "signal:v9-signal-replay:market",
                "news": "signal:v9-signal-replay:news",
            })

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .78,
                "confidence": .76,
                "reason_codes": ["signal_inconsistency",
                                 "news_direction_conflict"],
                "evidence_ids": ["signal:v9-signal-replay:market",
                                 "signal:v9-signal-replay:news"],
                "missing_information": [], "abstain_reason": None,
                "reason": "disorder weakens the setup and bullish news opposes short",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v9_repairs_favorable_news_misread_as_long_conflict(self):
        calls = []
        inp = replace(
            make_input("v9-favorable-news"),
            prompt_version="harness-risk-v9-news-extreme-event-semantics",
            tool_policy_version="tool-policy-v7-news-extreme-event-contract",
            signal={"base": "GRASS", "direction": "long"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": -.01, "momentum_4h": .02,
            }}},
            news={"news_score": .5714, "composite": .5157,
                  "news_bull": 11, "news_bear": 3},
            field_provenance={
                "market": "signal:v9-favorable-news:market",
                "news": "signal:v9-favorable-news:news",
            })

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .72,
                    "confidence": .75,
                    "reason_codes": ["news_direction_conflict",
                                     "signal_inconsistency"],
                    "evidence_ids": ["signal:v9-favorable-news:news",
                                     "signal:v9-favorable-news:market"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "bullish news conflicts with the long signal",
                }
            return {
                "verdict": "abstain", "risk_probability": .6,
                "confidence": .6, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "only one qualified ordinary risk family",
                "reason": "positive news is aligned with the long candidate",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("opposite-sign frozen sentiment", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v9_routine_volatility_cannot_claim_extreme_event(self):
        calls = []
        inp = replace(
            make_input("v9-not-extreme"),
            prompt_version="harness-risk-v9-news-extreme-event-semantics",
            tool_policy_version="tool-policy-v7-news-extreme-event-contract",
            signal={"base": "HOOD", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .02, "momentum_4h": -.01,
                "vol_of_vol": .02,
            }}, "regime": {"state": "vol_expansion"}},
            field_provenance={"market": "signal:v9-not-extreme:market"})

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {
                    "verdict": "reject", "risk_probability": .75,
                    "confidence": .75,
                    "reason_codes": ["extreme_market_event"],
                    "evidence_ids": ["signal:v9-not-extreme:market"],
                    "missing_information": [], "abstain_reason": None,
                    "reason": "vol_expansion and high volatility are extreme",
                }
            return {
                "verdict": "abstain", "risk_probability": .62,
                "confidence": .6, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "no explicit severe event flag",
                "reason": "routine volatility is not an extreme event",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("explicit frozen event flag", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.AGENT_ABSTAIN)

    def test_v9_explicit_extreme_event_remains_valid_single_family(self):
        inp = replace(
            make_input("v9-explicit-extreme"),
            prompt_version="harness-risk-v9-news-extreme-event-semantics",
            tool_policy_version="tool-policy-v7-news-extreme-event-contract",
            news={"extreme_market_event": True},
            field_provenance={"news": "signal:v9-explicit-extreme:news"})

        result = run_graph_harness(
            inp, baseline_passed=True,
            model_call=lambda _prompt: {
                "verdict": "reject", "risk_probability": .9,
                "confidence": .9, "reason_codes": ["extreme_market_event"],
                "evidence_ids": ["signal:v9-explicit-extreme:news"],
                "missing_information": [], "abstain_reason": None,
                "reason": "explicit frozen severe event flag is true",
            }, db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)

    def test_v9_repairs_duplicate_reject_evidence_ids(self):
        calls = []
        inp = replace(
            make_input("v9-duplicate-evidence"),
            prompt_version="harness-risk-v9-news-extreme-event-semantics",
            tool_policy_version="tool-policy-v7-news-extreme-event-contract",
            signal={"base": "ADA", "direction": "short"},
            market={"frozen_features": {"factor_features": {
                "momentum_1h": .01, "momentum_4h": -.01,
                "spread_bps": 9.0, "expected_slippage_bps": 12.0,
            }}},
            field_provenance={"market": "signal:v9-duplicate-evidence:market"})

        def model(prompt):
            calls.append(prompt)
            evidence = (["signal:v9-duplicate-evidence:market"] * 2
                        if len(calls) == 1 else
                        ["signal:v9-duplicate-evidence:market"])
            return {
                "verdict": "reject", "risk_probability": .78,
                "confidence": .75,
                "reason_codes": ["signal_inconsistency",
                                 "liquidity_failure"],
                "evidence_ids": evidence, "missing_information": [],
                "abstain_reason": None,
                "reason": "positive short momentum conflicts and costs are severe",
            }

        result = run_graph_harness(
            inp, baseline_passed=True, model_call=model, db_path=self.path)

        self.assertEqual(len(calls), 2)
        self.assertIn("evidence_ids must be unique", calls[1])
        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(result.run.final_action, FinalAction.SHADOW_REJECT)
        self.assertEqual(result.run.evidence_ids,
                         ("signal:v9-duplicate-evidence:market",))

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

    def test_late_valid_reject_is_discarded_by_total_time_budget(self):
        def late_reject(_prompt):
            time.sleep(.08)
            return {
                "verdict": "reject", "risk_probability": .9,
                "confidence": .9, "reason_codes": ["extreme_market_event"],
                "evidence_ids": ["market:1"], "reason": "late severe event",
            }

        result = run_graph_harness(
            make_input("late-total-timeout"), baseline_passed=True,
            model_call=late_reject, config=HarnessConfig(timeout_ms=50),
            db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.TIMEOUT)
        self.assertEqual(result.run.final_action, FinalAction.BASELINE_PASS)
        self.assertIsNone(result.decision)
        self.assertGreaterEqual(result.run.model_latency_ms, 50)

    def test_semantic_repair_receives_only_remaining_provider_budget(self):
        budgets = []

        def budgeted_model(_prompt, *, timeout_seconds=None):
            budgets.append(timeout_seconds)
            if len(budgets) == 1:
                time.sleep(.01)
                return {
                    "verdict": "abstain", "risk_probability": .55,
                    "confidence": .5,
                    "reason_codes": ["insufficient_evidence"],
                    "missing_information": [],
                    "abstain_reason": "缺少已验证模型",
                    "reason": "invalid governance evidence",
                }
            return {
                "verdict": "abstain", "risk_probability": .55,
                "confidence": .5, "reason_codes": [], "evidence_ids": [],
                "missing_information": [],
                "abstain_reason": "mixed current market evidence",
                "reason": "insufficient independent risk families",
            }

        budgeted_model.supports_timeout_budget = True
        result = run_graph_harness(
            make_input("remaining-repair-budget"), baseline_passed=True,
            model_call=budgeted_model, config=HarnessConfig(timeout_ms=100),
            db_path=self.path)

        self.assertEqual(result.run.runtime_status, RuntimeStatus.COMPLETED)
        self.assertEqual(len(budgets), 2)
        self.assertGreater(budgets[0], budgets[1])
        self.assertLessEqual(budgets[0], .1)

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

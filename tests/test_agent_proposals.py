"""Agent 主动提案：严格契约、证据门、2:1 几何与无执行权限。"""

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace

import config
import storage.db as sdb
from decision.agent_contracts import stable_hash
from decision.agent_proposals import (build_market_snapshot, list_proposals,
                                      run_proposal_cycle)
from engines.signal_scan import SignalScanMixin
from engines.signal_sampling import record_agent_proposal_sample
from exchange.fake_adapter import FakeAdapter


def market_rows(*, timeframe_seconds=900, n=70, base=100.0):
    end_open = int((time.time() - timeframe_seconds * 2) * 1000)
    start = end_open - (n - 1) * timeframe_seconds * 1000
    rows = []
    for index in range(n):
        close = base + index * 0.1
        rows.append([start + index * timeframe_seconds * 1000,
                     close - 0.05, close + 0.3, close - 0.3,
                     close, 1000 + index])
    return rows


def snapshot():
    return build_market_snapshot(
        "BTC", market_rows(),
        market_rows(timeframe_seconds=3600),
        market_rows(timeframe_seconds=14400),
        market_features={
            "spread_bps": 1.2, "book_imbalance": 0.25,
            "ofi_event_multilevel": 0.2, "ofi_event_count": 12,
        }, market_snapshot_ts=int(time.time() * 1000))


def valid_output(snap, *, confidence=0.82, evidence_id=None,
                 include_micro=True):
    evidence_ids = [evidence_id or snap.evidence_ids[0]]
    if include_micro and evidence_id is None:
        evidence_ids.append(snap.microstructure_evidence_id)
    return {"proposals": [{
        "base": "BTC", "direction": "long", "confidence": confidence,
        "thesis": "15m趋势和1h动量同向，作为可证伪影子候选",
        "evidence_ids": evidence_ids,
    }], "abstain_reason": None}


def recorder(db_path):
    return lambda **kwargs: record_agent_proposal_sample(
        db_path=db_path, **kwargs)


class AgentProposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent_proposal_")
        self.db_path = os.path.join(self.tmp.name, "proposal.db")
        sdb.init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_proposal_is_exact_2to1_shadow_and_counterfactual(self):
        snap = snapshot()
        result = run_proposal_cycle(
            [snap], model_call=lambda _prompt: valid_output(snap),
            sample_recorder=recorder(self.db_path),
            db_path=self.db_path,
            event_ts=snap.kline_ts / 1000 + config.SIGNAL_TIMEFRAME_SECONDS[
                config.SIGNAL_SAMPLE_TIMEFRAME])
        self.assertEqual(result["run"]["runtime_status"], "completed")
        self.assertEqual(len(result["proposals"]), 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["geometry_valid"], 1)
        self.assertEqual(proposal["prediction_passed"], 0)
        self.assertEqual(proposal["validation_status"], "shadow_geometry_valid")
        self.assertEqual(proposal["execution_authority"], 0)
        self.assertAlmostEqual(proposal["reward_risk"], 2.0)
        self.assertEqual(proposal["validation_reason"], "no_validated_active_model")
        sample = sdb.q1("SELECT * FROM signal_samples WHERE signal_id=?",
                        [proposal["signal_id"]], db_path=self.db_path)
        self.assertEqual(sample["strategy_id"], config.AGENT_PROPOSAL_STRATEGY_ID)
        self.assertEqual(sample["rule_decision"], "shadow")
        self.assertEqual(sample["final_decision"], "rejected")
        self.assertIn("agent_proposal_shadow", sample["reject_reason"])

    def test_snapshot_freezes_deterministic_aligned_direction(self):
        long_snap = snapshot()
        short_snap = replace(
            long_snap, ema20_15m=99.0, ema50_15m=100.0,
            momentum_1h=-0.01, momentum_4h=-0.02)
        mixed_snap = replace(long_snap, momentum_4h=-0.02)
        self.assertEqual(long_snap.aligned_direction, "long")
        self.assertEqual(short_snap.aligned_direction, "short")
        self.assertIsNone(mixed_snap.aligned_direction)
        self.assertEqual(long_snap.to_dict()["aligned_direction"], "long")

    def test_cycle_is_idempotent_and_does_not_rebill_model(self):
        snap = snapshot()
        calls = []

        def model(_prompt):
            calls.append(1)
            return valid_output(snap)

        first = run_proposal_cycle([snap], model_call=model,
                                   sample_recorder=recorder(self.db_path),
                                   db_path=self.db_path)
        second = run_proposal_cycle([snap], model_call=model,
                                    sample_recorder=recorder(self.db_path),
                                    db_path=self.db_path)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sdb.q1("SELECT COUNT(*) n FROM agent_proposal_runs",
                                db_path=self.db_path)["n"], 1)
        self.assertEqual(sdb.q1("SELECT COUNT(*) n FROM signal_samples",
                                db_path=self.db_path)["n"], 1)

    def test_unknown_evidence_is_rejected_before_signal_sampling(self):
        snap = snapshot()
        result = run_proposal_cycle(
            [snap], model_call=lambda _prompt: valid_output(
                snap, evidence_id="invented:evidence"), db_path=self.db_path)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["validation_status"], "rejected")
        self.assertEqual(proposal["validation_reason"], "unknown_evidence_id")
        self.assertIsNone(proposal["signal_id"])
        self.assertEqual(sdb.q1("SELECT COUNT(*) n FROM signal_samples",
                                db_path=self.db_path)["n"], 0)

    def test_low_confidence_is_audited_but_not_sampled(self):
        snap = snapshot()
        result = run_proposal_cycle(
            [snap], model_call=lambda _prompt: valid_output(
                snap, confidence=config.AGENT_PROPOSAL_MIN_CONFIDENCE - 0.01),
            db_path=self.db_path)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["validation_reason"],
                         "confidence_below_minimum")
        self.assertEqual(proposal["execution_authority"], 0)
        self.assertIsNone(proposal["signal_id"])

    def test_proposal_without_microstructure_evidence_is_rejected(self):
        snap = snapshot()
        result = run_proposal_cycle(
            [snap], model_call=lambda _prompt: valid_output(
                snap, include_micro=False), db_path=self.db_path)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["validation_reason"],
                         "microstructure_evidence_required")
        self.assertEqual(proposal["geometry_valid"], 0)
        self.assertIsNone(proposal["signal_id"])

    def test_direction_conflict_is_rejected_before_geometry_and_sampling(self):
        snap = snapshot()
        output = valid_output(snap)
        output["proposals"][0]["direction"] = "short"
        result = run_proposal_cycle(
            [snap], model_call=lambda _prompt: output,
            sample_recorder=recorder(self.db_path), db_path=self.db_path)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["validation_status"], "rejected")
        self.assertEqual(proposal["validation_reason"],
                         "direction_evidence_conflict")
        self.assertEqual(proposal["geometry_valid"], 0)
        self.assertIsNone(proposal["signal_id"])
        self.assertEqual(sdb.q1("SELECT COUNT(*) n FROM signal_samples",
                                db_path=self.db_path)["n"], 0)

    def test_schema_error_records_run_without_proposal(self):
        result = run_proposal_cycle(
            [snapshot()], model_call=lambda _prompt: {"buy_now": "BTC"},
            db_path=self.db_path)
        self.assertEqual(result["run"]["runtime_status"], "schema_error")
        self.assertEqual(result["run"]["valid_count"], 0)
        self.assertEqual(result["proposals"], [])

    def test_empty_proposal_requires_reason_and_freezes_exact_input(self):
        snap = snapshot()
        result = run_proposal_cycle(
            [snap], model_call=lambda _prompt: {
                "proposals": [], "abstain_reason": "microstructure_conflict"},
            db_path=self.db_path)
        self.assertEqual(result["run"]["runtime_status"], "completed")
        view = list_proposals(db_path=self.db_path)
        self.assertEqual(view["auditable_run_count"], 1)
        self.assertEqual(view["current_protocol_run_count"], 1)
        self.assertEqual(view["current_protocol_completed_count"], 1)
        self.assertEqual(view["current_protocol_abstain_count"], 1)
        self.assertEqual(view["current_protocol_proposal_count"], 0)
        self.assertEqual(view["current_protocol_proposal_coverage"], 0.0)
        run = view["runs"][0]
        self.assertEqual(run["abstain_reason"], "microstructure_conflict")
        audit = run["audit"]
        self.assertEqual(audit["implementation_version"],
                         config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION)
        self.assertEqual(audit["input_hash"],
                         stable_hash(audit["input_snapshot"]))
        self.assertEqual(audit["snapshot_count"], 1)
        self.assertGreater(audit["microstructure_coverage"], 0)
        frozen = audit["input_snapshot"]["snapshots"][0]
        self.assertEqual(frozen["aligned_direction"], "long")
        self.assertEqual(audit["input_snapshot"]["eligible_candidates"], [
            {"base": "BTC", "direction": "long"}])
        self.assertIn("microstructure", frozen)
        self.assertTrue(any(value.endswith(":microstructure")
                            for value in frozen["evidence_ids"]))

    def test_empty_proposal_without_standard_reason_is_schema_error(self):
        result = run_proposal_cycle(
            [snapshot()], model_call=lambda _prompt: {
                "proposals": [], "abstain_reason": None},
            db_path=self.db_path)
        self.assertEqual(result["run"]["runtime_status"], "schema_error")

    def test_false_no_aligned_reason_is_rejected_before_sampling(self):
        result = run_proposal_cycle(
            [snapshot()], model_call=lambda _prompt: {
                "proposals": [], "abstain_reason": "no_aligned_candidate"},
            db_path=self.db_path)
        self.assertEqual(result["run"]["runtime_status"], "schema_error")
        self.assertEqual(result["proposals"], [])
        self.assertEqual(sdb.q1("SELECT COUNT(*) n FROM signal_samples",
                                db_path=self.db_path)["n"], 0)

    def test_all_unaligned_requires_no_aligned_reason(self):
        unaligned = replace(snapshot(), momentum_4h=-0.02)
        valid = run_proposal_cycle(
            [unaligned], model_call=lambda _prompt: {
                "proposals": [], "abstain_reason": "no_aligned_candidate"},
            db_path=self.db_path)
        self.assertEqual(valid["run"]["runtime_status"], "completed")

        other_db = os.path.join(self.tmp.name, "proposal-other.db")
        sdb.init_db(other_db)
        invalid = run_proposal_cycle(
            [unaligned], model_call=lambda _prompt: {
                "proposals": [], "abstain_reason": "no_clear_edge"},
            db_path=other_db)
        self.assertEqual(invalid["run"]["runtime_status"], "schema_error")

    def test_scanner_hook_is_paper_shadow_and_never_places_order(self):
        snap_rows = {
            config.SIGNAL_SAMPLE_TIMEFRAME: market_rows(),
            config.SIGNAL_CONTEXT_TIMEFRAME: market_rows(
                timeframe_seconds=3600),
            config.SIGNAL_REGIME_TIMEFRAME: market_rows(
                timeframe_seconds=14400),
        }
        fake = FakeAdapter()
        inst_id = "BTC-USDT-SWAP"
        fake.funding_rates[inst_id] = 0.0001
        fake.open_interests[inst_id] = 110.0
        fake.basis_values[inst_id] = 0.0012
        fake.fetch_order_book = lambda _inst_id, _depth: {
            "bids": [[106.8, 4.0], [106.7, 2.0]],
            "asks": [[107.0, 2.0], [107.1, 1.0]],
        }
        holder = type("ProposalScanner", (), {})()
        holder.live_mode = False
        captured = {}

        def model(prompt):
            captured.update(json.loads(prompt))
            frozen = captured["snapshots"][0]
            return {"proposals": [{
                "base": "BTC", "direction": "long", "confidence": 0.82,
                "thesis": "三周期与订单流证据同向，作为可证伪影子候选",
                "evidence_ids": [frozen["evidence_ids"][0],
                                 frozen["evidence_ids"][-1]],
            }], "abstain_reason": None}

        holder.agent_proposal_model_call = model
        holder.watch_scores = {"BTC": 1.0}
        holder._db_path = self.db_path
        holder.exchange = fake
        holder._proposal_oi_state = {"BTC": 100.0}
        holder.rt = type("Realtime", (), {"get_orderflow": lambda _self, _base: {
            "ofi_event_multilevel": 0.35,
            "ofi_event_cancel_imbalance": -0.1,
            "ofi_event_count": 12,
            "ofi_event_age_ms": 42,
        }})()
        requested_limits = []

        def fetch(_base, tf, limit):
            requested_limits.append(limit)
            return snap_rows[tf]

        holder._fetch_klines_any = fetch
        result = SignalScanMixin._run_agent_proposal_shadow(holder, ["BTC"])
        self.assertIsNotNone(result)
        self.assertEqual(requested_limits, [
            config.AGENT_PROPOSAL_MIN_BARS + 2,
            config.AGENT_PROPOSAL_MIN_BARS + 2,
            config.AGENT_PROPOSAL_MIN_BARS + 2,
        ])
        self.assertEqual(fake.orders, [])
        self.assertEqual(fake.algos, [])
        micro = captured["snapshots"][0]["microstructure"]
        self.assertEqual(micro["funding_rate"], 0.0001)
        self.assertAlmostEqual(micro["open_interest_change"], 0.1)
        self.assertEqual(micro["ofi_event_multilevel"], 0.35)
        self.assertEqual(micro["ofi_event_count"], 12.0)
        self.assertGreater(micro["book_imbalance"], 0)
        self.assertGreater(micro["spread_bps"], 0)
        self.assertIsInstance(
            captured["snapshots"][0]["microstructure_as_of_ms"], int)
        self.assertEqual(captured["snapshots"][0]["aligned_direction"], "long")
        self.assertEqual(captured["eligible_candidates"], [
            {"base": "BTC", "direction": "long"}])
        self.assertTrue(captured["snapshots"][0]["evidence_ids"][-1].endswith(
            ":microstructure"))
        sample = sdb.q1(
            "SELECT features FROM signal_samples WHERE signal_id=?",
            [result["proposals"][0]["signal_id"]], db_path=self.db_path)
        features = json.loads(sample["features"])["factor_features"]
        self.assertEqual(features["funding_rate"], 0.0001)
        self.assertEqual(features["ofi_event_count"], 12.0)
        view = list_proposals(db_path=self.db_path)
        self.assertTrue(view["shadow_only"])
        self.assertFalse(view["execution_authority"])
        self.assertEqual(view["current_protocol_run_count"], 1)
        self.assertEqual(view["current_protocol_proposal_count"], 1)
        self.assertEqual(view["current_protocol_proposal_coverage"], 1.0)
        self.assertAlmostEqual(
            view["current_protocol_microstructure_coverage"], 13 / 15, 6)

    def test_live_mode_guard_does_not_call_model(self):
        calls = []
        holder = type("ProposalScanner", (), {})()
        holder.live_mode = True
        holder.agent_proposal_model_call = lambda _prompt: calls.append(1)
        holder.watch_scores = {"BTC": 1.0}
        holder._db_path = self.db_path
        self.assertIsNone(
            SignalScanMixin._run_agent_proposal_shadow(holder, ["BTC"]))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

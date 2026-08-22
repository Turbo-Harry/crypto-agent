import unittest

from decision.agent_context import build_context, context_hash, missing_fields, serialize_context
from decision.agent_contracts import AgentInput


def make_input(**kwargs):
    values = dict(
        run_id="r1", signal_id="s1", event_ts="2026-08-23T00:00:00Z",
        kline_ts="2026-08-23T00:00:00Z", strategy_version="strategy-v1",
        prompt_version="judge-v1", model_version="model-v1",
        context_version="context-v1", schema_version="schema-v1",
        retrieval_version="retrieval-v1", signal={"base": "BTC"},
        market={"regime": "trend"}, field_provenance={"market.regime": "snapshot:1"},
    )
    values.update(kwargs)
    return AgentInput(**values)


class AgentContextTest(unittest.TestCase):
    def test_same_snapshot_has_same_serialization_and_hash(self):
        left = make_input(signal={"b": 2, "a": 1})
        right = make_input(signal={"a": 1, "b": 2})
        self.assertEqual(serialize_context(left), serialize_context(right))
        self.assertEqual(context_hash(left), context_hash(right))

    def test_sections_are_fixed_and_missing_is_explicit(self):
        payload = build_context(make_input(news={}, account={}, health={}))
        self.assertEqual(tuple(payload), (
            "identity", "versions", "signal", "market", "news", "account",
            "health", "memory", "field_provenance",
        ))
        self.assertEqual(missing_fields(make_input(news={}, account={}, health={})),
                         ("news", "account", "health"))

    def test_oversized_context_fails_closed(self):
        with self.assertRaises(ValueError):
            serialize_context(make_input(signal={"large": "x" * 100}), max_chars=20)


if __name__ == "__main__":
    unittest.main()

import time
import unittest

from decision.agent_tools import (
    ReadOnlyToolRouter,
    ToolBudgetExceeded,
    ToolNotAllowed,
    ToolSchemaError,
    snapshot_tools,
)


class AgentHarnessToolsTest(unittest.TestCase):
    def test_only_registered_read_tool_can_run(self):
        router = ReadOnlyToolRouter(
            snapshot_tools(signal=lambda args: {"base": args["base"], "price": 1}),
            allowed_tools=("get_signal_snapshot",), max_calls=2)
        result = router.call("get_signal_snapshot", {"base": "BTC"})
        self.assertTrue(result.ok)
        with self.assertRaises(ToolNotAllowed):
            router.call("execute_order", {"side": "buy"})

    def test_schema_and_call_budget_are_enforced(self):
        router = ReadOnlyToolRouter(
            snapshot_tools(signal=lambda args: {"ok": True}),
            allowed_tools=("get_signal_snapshot",), max_calls=1)
        with self.assertRaises(ToolSchemaError):
            router.call("get_signal_snapshot", "not-an-object")
        router.call("get_signal_snapshot", {})
        with self.assertRaises(ToolBudgetExceeded):
            router.call("get_signal_snapshot", {})

    def test_output_bound_and_trace_are_recorded(self):
        router = ReadOnlyToolRouter(
            {"get_signal_snapshot": lambda args: "x" * 9000},
            allowed_tools=("get_signal_snapshot",), max_calls=2)
        with self.assertRaises(ToolSchemaError):
            router.call("get_signal_snapshot", {})
        self.assertEqual(len(router.trace), 1)
        self.assertFalse(router.trace[0].ok)

    def test_time_budget_fails_before_next_call(self):
        router = ReadOnlyToolRouter(
            {"get_signal_snapshot": lambda args: {}},
            allowed_tools=("get_signal_snapshot",), max_calls=3, deadline_ms=1)
        router.started -= 1
        with self.assertRaises(ToolBudgetExceeded):
            router.call("get_signal_snapshot", {})


if __name__ == "__main__":
    unittest.main()

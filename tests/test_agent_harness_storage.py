import tempfile
import unittest

from decision.agent_contracts import (
    AgentInput,
    AgentStep,
    FinalAction,
    HarnessRun,
    LifecycleStatus,
    RuntimeStatus,
    StepStatus,
    StepType,
)
from storage import agent_harness


class AgentHarnessStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.input = AgentInput(
            run_id="run-1", signal_id="signal-1", event_ts="1", kline_ts="1",
            strategy_version="strategy-v1", prompt_version="judge-v1",
            model_version="model-v1", context_version="context-v1",
            schema_version="schema-v1", retrieval_version="retrieval-v1",
        )

    def tearDown(self):
        import os
        os.unlink(self.tmp.name)

    def test_run_is_idempotent_and_steps_are_traceable(self):
        run = HarnessRun(
            run_id="run-1", signal_id="signal-1",
            runtime_status=RuntimeStatus.TIMEOUT,
            final_action=FinalAction.BASELINE_PASS,
            error_type="timeout",
        )
        first = agent_harness.record_run(run, self.input, db_path=self.tmp.name)
        second = agent_harness.record_run(run, self.input, db_path=self.tmp.name)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(agent_harness.list_runs(db_path=self.tmp.name)), 1)
        agent_harness.record_step(AgentStep(
            run_id="run-1", step_no=1, step_type=StepType.MODEL,
            status=StepStatus.FAILED, started_at="1", finished_at="2",
            error_type="timeout", fallback_action="baseline_pass",
        ), db_path=self.tmp.name)
        self.assertEqual(agent_harness.get_run("run-1", db_path=self.tmp.name)["signal_id"], "signal-1")

    def test_evaluation_can_start_pending(self):
        agent_harness.record_evaluation("run-1", lifecycle_status=LifecycleStatus.PENDING,
                                        db_path=self.tmp.name)
        from storage import db
        row = db.q1("SELECT lifecycle_status FROM agent_evaluations WHERE run_id=?",
                    ["run-1"], db_path=self.tmp.name)
        self.assertEqual(row["lifecycle_status"], "pending")


if __name__ == "__main__":
    unittest.main()

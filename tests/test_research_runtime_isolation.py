"""Research CLI entrypoints must not shadow the Python 3.12 runtime with legacy lib/."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_LIB = (ROOT / "lib").resolve()
RESEARCH_MODULES = (
    "tools.replay_15m_research",
    "tools.evaluate_15m_research",
    "tools.evaluate_strategy_c_reversal",
    "tools.evaluate_strategy_b_confirmation",
)


class ResearchRuntimeIsolationTest(unittest.TestCase):
    def test_research_entrypoints_keep_numpy_outside_legacy_lib(self):
        env = os.environ.copy()
        env["CRYPTO_AGENT_MODE"] = "paper"
        env["PYTHONPATH"] = str(ROOT)
        for module in RESEARCH_MODULES:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-c",
                     f"import {module}; import numpy; print(numpy.__file__)"],
                    cwd=ROOT, env=env, capture_output=True, text=True,
                    timeout=30, check=False)
                self.assertEqual(
                    result.returncode, 0,
                    f"{module}: {result.stdout}\n{result.stderr}")
                numpy_path = Path(result.stdout.strip().splitlines()[-1]).resolve()
                self.assertFalse(
                    numpy_path.is_relative_to(LEGACY_LIB),
                    f"{module} loaded legacy numpy from {numpy_path}")


if __name__ == "__main__":
    unittest.main()

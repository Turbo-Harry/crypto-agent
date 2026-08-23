#!/usr/bin/env python3
"""CLI compatibility wrapper for :mod:`decision.readiness`."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from decision.readiness import readiness_status, render_lines, sqn

__all__ = ["readiness_status", "render_lines", "sqn"]


if __name__ == "__main__":
    for line in render_lines():
        print(line)

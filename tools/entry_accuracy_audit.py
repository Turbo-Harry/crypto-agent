#!/usr/bin/env python3
"""CLI compatibility wrapper for :mod:`decision.entry_accuracy_audit`."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from decision.entry_accuracy_audit import audit_status, main

__all__ = ["audit_status", "main"]


if __name__ == "__main__":
    raise SystemExit(main())

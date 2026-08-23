#!/usr/bin/env python3
"""Compatibility wrapper for the anomaly storage interface."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from storage.anomaly_repository import list_new, register, resolve

__all__ = ["list_new", "register", "resolve"]

#!/usr/bin/env python3
"""Offline JSONL evaluator for frozen Agent Harness outcomes.

No model/network calls are made. Each input row must already contain a settled
path outcome and (optionally) an input_hash for paired comparisons.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from decision.agent_evaluation import compare_same_inputs, summarize


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no} is not an object")
        rows.append(row)
    return rows


def build_report(rows: list[dict], *, model_cost: float = 0.0,
                 challenger_rows: list[dict] | None = None) -> dict:
    metrics = summarize(rows, model_cost=model_cost)
    report = {"metrics": metrics.__dict__, "rows": len(rows)}
    if challenger_rows is not None:
        report["paired"] = compare_same_inputs(rows, challenger_rows)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="evaluate frozen Agent Harness JSONL")
    parser.add_argument("input", help="champion JSONL path")
    parser.add_argument("--challenger", help="optional challenger JSONL path")
    parser.add_argument("--model-cost", type=float, default=0.0)
    parser.add_argument("--output", help="optional report JSON path")
    args = parser.parse_args(argv)
    rows = _read_jsonl(args.input)
    challenger = _read_jsonl(args.challenger) if args.challenger else None
    report = build_report(rows, model_cost=args.model_cost, challenger_rows=challenger)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())


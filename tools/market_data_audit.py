#!/usr/bin/env python3
"""Read-only quality gate for strict OKX SWAP ``klines_v2`` data."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data.market_data import BAR_MS, audit_window


DEFAULT_DB = os.path.join(ROOT, "data", "market.db")


def _window(date_text: str) -> tuple[int, int]:
    day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(day.timestamp() * 1000)
    return start, start + BAR_MS["1D"]


def _latest_run(conn: sqlite3.Connection, target_date: str) -> dict:
    row = conn.execute(
        "SELECT * FROM market_collection_runs WHERE mode='reconcile' "
        "AND target_date=? ORDER BY requested_series DESC,started_at DESC LIMIT 1",
        [target_date]).fetchone()
    if not row:
        return {}
    result = dict(row)
    try:
        result["details"] = json.loads(result.get("details") or "{}")
    except json.JSONDecodeError:
        result["details"] = {}
    return result


def audit(db_path: str, target_date: str,
          bars: list[str] | None = None) -> dict:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True,
                           timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        run = _latest_run(conn, target_date)
        details = run.get("details") or {}
        symbols = list(details.get("symbols") or [])
        selected_bars = list(bars or details.get("bars") or BAR_MS)
        if not symbols:
            rows = conn.execute(
                "SELECT DISTINCT inst_id FROM klines_v2 WHERE source='okx' "
                "AND venue='swap' ORDER BY inst_id").fetchall()
            symbols = [str(row[0]) for row in rows]
        start_ms, end_ms = _window(target_date)
        quality = audit_window(
            conn, symbols, selected_bars, start_ms, end_ms) if symbols else {
                "complete": False, "missing": 0, "bad": 0, "series": {}}
        failed_run = bool(run) and run.get("status") != "success"
        complete = bool(run) and quality["complete"] and not failed_run
        return {"db": os.path.abspath(db_path), "date_utc": target_date,
                "symbols": len(symbols), "bars": selected_bars,
                "run_status": run.get("status") if run else "missing",
                "run_id": run.get("run_id"), "missing": quality["missing"],
                "source_gaps": quality.get("source_gaps", 0),
                "unexplained_missing": quality.get(
                    "unexplained_missing", quality["missing"]),
                "bad": quality["bad"], "complete": complete,
                "failed_series": [
                    {"series": key, **value}
                    for key, value in quality.get("series", {}).items()
                    if value.get("unexplained_missing") or value["bad"]][:100],
                "source_gap_series": [
                    {"series": key, **value}
                    for key, value in quality.get("series", {}).items()
                    if value.get("source_gaps")][:100]}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--date", default=(datetime.now(timezone.utc).date() -
                           timedelta(days=1)).isoformat())
    parser.add_argument("--bars", help="comma-separated subset")
    args = parser.parse_args(argv)
    bars = ([item.strip() for item in args.bars.split(",") if item.strip()]
            if args.bars else None)
    result = audit(args.db, args.date, bars)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Replay the frozen paper proposal Agent on causal historical snapshots.

The command is intentionally fixed to a five-symbol panel and two UTC batches
per day.  It writes only to an explicitly supplied independent research DB;
runtime databases are rejected.  ``--apply`` is required for model calls and
writes, and reruns are idempotent by the production proposal cycle key.
"""

from __future__ import annotations

import argparse
import bisect
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.agent_proposals import (build_market_snapshot,
                                      production_proposal_model_call,
                                      run_proposal_cycle)
from decision.signal_outcomes import persist_outcome, settle_path
from engines.signal_sampling import record_agent_proposal_sample


SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
BAR_MS = {"1m": 60_000, "15m": 900_000, "1H": 3_600_000,
          "4H": 14_400_000}
TRAIN_START_TS = 1_779_840_000.0   # 2026-05-27 00:00 UTC
TRAIN_END_TS = 1_784_937_600.0     # 2026-07-25 00:00 UTC
VALIDATION_END_TS = 1_787_486_400.0  # 2026-08-23 12:00 UTC (inclusive event)
EVENT_STRIDE_SECONDS = 12 * 3600
REPLAY_VERSION = "agent-proposal-causal-replay-v1"
FROZEN_PROMPT_VERSION = "agent-proposal-v1"
RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}


class MarketReader:
    def __init__(self, path: str):
        resolved = Path(path).expanduser().resolve()
        if resolved.name in RUNTIME_DB_NAMES or not resolved.is_file():
            raise ValueError("market DB 必须是独立历史行情库")
        self.path = resolved
        self.conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._cache: dict[tuple[str, str], tuple[list[int], list[dict[str, Any]]]] = {}

    def close(self) -> None:
        self.conn.close()

    def _series(self, base: str, bar: str) -> tuple[list[int], list[dict[str, Any]]]:
        key = (base, bar)
        if key not in self._cache:
            rows = [dict(row) for row in self.conn.execute(
                "SELECT open_time,open,high,low,close,volume,quote_volume "
                "FROM klines WHERE inst_id=? AND bar=? ORDER BY open_time",
                [f"{base}-USDT-SWAP", bar]).fetchall()]
            self._cache[key] = ([int(row["open_time"]) for row in rows], rows)
        return self._cache[key]

    def closed(self, base: str, bar: str, event_ms: int,
               limit: int) -> list[dict[str, Any]]:
        times, rows = self._series(base, bar)
        end = bisect.bisect_right(times, int(event_ms) - BAR_MS[bar])
        return rows[max(0, end - int(limit)):end]

    def path_1m(self, base: str, event_ms: int,
                horizon_hours: int = 4) -> list[dict[str, Any]]:
        times, rows = self._series(base, "1m")
        lo = bisect.bisect_left(times, int(event_ms))
        hi = bisect.bisect_left(
            times, int(event_ms) + int(horizon_hours) * 3_600_000)
        return rows[lo:hi]


def _validate_output(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.name in RUNTIME_DB_NAMES:
        raise ValueError("拒绝把 Agent 历史回放写入运行数据库")
    return resolved


def _phase_bounds(phase: str) -> tuple[float, float]:
    if phase == "training":
        return TRAIN_START_TS, TRAIN_END_TS
    if phase == "validation":
        # End is inclusive in the predeclaration, while range() semantics here
        # are half-open.
        return TRAIN_END_TS, VALIDATION_END_TS + EVENT_STRIDE_SECONDS
    raise ValueError(f"unsupported phase: {phase}")


def inventory(market_db: str, phase: str) -> dict[str, Any]:
    start, end = _phase_bounds(phase)
    events = list(range(int(start), int(end), EVENT_STRIDE_SECONDS))
    path = Path(market_db).expanduser().resolve()
    return {
        "research_only": True,
        "phase": phase,
        "symbols": list(SYMBOLS),
        "start_ts": start,
        "end_ts_exclusive": end,
        "stride_seconds": EVENT_STRIDE_SECONDS,
        "event_count": len(events),
        "market_db": str(path),
        "market_db_sha256": _sha256(path),
        "prompt_version": FROZEN_PROMPT_VERSION,
        "schema_version": config.AGENT_PROPOSAL_SCHEMA_VERSION,
        "model_version": config.AGENT_JUDGE_MODEL,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _init_output(output_db: Path, market_db: str) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    import storage.db as sdb
    sdb.init_db(str(output_db))
    with sqlite3.connect(output_db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS proposal_replay_costs ("
            "run_id TEXT PRIMARY KEY,phase TEXT NOT NULL,prompt_chars INTEGER NOT NULL,"
            "assumed_input_tokens INTEGER NOT NULL,assumed_output_tokens INTEGER NOT NULL,"
            "estimated_cost_usd REAL NOT NULL,pricing_version TEXT NOT NULL)"
        )
        proof = {
            "research_only": True, "version": REPLAY_VERSION,
            "market_db": str(Path(market_db).expanduser().resolve()),
            "market_db_sha256": _sha256(Path(market_db).expanduser().resolve()),
            "symbols": list(SYMBOLS), "stride_seconds": EVENT_STRIDE_SECONDS,
            "prompt_version": FROZEN_PROMPT_VERSION,
            "schema_version": config.AGENT_PROPOSAL_SCHEMA_VERSION,
            "model_version": config.AGENT_JUDGE_MODEL,
        }
        conn.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            ["research.agent_proposal_replay.latest",
             json.dumps(proof, ensure_ascii=False, sort_keys=True), time.time()])
        conn.commit()


def _snapshots(reader: MarketReader, event_ts: float):
    event_ms = int(event_ts * 1000)
    snapshots = []
    for base in SYMBOLS:
        rows15 = reader.closed(base, "15m", event_ms,
                               config.AGENT_PROPOSAL_MIN_BARS)
        rows1h = reader.closed(base, "1H", event_ms,
                              config.AGENT_PROPOSAL_MIN_BARS)
        rows4h = reader.closed(base, "4H", event_ms,
                              config.AGENT_PROPOSAL_MIN_BARS)
        try:
            snapshots.append(build_market_snapshot(base, rows15, rows1h, rows4h))
        except (TypeError, ValueError):
            continue
    return snapshots


def _estimated_call_cost(prompt: str) -> tuple[int, int, float]:
    # Deliberately pessimistic: one Unicode character is counted as one input
    # token, every call pays cache-miss price, and all 200 allowed output tokens
    # are charged even when the response is shorter.
    input_tokens = len(prompt)
    output_tokens = 200
    usd = (
        input_tokens * config.AGENT_HARNESS_INPUT_CACHE_MISS_USD_PER_M +
        output_tokens * config.AGENT_HARNESS_OUTPUT_USD_PER_M
    ) / 1_000_000
    return input_tokens, output_tokens, usd


@contextmanager
def _frozen_v1_protocol():
    """Scope the research process to the predeclared v1 prompt identity."""
    previous = config.AGENT_PROPOSAL_PROMPT_VERSION
    config.AGENT_PROPOSAL_PROMPT_VERSION = FROZEN_PROMPT_VERSION
    try:
        yield
    finally:
        config.AGENT_PROPOSAL_PROMPT_VERSION = previous


def _record_cost(output_db: str, run_id: str, phase: str,
                 prompt_chars: int, input_tokens: int,
                 output_tokens: int, cost_usd: float) -> None:
    with sqlite3.connect(output_db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO proposal_replay_costs VALUES(?,?,?,?,?,?,?)",
            [run_id, phase, prompt_chars, input_tokens, output_tokens, cost_usd,
             config.AGENT_HARNESS_PRICING_VERSION])
        conn.commit()


def replay(market_db: str, output_db: str, phase: str, *,
           model_call: Callable[[str], Any], enforce_validation_gate: bool = True,
           progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    output = _validate_output(output_db)
    if phase == "validation" and enforce_validation_gate:
        from tools.evaluate_agent_proposal_replay import evaluate_phase
        training = evaluate_phase(str(output), "training")
        if training.get("status") != "passed":
            raise ValueError("training phase 未通过，validation 保持封存")
    reader = None
    with _frozen_v1_protocol():
        _init_output(output, market_db)
        reader = MarketReader(market_db)
        start, end = _phase_bounds(phase)
        stats = {"phase": phase, "events": 0, "runs_created": 0,
                 "runs_deduplicated": 0, "proposals": 0, "settled": 0,
                 "missing_path": 0, "empty_snapshots": 0}
        try:
            return _replay_events(
                reader, output, phase, start, end, stats, model_call, progress)
        finally:
            reader.close()


def _replay_events(reader: MarketReader, output: Path, phase: str,
                   start: float, end: float, stats: dict[str, Any],
                   model_call: Callable[[str], Any],
                   progress: Callable[[dict[str, Any]], None] | None
                   ) -> dict[str, Any]:
    """Replay loop, called only while the frozen v1 protocol is active."""
    try:
        for event in range(int(start), int(end), EVENT_STRIDE_SECONDS):
            stats["events"] += 1
            snapshots = _snapshots(reader, float(event))
            if len(snapshots) != len(SYMBOLS):
                stats["empty_snapshots"] += 1
                if progress:
                    progress(dict(stats, event_ts=event))
                continue
            call_meta: dict[str, Any] = {}

            def costing_call(prompt: str):
                input_tokens, output_tokens, cost_usd = _estimated_call_cost(prompt)
                call_meta.update({
                    "prompt_chars": len(prompt), "input_tokens": input_tokens,
                    "output_tokens": output_tokens, "cost_usd": cost_usd,
                })
                return model_call(prompt)

            costing_call.model_version = str(
                getattr(model_call, "model_version", None) or
                config.AGENT_JUDGE_MODEL)
            result = run_proposal_cycle(
                snapshots, model_call=costing_call,
                sample_recorder=lambda **kwargs: record_agent_proposal_sample(
                    **kwargs, db_path=str(output)),
                db_path=str(output), event_ts=float(event))
            if result.get("deduplicated"):
                stats["runs_deduplicated"] += 1
            else:
                stats["runs_created"] += int(result.get("run") is not None)
            run = result.get("run") or {}
            if call_meta and run.get("run_id"):
                _record_cost(
                    str(output), run["run_id"], phase,
                    call_meta["prompt_chars"], call_meta["input_tokens"],
                    call_meta["output_tokens"], call_meta["cost_usd"])
            for proposal in result.get("proposals") or []:
                signal_id = proposal.get("signal_id")
                if not signal_id:
                    continue
                stats["proposals"] += 1
                import storage.db as sdb
                if sdb.q1("SELECT signal_id FROM signal_outcomes WHERE signal_id=?",
                          [signal_id], db_path=str(output)):
                    continue
                sample = sdb.q1("SELECT * FROM signal_samples WHERE signal_id=?",
                                [signal_id], db_path=str(output))
                outcome = settle_path(
                    sample, reader.path_1m(proposal["base"], event * 1000),
                    bar_resolution="1m",
                    label_version=config.SIGNAL_OUTCOME_LABEL_VERSION)
                if outcome is None:
                    stats["missing_path"] += 1
                else:
                    persist_outcome(outcome, db_path=str(output))
                    stats["settled"] += 1
            if progress:
                progress(dict(stats, event_ts=event,
                              runtime_status=run.get("runtime_status")))
        return stats
    finally:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-db", required=True)
    parser.add_argument("--output-db")
    parser.add_argument("--phase", choices=("training", "validation"),
                        default="training")
    parser.add_argument("--apply", action="store_true",
                        help="perform provider calls and write the research DB")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps(inventory(args.market_db, args.phase),
                         ensure_ascii=False, indent=2, sort_keys=True))
        return
    if not args.output_db:
        parser.error("--apply 必须显式提供 --output-db")
    from decision.agent_judge import harness_model_available
    if not harness_model_available():
        parser.error("provider credential unavailable")

    def report(item: dict[str, Any]) -> None:
        if item["events"] % 10 == 0:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True), flush=True)

    result = replay(
        args.market_db, args.output_db, args.phase,
        model_call=production_proposal_model_call, progress=report)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

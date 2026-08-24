#!/usr/bin/env python3
"""Evaluate a predeclared, research-only B-long confirmation policy.

The original B breakout must survive one fully closed 15m candle.  The policy
enters at the following 1m open only when that candle has a bullish body and
closes above the original breakout entry.  Rules and validation order are
constants: early core development -> late core validation -> symbol holdout.
Later stages remain sealed as soon as an earlier gate fails.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from decision.entry_probability import cost_breakdown_r


STRATEGY_ID = "B_breakout"
DIRECTION = "long"
TIMEFRAME = "15m"
HORIZON_HOURS = 4
BAR_15M_MS = 15 * 60_000
MINUTE_MS = 60_000
COOLDOWN_MS = HORIZON_HOURS * 3_600_000
VALIDATION_CUTOFF_TS = 1_784_966_400.0  # 2026-07-25 08:00:00 UTC
DEVELOPMENT_SYMBOLS = (
    "BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX")
HOLDOUT_SYMBOLS = ("BNB", "LTC")
POLICY_VERSION = "strategy-b-long-one-bar-confirmation-v1-predeclared"
MARKET_INPUT_VERSION = "confirmed-klines-v2-preferred"
RUNTIME_DB_NAMES = {"crypto_agent.db", "crypto_agent_live.db"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preferred_kline_table(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='klines_v2'"
    ).fetchone()
    return "klines_v2" if row else "klines"


def _open_inputs(replay_db: str, market_db: str) -> tuple[
        Path, Path, sqlite3.Connection, sqlite3.Connection, Mapping[str, Any]]:
    replay_path = Path(replay_db).expanduser().resolve()
    market_path = Path(market_db).expanduser().resolve()
    if (replay_path.name in RUNTIME_DB_NAMES or
            market_path.name in RUNTIME_DB_NAMES):
        raise ValueError("refuse runtime databases for confirmation research")
    if not replay_path.is_file() or not market_path.is_file():
        raise FileNotFoundError("research replay and market databases must exist")
    replay = sqlite3.connect(replay_path.as_uri() + "?mode=ro", uri=True)
    replay.row_factory = sqlite3.Row
    market = sqlite3.connect(market_path.as_uri() + "?mode=ro", uri=True)
    try:
        row = replay.execute(
            "SELECT value FROM kv WHERE key='research.15m_replay.latest'"
        ).fetchone()
        proof = json.loads(row[0]) if row else {}
        if not isinstance(proof, Mapping) or proof.get("research_only") is not True:
            raise ValueError("replay database lacks research-only provenance")
        proven_market = Path(str(proof.get("market_db") or "")).expanduser().resolve()
        if proven_market != market_path:
            raise ValueError("market database does not match replay provenance")
    except Exception:
        replay.close()
        market.close()
        raise
    return replay_path, market_path, replay, market, proof


def confirm_candidate(candidate: Mapping[str, Any],
                      confirmation_bar: tuple[Any, ...] | None
                      ) -> dict[str, Any] | None:
    """Return a causal entry candidate after one fully closed 15m candle."""
    if confirmation_bar is None:
        return None
    event_ms = int(round(float(candidate["event_ts"]) * 1000))
    open_time, open_price, _high, _low, close, _volume = confirmation_bar
    if int(open_time) != event_ms:
        return None
    original_entry = float(candidate["entry"])
    close = float(close)
    if close <= float(open_price) or close <= original_entry:
        return None
    atr = float(candidate["atr"])
    if close <= 0 or atr <= 0:
        return None
    return {
        **dict(candidate),
        "signal_event_ms": event_ms,
        "entry_event_ms": event_ms + BAR_15M_MS,
        "confirmation_close": close,
        "confirmation_body": "bullish",
    }


def resolve_trade(candidate: Mapping[str, Any],
                  minute_bars: list[tuple[Any, ...]],
                  *, funding_rate: float | None = None
                  ) -> dict[str, Any] | None:
    """Resolve the delayed entry with a complete 4h path and SL-first ties."""
    entry_ms = int(candidate["entry_event_ms"])
    end_ms = entry_ms + COOLDOWN_MS
    times = [int(row[0]) for row in minute_bars]
    lo = bisect.bisect_left(times, entry_ms)
    hi = bisect.bisect_left(times, end_ms)
    path = minute_bars[lo:hi]
    expected = HORIZON_HOURS * 60
    if (len(path) != expected or not path or int(path[0][0]) != entry_ms or
            int(path[-1][0]) + MINUTE_MS != end_ms or
            any(int(right[0]) - int(left[0]) != MINUTE_MS
                for left, right in zip(path, path[1:]))):
        return None
    entry = float(path[0][1])
    risk = float(candidate["atr"])
    if entry <= 0 or risk <= 0:
        return None
    stop, tp = entry - risk, entry + 2 * risk
    exit_price = None
    outcome = "timeout"
    for bar in path:
        high, low = float(bar[2]), float(bar[3])
        if low <= stop:
            exit_price, outcome = stop, "sl"
            break
        if high >= tp:
            exit_price, outcome = tp, "tp"
            break
    if exit_price is None:
        exit_price = float(path[-1][4])
    gross_r = (exit_price - entry) / risk
    costs = cost_breakdown_r({
        "entry": entry, "stop": stop, "direction": DIRECTION,
        "horizon_hours": HORIZON_HOURS, "funding_rate": funding_rate,
    })
    if costs is None:
        return None
    return {
        **dict(candidate), "entry": entry, "stop": stop, "tp": tp,
        "funding_rate": funding_rate, "outcome": outcome,
        "gross_r": gross_r, "cost_r": costs["total_cost_r"],
        "net_r": gross_r - costs["total_cost_r"],
    }


def _funding_asof(conn: sqlite3.Connection, inst_id: str,
                  event_ms: int) -> float | None:
    try:
        row = conn.execute(
            "SELECT funding_rate FROM funding_rates WHERE inst_id=? "
            "AND funding_time<=? ORDER BY funding_time DESC LIMIT 1",
            [inst_id, event_ms]).fetchone()
    except sqlite3.Error:
        return None
    return float(row[0]) if row and row[0] is not None else None


def _candidate_rows(conn: sqlite3.Connection, symbols: Iterable[str], *,
                    start_ts: float | None = None,
                    end_ts: float | None = None) -> list[dict[str, Any]]:
    symbols = tuple(symbols)
    placeholders = ",".join("?" for _ in symbols)
    clauses = [
        "strategy_id=?", "direction=?", "timeframe=?", "horizon_hours=?",
        f"symbol IN ({placeholders})",
    ]
    args: list[Any] = [
        STRATEGY_ID, DIRECTION, TIMEFRAME, HORIZON_HOURS, *symbols]
    if start_ts is not None:
        clauses.append("event_ts>=?")
        args.append(float(start_ts))
    if end_ts is not None:
        clauses.append("event_ts<?")
        args.append(float(end_ts))
    rows = conn.execute(
        "SELECT signal_id,symbol,event_ts,entry,atr FROM signal_samples WHERE " +
        " AND ".join(clauses) + " ORDER BY event_ts,symbol,signal_id", args
    ).fetchall()
    return [dict(row) for row in rows]


def _market_series(conn: sqlite3.Connection, table: str, inst_id: str,
                   bar: str) -> list[tuple[Any, ...]]:
    extra = " AND confirmed=1" if table == "klines_v2" else ""
    rows = conn.execute(
        f"SELECT open_time,open,high,low,close,volume FROM {table} "
        f"WHERE inst_id=? AND bar=?{extra} ORDER BY open_time",
        [inst_id, bar]).fetchall()
    # Confirmed v2 may contain multiple as-of rows for one candle.  Keeping the
    # last database row would itself be lookahead, so reject duplicate times.
    if len({int(row[0]) for row in rows}) != len(rows):
        raise ValueError("market input contains duplicate confirmed candles")
    return rows


def _evaluate_slice(replay: sqlite3.Connection, market: sqlite3.Connection,
                    table: str, symbols: Iterable[str], *,
                    start_ts: float | None = None,
                    end_ts: float | None = None
                    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
    candidates = _candidate_rows(
        replay, symbols, start_ts=start_ts, end_ts=end_ts)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[str(row["symbol"])].append(row)
    confirmed = 0
    missing_confirmation = 0
    missing_path = 0
    cooldown_skipped = 0
    resolved: list[dict[str, Any]] = []
    for symbol, rows in grouped.items():
        inst_id = f"{symbol}-USDT-SWAP"
        bars_15m = _market_series(market, table, inst_id, TIMEFRAME)
        confirmation_by_time = {int(row[0]): row for row in bars_15m}
        minute_bars = _market_series(market, table, inst_id, "1m")
        next_allowed_ms = 0
        for row in rows:
            event_ms = int(round(float(row["event_ts"]) * 1000))
            if event_ms < next_allowed_ms:
                cooldown_skipped += 1
                continue
            delayed = confirm_candidate(row, confirmation_by_time.get(event_ms))
            if delayed is None:
                missing_confirmation += 1
                continue
            confirmed += 1
            next_allowed_ms = int(delayed["entry_event_ms"]) + COOLDOWN_MS
            outcome = resolve_trade(
                delayed, minute_bars,
                funding_rate=_funding_asof(
                    market, inst_id, int(delayed["entry_event_ms"])))
            if outcome is None:
                missing_path += 1
            else:
                resolved.append(outcome)
    return {
        "raw_candidates": len(candidates), "confirmed": confirmed,
        "cooldown_skipped": cooldown_skipped,
        "missing_confirmation": missing_confirmation,
        "missing_path": missing_path,
    }, resolved


def summarize(rows: list[dict[str, Any]], *, stats: Mapping[str, int],
              folds: int, min_n: int, min_positive_folds: int,
              max_symbol_concentration: float | None = None,
              require_each_symbol_positive: Iterable[str] = ()) -> dict[str, Any]:
    result: dict[str, Any] = {
        **dict(stats), "complete": len(rows), "tp_first": 0, "sl_first": 0,
        "timeout": 0, "win_rate": None, "gross_ev_r": None,
        "net_ev_r": None, "clustered_event_net_ev_r": None,
        "net_ev_lower_95": None, "positive_folds": 0, "folds": [],
        "symbols": {}, "symbol_concentration": None, "gate_passed": False,
    }
    if not rows:
        result["status"] = "insufficient_data"
        return result
    result["tp_first"] = sum(row["outcome"] == "tp" for row in rows)
    result["sl_first"] = sum(row["outcome"] == "sl" for row in rows)
    result["timeout"] = sum(row["outcome"] == "timeout" for row in rows)
    result["win_rate"] = round(result["tp_first"] / len(rows), 6)
    result["gross_ev_r"] = round(statistics.fmean(
        float(row["gross_r"]) for row in rows), 6)
    result["net_ev_r"] = round(statistics.fmean(
        float(row["net_r"]) for row in rows), 6)

    events: dict[int, list[float]] = defaultdict(list)
    symbols: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        events[int(row["entry_event_ms"])].append(float(row["net_r"]))
        symbols[str(row["symbol"])].append(float(row["net_r"]))
    event_values = [statistics.fmean(values)
                    for _, values in sorted(events.items())]
    event_mean = statistics.fmean(event_values)
    variance = statistics.variance(event_values) if len(event_values) > 1 else 0
    lower = event_mean - config.ENTRY_MODEL_EV_Z * math.sqrt(
        variance / len(event_values))
    result["clustered_event_net_ev_r"] = round(event_mean, 6)
    result["net_ev_lower_95"] = round(lower, 6)
    result["symbols"] = {
        symbol: {"n": len(values),
                 "net_ev_r": round(statistics.fmean(values), 6)}
        for symbol, values in sorted(symbols.items())}
    positive = [sum(values) for values in symbols.values() if sum(values) > 0]
    concentration = max(positive) / sum(positive) if positive else 1.0
    result["symbol_concentration"] = round(concentration, 6)

    ordered = sorted(events.items())
    block = len(ordered) // folds
    if block:
        for index in range(folds):
            lo = index * block
            hi = (index + 1) * block if index < folds - 1 else len(ordered)
            values = [value for _, event in ordered[lo:hi] for value in event]
            result["folds"].append({
                "fold": index, "n": len(values),
                "net_ev_r": round(statistics.fmean(values), 6),
            })
    result["positive_folds"] = sum(
        fold["net_ev_r"] > 0 for fold in result["folds"])
    required_symbols = tuple(require_each_symbol_positive)
    each_symbol_positive = (not required_symbols or (
        set(result["symbols"]) == set(required_symbols) and
        all(result["symbols"][symbol]["net_ev_r"] > 0
            for symbol in required_symbols)))
    concentration_ok = (max_symbol_concentration is None or
                        concentration <= max_symbol_concentration)
    result["gate_passed"] = bool(
        len(rows) >= min_n and result["net_ev_r"] > 0 and lower > 0 and
        len(result["folds"]) == folds and
        result["positive_folds"] >= min_positive_folds and
        concentration_ok and each_symbol_positive)
    result["status"] = "pass" if result["gate_passed"] else "stop_no_promotion"
    return result


def evaluate(replay_db: str, market_db: str) -> dict[str, Any]:
    replay_path, market_path, replay, market, proof = _open_inputs(
        replay_db, market_db)
    try:
        table = _preferred_kline_table(market)
        dev_stats, dev_rows = _evaluate_slice(
            replay, market, table, DEVELOPMENT_SYMBOLS,
            end_ts=VALIDATION_CUTOFF_TS)
        development = summarize(
            dev_rows, stats=dev_stats, folds=5, min_n=100,
            min_positive_folds=4, max_symbol_concentration=0.50)
        if not development["gate_passed"]:
            late_validation = {"status": "sealed_not_opened"}
            holdout = {"status": "sealed_not_opened",
                       "symbols": list(HOLDOUT_SYMBOLS)}
            verdict = "stop_no_promotion"
        else:
            late_stats, late_rows = _evaluate_slice(
                replay, market, table, DEVELOPMENT_SYMBOLS,
                start_ts=VALIDATION_CUTOFF_TS)
            late_validation = summarize(
                late_rows, stats=late_stats, folds=4, min_n=50,
                min_positive_folds=3, max_symbol_concentration=0.50)
            if not late_validation["gate_passed"]:
                holdout = {"status": "sealed_not_opened",
                           "symbols": list(HOLDOUT_SYMBOLS)}
                verdict = "stop_no_promotion"
            else:
                holdout_stats, holdout_rows = _evaluate_slice(
                    replay, market, table, HOLDOUT_SYMBOLS)
                holdout = summarize(
                    holdout_rows, stats=holdout_stats, folds=4, min_n=30,
                    min_positive_folds=3,
                    require_each_symbol_positive=HOLDOUT_SYMBOLS)
                verdict = (
                    "eligible_for_separate_paper_shadow_review"
                    if holdout["gate_passed"] else "stop_no_promotion")
    finally:
        replay.close()
        market.close()
    return {
        "policy_version": POLICY_VERSION, "research_only": True,
        "execution_authority": False, "budget_expansion_allowed": False,
        "replay_db": str(replay_path), "replay_sha256": _sha256(replay_path),
        "market_db": str(market_path), "market_sha256": _sha256(market_path),
        "market_input_version": MARKET_INPUT_VERSION, "market_table": table,
        "replay_provenance": dict(proof),
        "policy": {
            "source_strategy": STRATEGY_ID, "direction": DIRECTION,
            "confirmation": "next_closed_15m_bull_body_and_close_above_breakout",
            "entry": "following_1m_open", "stop_atr": 1.0, "tp_atr": 2.0,
            "horizon_hours": HORIZON_HOURS,
            "same_minute_tie": "sl_first",
            "symbol_cooldown_hours": HORIZON_HOURS,
            "cost_model_version": config.ENTRY_COST_MODEL_VERSION,
        },
        "split": {
            "validation_cutoff_ts": VALIDATION_CUTOFF_TS,
            "development_symbols": list(DEVELOPMENT_SYMBOLS),
            "holdout_symbols": list(HOLDOUT_SYMBOLS),
        },
        "development": development, "late_validation": late_validation,
        "holdout": holdout, "verdict": verdict,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-db", required=True,
                        help="isolated A/B research replay database")
    parser.add_argument("--market-db", required=True,
                        help="matching isolated public-market database")
    args = parser.parse_args()
    print(json.dumps(evaluate(args.replay_db, args.market_db),
                     ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

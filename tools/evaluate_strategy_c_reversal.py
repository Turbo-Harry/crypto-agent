#!/usr/bin/env python3
"""Research-only evaluation of a predeclared 15m extreme-reversal strategy.

The tool reads an isolated public-market SQLite database and emits evidence.
It never imports an execution adapter, writes model state, or changes trading
configuration.  Development symbols are evaluated first; holdout symbols stay
sealed unless the development gate passes.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from decision.entry_probability import cost_breakdown_r


BAR_15M_MS = 15 * 60_000
MINUTE_MS = 60_000
HORIZON_HOURS = 4
RSI_PERIOD = 14
RSI_LONG_MAX = 25.0
RSI_SHORT_MIN = 75.0
BB_PERIOD = 20
BB_STDDEV = 2.0
ADX_PERIOD = 14
ADX_MAX = 20.0
DEVELOPMENT = (
    "BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX")
HOLDOUT = ("BNB", "LTC")
POLICY_VERSION = "strategy-c-extreme-reversal-v1-predeclared"
MARKET_INPUT_VERSION = "confirmed-klines-v2-preferred"


def _preferred_kline_table(conn: sqlite3.Connection) -> str:
    has_confirmed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='klines_v2'").fetchone()
    return "klines_v2" if has_confirmed else "klines"


def _wilder(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    average = sum(values[:period]) / period
    out[period - 1] = average
    for index in range(period, len(values)):
        average = (average * (period - 1) + values[index]) / period
        out[index] = average
    return out


def _indicators(bars: list[tuple[Any, ...]]) -> dict[str, list[float | None]]:
    """Return causal RSI/ATR/ADX/Bollinger series for closed 15m bars."""
    size = len(bars)
    closes = [float(row[4]) for row in bars]
    gains = [0.0] * size
    losses = [0.0] * size
    true_range = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for index in range(1, size):
        change = closes[index] - closes[index - 1]
        gains[index] = max(0.0, change)
        losses[index] = max(0.0, -change)
        high, low = float(bars[index][2]), float(bars[index][3])
        previous_close = closes[index - 1]
        true_range[index] = max(
            high - low, abs(high - previous_close), abs(low - previous_close))
        up = high - float(bars[index - 1][2])
        down = float(bars[index - 1][3]) - low
        plus_dm[index] = up if up > down and up > 0 else 0.0
        minus_dm[index] = down if down > up and down > 0 else 0.0

    avg_gain = _wilder(gains[1:], RSI_PERIOD)
    avg_loss = _wilder(losses[1:], RSI_PERIOD)
    rsi: list[float | None] = [None] * size
    for index in range(RSI_PERIOD, size):
        gain = avg_gain[index - 1]
        loss = avg_loss[index - 1]
        if gain is None or loss is None:
            continue
        if loss == 0:
            rsi[index] = 100.0
        elif gain == 0:
            rsi[index] = 0.0
        else:
            rsi[index] = 100 - 100 / (1 + gain / loss)

    atr_raw = _wilder(true_range[1:], ADX_PERIOD)
    plus_raw = _wilder(plus_dm[1:], ADX_PERIOD)
    minus_raw = _wilder(minus_dm[1:], ADX_PERIOD)
    atr: list[float | None] = [None] * size
    dx: list[float | None] = [None] * size
    for index in range(ADX_PERIOD, size):
        tr = atr_raw[index - 1]
        plus = plus_raw[index - 1]
        minus = minus_raw[index - 1]
        if tr is None or plus is None or minus is None or tr <= 0:
            continue
        atr[index] = tr
        plus_di, minus_di = 100 * plus / tr, 100 * minus / tr
        denominator = plus_di + minus_di
        dx[index] = (100 * abs(plus_di - minus_di) / denominator
                     if denominator > 0 else 0.0)
    adx: list[float | None] = [None] * size
    first_dx = [value for value in dx[ADX_PERIOD:] if value is not None]
    smoothed_dx = _wilder(first_dx, ADX_PERIOD)
    cursor = 0
    for index in range(ADX_PERIOD, size):
        if dx[index] is None:
            continue
        value = smoothed_dx[cursor] if cursor < len(smoothed_dx) else None
        if value is not None:
            adx[index] = value
        cursor += 1

    lower: list[float | None] = [None] * size
    upper: list[float | None] = [None] * size
    for index in range(BB_PERIOD - 1, size):
        window = closes[index - BB_PERIOD + 1:index + 1]
        mean = sum(window) / BB_PERIOD
        variance = sum((value - mean) ** 2 for value in window) / BB_PERIOD
        deviation = math.sqrt(variance)
        lower[index] = mean - BB_STDDEV * deviation
        upper[index] = mean + BB_STDDEV * deviation
    return {"rsi": rsi, "atr": atr, "adx": adx,
            "bb_lower": lower, "bb_upper": upper}


def candidate_direction(bar: tuple[Any, ...], *, rsi: float | None,
                        atr: float | None, adx: float | None,
                        bb_lower: float | None,
                        bb_upper: float | None) -> str | None:
    """Apply the immutable predeclared candidate rule to one closed bar."""
    if any(value is None for value in (rsi, atr, adx, bb_lower, bb_upper)):
        return None
    open_price, high, low, close = map(float, bar[1:5])
    if atr <= 0 or close <= 0 or adx > ADX_MAX:
        return None
    body = abs(close - open_price)
    lower_wick = min(open_price, close) - low
    upper_wick = high - max(open_price, close)
    if rsi <= RSI_LONG_MAX and close <= bb_lower and lower_wick >= body:
        return "long"
    if rsi >= RSI_SHORT_MIN and close >= bb_upper and upper_wick >= body:
        return "short"
    return None


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


def _detect_symbol(conn: sqlite3.Connection, symbol: str,
                   kline_table: str) -> list[dict[str, Any]]:
    inst_id = f"{symbol}-USDT-SWAP"
    bars = conn.execute(
        f"SELECT open_time,open,high,low,close FROM {kline_table} WHERE inst_id=? "
        "AND bar='15m' ORDER BY open_time", [inst_id]).fetchall()
    indicators = _indicators(bars)
    candidates = []
    next_allowed_ms = 0
    for index, bar in enumerate(bars):
        event_ms = int(bar[0]) + BAR_15M_MS
        if event_ms < next_allowed_ms:
            continue
        direction = candidate_direction(
            bar, rsi=indicators["rsi"][index],
            atr=indicators["atr"][index], adx=indicators["adx"][index],
            bb_lower=indicators["bb_lower"][index],
            bb_upper=indicators["bb_upper"][index])
        if direction is None:
            continue
        candidates.append({
            "symbol": symbol, "inst_id": inst_id, "event_ms": event_ms,
            "direction": direction, "atr": indicators["atr"][index],
            "rsi": indicators["rsi"][index], "adx": indicators["adx"][index],
            "funding_rate": _funding_asof(conn, inst_id, event_ms),
        })
        next_allowed_ms = event_ms + HORIZON_HOURS * 3_600_000
    return candidates


def resolve_candidate(candidate: dict[str, Any],
                      minute_bars: list[tuple[Any, ...]]) -> dict[str, Any] | None:
    """Resolve one candidate from the next minute with adverse tie ordering."""
    event_ms = int(candidate["event_ms"])
    end_ms = event_ms + HORIZON_HOURS * 3_600_000
    times = [int(row[0]) for row in minute_bars]
    lo = bisect.bisect_left(times, event_ms)
    hi = bisect.bisect_left(times, end_ms)
    path = minute_bars[lo:hi]
    expected = HORIZON_HOURS * 60
    if (len(path) < expected or not path or int(path[0][0]) != event_ms or
            int(path[-1][0]) + MINUTE_MS != end_ms or
            any(int(right[0]) - int(left[0]) != MINUTE_MS
                for left, right in zip(path, path[1:]))):
        return None
    entry = float(path[0][1])
    risk = float(candidate["atr"])
    direction = str(candidate["direction"])
    if entry <= 0 or risk <= 0 or direction not in ("long", "short"):
        return None
    stop = entry - risk if direction == "long" else entry + risk
    tp = entry + 2 * risk if direction == "long" else entry - 2 * risk
    exit_price = None
    outcome = "timeout"
    for bar in path:
        high, low = float(bar[2]), float(bar[3])
        sl_hit = low <= stop if direction == "long" else high >= stop
        tp_hit = high >= tp if direction == "long" else low <= tp
        if sl_hit:
            exit_price, outcome = stop, "sl"
            break
        if tp_hit:
            exit_price, outcome = tp, "tp"
            break
    if exit_price is None:
        exit_price = float(path[-1][4])
    gross_r = ((exit_price - entry) / risk if direction == "long"
               else (entry - exit_price) / risk)
    costs = cost_breakdown_r({
        "entry": entry, "stop": stop, "direction": direction,
        "horizon_hours": HORIZON_HOURS,
        "funding_rate": candidate.get("funding_rate"),
    })
    if costs is None:
        return None
    return {
        **candidate, "entry": entry, "stop": stop, "tp": tp,
        "outcome": outcome, "gross_r": gross_r,
        "cost_r": costs["total_cost_r"],
        "net_r": gross_r - costs["total_cost_r"],
    }


def _cluster_lower(rows: list[dict[str, Any]]) -> tuple[float, float]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        grouped[int(row["event_ms"])].append(float(row["net_r"]))
    values = [sum(group) / len(group) for _, group in sorted(grouped.items())]
    mean = sum(values) / len(values)
    variance = (sum((value - mean) ** 2 for value in values) /
                (len(values) - 1) if len(values) > 1 else 0.0)
    lower = mean - config.ENTRY_MODEL_EV_Z * math.sqrt(
        variance / len(values))
    return mean, lower


def summarize(rows: list[dict[str, Any]], *, candidates: int,
              missing_path: int, holdout: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidates": candidates, "complete": len(rows),
        "missing_path": missing_path, "tp_first": 0, "sl_first": 0,
        "timeout": 0, "win_rate": None, "gross_ev_r": None,
        "net_ev_r": None, "clustered_event_net_ev_r": None,
        "net_ev_lower_95": None, "positive_folds": 0, "folds": [],
        "months": {}, "symbols": {}, "symbol_concentration": None,
        "gate_passed": False,
    }
    if not rows:
        result["status"] = "insufficient_data"
        return result
    result["tp_first"] = sum(row["outcome"] == "tp" for row in rows)
    result["sl_first"] = sum(row["outcome"] == "sl" for row in rows)
    result["timeout"] = sum(row["outcome"] == "timeout" for row in rows)
    result["win_rate"] = round(result["tp_first"] / len(rows), 6)
    result["gross_ev_r"] = round(
        sum(float(row["gross_r"]) for row in rows) / len(rows), 6)
    result["net_ev_r"] = round(
        sum(float(row["net_r"]) for row in rows) / len(rows), 6)
    cluster_mean, lower = _cluster_lower(rows)
    result["clustered_event_net_ev_r"] = round(cluster_mean, 6)
    result["net_ev_lower_95"] = round(lower, 6)

    symbols: dict[str, list[float]] = defaultdict(list)
    months: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        symbols[str(row["symbol"])].append(float(row["net_r"]))
        months[time.strftime("%Y-%m", time.gmtime(
            int(row["event_ms"]) / 1000))].append(float(row["net_r"]))
    result["symbols"] = {
        name: {"n": len(values), "net_ev_r": round(sum(values) / len(values), 6)}
        for name, values in sorted(symbols.items())}
    result["months"] = {
        name: {"n": len(values), "net_ev_r": round(sum(values) / len(values), 6)}
        for name, values in sorted(months.items())}
    positive = [sum(values) for values in symbols.values() if sum(values) > 0]
    concentration = max(positive) / sum(positive) if positive else 1.0
    result["symbol_concentration"] = round(concentration, 6)

    events: list[list[dict[str, Any]]] = []
    for row in sorted(rows, key=lambda item: (
            int(item["event_ms"]), str(item["symbol"]))):
        if not events or int(events[-1][0]["event_ms"]) != int(row["event_ms"]):
            events.append([])
        events[-1].append(row)
    fold_count = int(config.FACTOR_WALK_FORWARD_FOLDS)
    block = len(events) // fold_count
    if block:
        for fold in range(fold_count):
            lo = fold * block
            hi = (fold + 1) * block if fold < fold_count - 1 else len(events)
            material = [row for event in events[lo:hi] for row in event]
            value = sum(float(row["net_r"]) for row in material) / len(material)
            result["folds"].append({
                "fold": fold, "n": len(material), "net_ev_r": round(value, 6)})
    result["positive_folds"] = sum(
        fold["net_ev_r"] > 0 for fold in result["folds"])
    if holdout:
        passed = len(rows) >= 30 and result["net_ev_r"] > 0 and lower > 0
    else:
        passed = (
            len(rows) >= 100 and len(result["folds"]) == fold_count and
            result["positive_folds"] >= config.FACTOR_MIN_CONSISTENT_FOLDS and
            lower > 0 and
            concentration <= config.FACTOR_MAX_SYMBOL_CONCENTRATION)
    result["gate_passed"] = passed
    result["status"] = "pass" if passed else "stop_no_promotion"
    return result


def _evaluate_symbols(conn: sqlite3.Connection,
                      symbols: Iterable[str],
                      kline_table: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for symbol in symbols:
        candidates.extend(_detect_symbol(conn, symbol, kline_table))
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_symbol[str(row["symbol"])].append(row)
    resolved = []
    missing = 0
    for symbol, material in by_symbol.items():
        lo = min(int(row["event_ms"]) for row in material)
        hi = max(int(row["event_ms"]) for row in material) + \
            HORIZON_HOURS * 3_600_000
        minute_bars = conn.execute(
            f"SELECT open_time,open,high,low,close FROM {kline_table} "
            "WHERE inst_id=? "
            "AND bar='1m' AND open_time>=? AND open_time<? ORDER BY open_time",
            [f"{symbol}-USDT-SWAP", lo, hi]).fetchall()
        for candidate in material:
            outcome = resolve_candidate(candidate, minute_bars)
            if outcome is None:
                missing += 1
            else:
                resolved.append(outcome)
    return {"candidates": len(candidates), "missing_path": missing}, resolved


def evaluate(market_db: str) -> dict[str, Any]:
    path = Path(market_db).expanduser().resolve()
    if not path.is_file() or path.name in {"crypto_agent.db", "crypto_agent_live.db"}:
        raise ValueError("market_db must be an isolated research database")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        kline_table = _preferred_kline_table(conn)
        meta, rows = _evaluate_symbols(conn, DEVELOPMENT, kline_table)
        development = summarize(
            rows, candidates=meta["candidates"],
            missing_path=meta["missing_path"])
        if not development["gate_passed"]:
            holdout = {"status": "sealed_not_opened", "symbols": list(HOLDOUT)}
            verdict = "stop_no_promotion"
        else:
            holdout_meta, holdout_rows = _evaluate_symbols(
                conn, HOLDOUT, kline_table)
            holdout = summarize(
                holdout_rows, candidates=holdout_meta["candidates"],
                missing_path=holdout_meta["missing_path"], holdout=True)
            verdict = ("eligible_for_separate_paper_shadow_review"
                       if holdout["gate_passed"] else "stop_no_promotion")
    finally:
        conn.close()
    return {
        "generated_ts": time.time(), "policy_version": POLICY_VERSION,
        "research_only": True, "market_db": str(path), "data_hash": digest,
        "market_input_version": MARKET_INPUT_VERSION,
        "market_table": kline_table,
        "policy": {
            "timeframe": "15m", "entry": "next_1m_open",
            "rsi": {"period": RSI_PERIOD, "long_max": RSI_LONG_MAX,
                    "short_min": RSI_SHORT_MIN},
            "bollinger": {"period": BB_PERIOD, "stddev": BB_STDDEV},
            "adx": {"period": ADX_PERIOD, "max": ADX_MAX},
            "wick_body_min_ratio": 1.0, "stop_atr": 1.0, "tp_atr": 2.0,
            "horizon_hours": HORIZON_HOURS,
            "same_minute_tie": "sl_first", "symbol_cooldown_hours": 4,
            "cost_model_version": config.ENTRY_COST_MODEL_VERSION,
        },
        "development_symbols": list(DEVELOPMENT), "development": development,
        "holdout": holdout, "verdict": verdict,
        "execution_authority": False, "budget_expansion_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate predeclared research-only strategy C")
    parser.add_argument("--market-db", required=True,
                        help="isolated market SQLite with 15m/1m/funding data")
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args()
    result = evaluate(args.market_db)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()

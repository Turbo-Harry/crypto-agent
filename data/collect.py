"""Strict OKX SWAP market-data collector.

The legacy ``klines`` table is never modified. New data is written to
``klines_v2`` only after OKX marks a candle final (``confirm=1``). A daily
reconciliation mode downloads the complete previous UTC day from
``history-candles`` and audits every expected timestamp.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.market_data import (BAR_MS, audit_window, connect, parse_okx_rows,
                              record_run, sync_source_gaps, upsert_rows)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "market.db")
BASE = "https://www.okx.com"
ALL_BARS = ("1m", "15m", "1H", "4H", "1D")
OKX_BAR = {"1D": "1Dutc"}
REQUEST_INTERVAL_SECONDS = 0.11
REQUEST_RETRIES = 4

_rate_lock = threading.Lock()
_next_request_at = [0.0]


def _get(url: str, timeout: float = 20.0) -> dict:
    last_error = None
    for attempt in range(REQUEST_RETRIES):
        with _rate_lock:
            wait = max(0.0, _next_request_at[0] - time.monotonic())
            if wait:
                time.sleep(wait)
            _next_request_at[0] = time.monotonic() + REQUEST_INTERVAL_SECONDS
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if str(payload.get("code")) != "0":
                raise RuntimeError(
                    f"OKX error {payload.get('code')}: {payload.get('msg')}")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt + 1 < REQUEST_RETRIES:
                time.sleep(min(2.0, 0.25 * (2 ** attempt)))
    raise RuntimeError(f"request failed after retries: {last_error}")


def fetch_swap_symbols(top_n: int, *, include_crypto: bool,
                       include_stocks: bool) -> list[str]:
    instruments = _get(
        f"{BASE}/api/v5/public/instruments?instType=SWAP").get("data") or []
    tickers = _get(
        f"{BASE}/api/v5/market/tickers?instType=SWAP").get("data") or []
    turnover = {}
    for ticker in tickers:
        try:
            turnover[ticker["instId"]] = (
                float(ticker.get("volCcy24h") or 0) *
                float(ticker.get("last") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    stock_bases = set(config.STOCK_SWAP_TOKENS)
    crypto = []
    stocks = []
    for item in instruments:
        inst_id = str(item.get("instId") or "")
        if (item.get("state") != "live" or item.get("settleCcy") != "USDT"
                or not inst_id.endswith("-USDT-SWAP")):
            continue
        base = inst_id.split("-")[0]
        if base in stock_bases:
            stocks.append(inst_id)
            continue
        if (base in config.STABLECOINS or
                any(base.endswith(suffix) for suffix in config.LEVERAGED_SUFFIX)):
            continue
        crypto.append(inst_id)
    crypto.sort(key=lambda name: turnover.get(name, 0), reverse=True)
    stocks.sort(key=lambda name: turnover.get(name, 0), reverse=True)
    result = []
    if include_crypto:
        result.extend(crypto[:max(1, int(top_n))])
    if include_stocks:
        result.extend(stocks)
    return list(dict.fromkeys(result))


def fetch_recent(inst_id: str, bar: str) -> tuple[list[list[str]], int]:
    params = urllib.parse.urlencode({
        "instId": inst_id, "bar": OKX_BAR.get(bar, bar), "limit": "300"})
    payload = _get(f"{BASE}/api/v5/market/candles?{params}")
    return payload.get("data") or [], 1


def fetch_history_window(inst_id: str, bar: str, start_ms: int,
                         end_ms: int) -> tuple[list[list[str]], int]:
    """Download a closed UTC window, paging older from its exclusive end."""
    target = max(1, (int(end_ms) - int(start_ms)) // BAR_MS[bar])
    max_pages = (target + 99) // 100 + 4
    cursor = int(end_ms)
    unique: dict[int, list[str]] = {}
    requests = 0
    for _ in range(max_pages):
        params = urllib.parse.urlencode({
            "instId": inst_id, "bar": OKX_BAR.get(bar, bar),
            "limit": "100", "after": str(cursor)})
        page = (_get(f"{BASE}/api/v5/market/history-candles?{params}")
                .get("data") or [])
        requests += 1
        if not page:
            break
        timestamps = []
        for row in page:
            try:
                ts = int(row[0])
                timestamps.append(ts)
                if int(start_ms) <= ts < int(end_ms):
                    unique[ts] = row
            except (IndexError, TypeError, ValueError):
                continue
        if not timestamps:
            break
        oldest = min(timestamps)
        if oldest < int(start_ms) or oldest >= cursor:
            break
        cursor = oldest
    return [unique[key] for key in sorted(unique)], requests


def _bars(value: str | None) -> list[str]:
    result = ([item.strip() for item in value.split(",") if item.strip()]
              if value else list(ALL_BARS))
    unknown = [item for item in result if item not in BAR_MS]
    if unknown:
        raise ValueError(f"unsupported bars: {unknown}")
    return result


def _symbols(args) -> list[str]:
    if args.inst:
        symbols = [item.strip() for item in args.inst.split(",") if item.strip()]
        bad = [item for item in symbols if not item.endswith("-USDT-SWAP")]
        if bad:
            raise ValueError(f"only *-USDT-SWAP is accepted: {bad}")
        return list(dict.fromkeys(symbols))
    include_crypto = bool(args.crypto or args.all or
                          (not args.crypto and not args.stocks))
    include_stocks = bool(args.stocks or args.all or
                          (not args.crypto and not args.stocks))
    return fetch_swap_symbols(
        args.top or config.OBSERVE_POOL_SIZE,
        include_crypto=include_crypto, include_stocks=include_stocks)


def _window(date_text: str) -> tuple[int, int]:
    day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(day.timestamp() * 1000)
    return start, start + 86_400_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bar", help="single bar")
    parser.add_argument("--bars", help="comma-separated bars")
    parser.add_argument("--inst", help="comma-separated *-USDT-SWAP ids")
    parser.add_argument("--top", type=int)
    parser.add_argument("--stocks", action="store_true")
    parser.add_argument("--crypto", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--threads", type=int, default=5)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--reconcile-date",
        help="UTC date YYYY-MM-DD; use history-candles and require full coverage")
    args = parser.parse_args(argv)
    bars = _bars(args.bars or args.bar)
    symbols = _symbols(args)
    if not symbols:
        raise RuntimeError("empty SWAP universe")
    mode = "reconcile" if args.reconcile_date else "incremental"
    start_ms = end_ms = None
    if args.reconcile_date:
        start_ms, end_ms = _window(args.reconcile_date)

    started = time.time()
    run_id = f"market-{int(started * 1000)}-{uuid.uuid4().hex[:8]}"
    conn = connect(os.path.abspath(args.db))
    totals = {"requested_series": len(symbols) * len(bars),
              "successful_series": 0, "failed_series": 0,
              "received_rows": 0, "confirmed_rows": 0,
              "inserted_rows": 0, "updated_rows": 0,
              "invalid_rows": 0, "requests": 0}
    errors = []

    def download(inst_id: str, bar: str):
        if args.reconcile_date:
            raw, requests = fetch_history_window(
                inst_id, bar, int(start_ms), int(end_ms))
        else:
            raw, requests = fetch_recent(inst_id, bar)
        parsed, parse_stats = parse_okx_rows(
            inst_id, bar, raw, as_of_ms=end_ms if args.reconcile_date else None,
            start_ms=start_ms, end_ms=end_ms)
        source_gaps: list[int] = []
        if args.reconcile_date:
            by_time = {int(row["open_time"]): row for row in parsed}
            expected_times = range(int(start_ms), int(end_ms), BAR_MS[bar])
            missing = [ts for ts in expected_times if ts not in by_time]
            if missing:
                # A missing source bar is data, not a zero candle.  Confirm it
                # with an independent timestamp-scoped history request before
                # acknowledging the gap.  Large gaps use a second full scan to
                # avoid thousands of point requests (for example a new listing).
                windows = ([(ts, ts + BAR_MS[bar]) for ts in missing]
                           if len(missing) <= 50 else
                           [(int(start_ms), int(end_ms))])
                for gap_start, gap_end in windows:
                    retry_raw, retry_requests = fetch_history_window(
                        inst_id, bar, gap_start, gap_end)
                    requests += retry_requests
                    retry_parsed, retry_stats = parse_okx_rows(
                        inst_id, bar, retry_raw, as_of_ms=end_ms,
                        start_ms=start_ms, end_ms=end_ms)
                    parse_stats["received"] += retry_stats["received"]
                    parse_stats["invalid"] += retry_stats["invalid"]
                    for row in retry_parsed:
                        by_time[int(row["open_time"])] = row
                parsed = [by_time[key] for key in sorted(by_time)]
                source_gaps = [ts for ts in expected_times if ts not in by_time]
                parse_stats["confirmed"] = len(parsed)
        return inst_id, bar, parsed, parse_stats, requests, source_gaps

    tasks = [(inst_id, bar) for inst_id in symbols for bar in bars]
    with ThreadPoolExecutor(max_workers=max(1, min(int(args.threads), 12))) as pool:
        futures = {pool.submit(download, inst_id, bar): (inst_id, bar)
                   for inst_id, bar in tasks}
        for future in as_completed(futures):
            inst_id, bar = futures[future]
            try:
                _, _, parsed, parse_stats, requests, source_gaps = future.result()
                totals["requests"] += requests
                totals["received_rows"] += parse_stats["received"]
                totals["confirmed_rows"] += parse_stats["confirmed"]
                totals["invalid_rows"] += parse_stats["invalid"]
                if not parsed or parse_stats["invalid"]:
                    raise RuntimeError(
                        f"confirmed={len(parsed)} invalid={parse_stats['invalid']}")
                saved = upsert_rows(conn, parsed)
                if args.reconcile_date:
                    sync_source_gaps(
                        conn, inst_id, bar, int(start_ms), int(end_ms),
                        source_gaps)
                totals["inserted_rows"] += saved["inserted"]
                totals["updated_rows"] += saved["updated"]
                totals["successful_series"] += 1
            except Exception as exc:
                totals["failed_series"] += 1
                errors.append(f"{inst_id}:{bar}: {type(exc).__name__}: {exc}")

    audit = None
    if args.reconcile_date:
        audit = audit_window(conn, symbols, bars, int(start_ms), int(end_ms))
        if not audit["complete"]:
            errors.append(
                f"quality_gate: unexplained_missing="
                f"{audit['unexplained_missing']} bad={audit['bad']}")
    status = "success" if not errors else "failed"
    record_run(conn, {
        "run_id": run_id, "started_at": started, "finished_at": time.time(),
        "mode": mode, "target_date": args.reconcile_date,
        **{key: totals[key] for key in (
            "requested_series", "successful_series", "failed_series",
            "received_rows", "confirmed_rows", "inserted_rows",
            "updated_rows", "invalid_rows")},
        "status": status,
        "details": {"bars": bars, "symbols": symbols, "errors": errors,
                    "audit": audit},
    })
    conn.close()
    report = {"status": status, "mode": mode,
              "db": os.path.abspath(args.db), "bars": bars,
              "symbols": len(symbols), **totals,
              "audit": ({"complete": audit["complete"],
                         "missing": audit["missing"],
                         "source_gaps": audit["source_gaps"],
                         "unexplained_missing": audit["unexplained_missing"],
                         "bad": audit["bad"]}
                        if audit else None), "errors": errors[:20],
              "elapsed_seconds": round(time.time() - started, 2)}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

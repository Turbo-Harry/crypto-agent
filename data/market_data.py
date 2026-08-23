"""Strict, audit-friendly market data persistence primitives.

``klines`` is the legacy snapshot table and is intentionally left untouched.
New collection writes ``klines_v2`` only: confirmed OKX bars, explicit venue
identity, ingestion as-of, and final-value UPSERT semantics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from typing import Any, Iterable


BAR_MS = {
    "1m": 60_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines_v2 (
    source TEXT NOT NULL,
    venue TEXT NOT NULL,
    time_zone TEXT NOT NULL DEFAULT 'UTC' CHECK (time_zone = 'UTC'),
    inst_id TEXT NOT NULL,
    bar TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    quote_volume REAL NOT NULL,
    confirmed INTEGER NOT NULL CHECK (confirmed = 1),
    ingested_at REAL NOT NULL,
    as_of_ms INTEGER NOT NULL,
    raw_hash TEXT NOT NULL,
    PRIMARY KEY (source, venue, inst_id, bar, open_time)
);
CREATE INDEX IF NOT EXISTS idx_klines_v2_series_time
ON klines_v2(source, venue, inst_id, bar, open_time);

CREATE TABLE IF NOT EXISTS market_collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    mode TEXT NOT NULL,
    target_date TEXT,
    requested_series INTEGER NOT NULL DEFAULT 0,
    successful_series INTEGER NOT NULL DEFAULT 0,
    failed_series INTEGER NOT NULL DEFAULT 0,
    received_rows INTEGER NOT NULL DEFAULT 0,
    confirmed_rows INTEGER NOT NULL DEFAULT 0,
    inserted_rows INTEGER NOT NULL DEFAULT 0,
    updated_rows INTEGER NOT NULL DEFAULT 0,
    invalid_rows INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_datasets (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    venue TEXT NOT NULL,
    time_zone TEXT NOT NULL,
    notes TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS market_data_gaps (
    source TEXT NOT NULL,
    venue TEXT NOT NULL,
    time_zone TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    bar TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    reason TEXT NOT NULL,
    first_observed_at REAL NOT NULL,
    last_checked_at REAL NOT NULL,
    check_count INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY (source, venue, inst_id, bar, open_time)
);
CREATE INDEX IF NOT EXISTS idx_market_data_gaps_series_time
ON market_data_gaps(source, venue, inst_id, bar, open_time);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    # Early v2 deployments predated the explicit timezone column.  Keep the
    # migration additive so a running collector never needs a destructive DB
    # rebuild.
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(klines_v2)")}
    if "time_zone" not in columns:
        conn.execute(
            "ALTER TABLE klines_v2 ADD COLUMN time_zone TEXT NOT NULL DEFAULT 'UTC'")
    now = time.time()
    conn.executemany(
        "INSERT INTO market_datasets(name,status,source,venue,time_zone,notes,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
        "status=excluded.status,source=excluded.source,venue=excluded.venue,"
        "time_zone=excluded.time_zone,notes=excluded.notes,updated_at=excluded.updated_at",
        [
            ("klines", "legacy_unverified", "mixed", "mixed", "unknown",
             "Legacy snapshot table may contain unclosed bars; excluded from current research.", now),
            ("klines_v2", "strict_confirmed", "okx", "swap", "UTC",
             "Confirmed final OKX USDT-SWAP bars with UPSERT and audit lineage.", now),
        ],
    )
    conn.commit()
    return conn


def _raw_hash(values: list[Any]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_okx_rows(inst_id: str, bar: str, raw_rows: Iterable[list[Any]], *,
                   as_of_ms: int | None = None,
                   start_ms: int | None = None,
                   end_ms: int | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Normalize only final OKX SWAP bars; malformed rows remain observable."""
    if bar not in BAR_MS:
        raise ValueError(f"unsupported bar: {bar}")
    if not inst_id.endswith("-USDT-SWAP"):
        raise ValueError(f"strict market data requires SWAP inst_id: {inst_id}")
    as_of = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
    stats = {"received": 0, "confirmed": 0, "unconfirmed": 0,
             "invalid": 0, "outside_window": 0}
    unique: dict[int, dict[str, Any]] = {}
    for raw in raw_rows:
        stats["received"] += 1
        try:
            # OKX row[8] is 1 only after the candle is final. Missing confirm is
            # never assumed safe: old/partial payloads fail closed.
            if len(raw) <= 8 or str(raw[8]) != "1":
                stats["unconfirmed"] += 1
                continue
            open_time = int(raw[0])
            close_time = open_time + BAR_MS[bar]
            if close_time > as_of:
                stats["unconfirmed"] += 1
                continue
            if ((start_ms is not None and open_time < int(start_ms)) or
                    (end_ms is not None and open_time >= int(end_ms))):
                stats["outside_window"] += 1
                continue
            o, h, low, close = map(float, raw[1:5])
            volume = float(raw[5] or 0)
            quote_volume = float(raw[6] or 0)
            if (o <= 0 or close <= 0 or low <= 0 or h < low or
                    h < max(o, close) or low > min(o, close) or
                    volume < 0 or quote_volume < 0):
                stats["invalid"] += 1
                continue
            normalized = [open_time, o, h, low, close, volume, quote_volume]
            unique[open_time] = {
                "source": "okx", "venue": "swap", "inst_id": inst_id,
                "time_zone": "UTC", "bar": bar, "open_time": open_time,
                "close_time": close_time, "open": o, "high": h,
                "low": low, "close": close, "volume": volume,
                "quote_volume": quote_volume, "confirmed": 1,
                "ingested_at": time.time(), "as_of_ms": as_of,
                "raw_hash": _raw_hash(normalized),
            }
        except (IndexError, TypeError, ValueError):
            stats["invalid"] += 1
    stats["confirmed"] = len(unique)
    return [unique[key] for key in sorted(unique)], stats


_UPSERT_SQL = """
INSERT INTO klines_v2 (
    source,venue,time_zone,inst_id,bar,open_time,close_time,open,high,low,close,
    volume,quote_volume,confirmed,ingested_at,as_of_ms,raw_hash
) VALUES (
    :source,:venue,:time_zone,:inst_id,:bar,:open_time,:close_time,:open,:high,:low,:close,
    :volume,:quote_volume,:confirmed,:ingested_at,:as_of_ms,:raw_hash
)
ON CONFLICT(source,venue,inst_id,bar,open_time) DO UPDATE SET
    close_time=excluded.close_time, open=excluded.open, high=excluded.high,
    low=excluded.low, close=excluded.close, volume=excluded.volume,
    quote_volume=excluded.quote_volume, confirmed=1,
    time_zone=excluded.time_zone,
    ingested_at=excluded.ingested_at, as_of_ms=excluded.as_of_ms,
    raw_hash=excluded.raw_hash
WHERE klines_v2.raw_hash <> excluded.raw_hash
"""


def upsert_rows(conn: sqlite3.Connection,
                rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    materialized = list(rows)
    if not materialized:
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    existing: dict[tuple[str, str, str, str, int], str] = {}
    groups: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    for row in materialized:
        groups[(row["source"], row["venue"], row["inst_id"], row["bar"])].append(
            int(row["open_time"]))
    for (source, venue, inst_id, bar), times in groups.items():
        placeholders = ",".join("?" for _ in times)
        query = (
            "SELECT open_time,raw_hash FROM klines_v2 WHERE source=? AND venue=? "
            "AND inst_id=? AND bar=? AND open_time IN (" + placeholders + ")")
        for found in conn.execute(query, [source, venue, inst_id, bar, *times]):
            existing[(source, venue, inst_id, bar,
                      int(found["open_time"]))] = str(found["raw_hash"])
    inserted = updated = unchanged = 0
    for row in materialized:
        key = (row["source"], row["venue"], row["inst_id"], row["bar"],
               int(row["open_time"]))
        old_hash = existing.get(key)
        if old_hash is None:
            inserted += 1
        elif old_hash != row["raw_hash"]:
            updated += 1
        else:
            unchanged += 1
    with conn:
        conn.executemany(_UPSERT_SQL, materialized)
        # A later authoritative bar resolves a previously acknowledged source
        # gap.  Delete the stale exception in the same transaction.
        conn.executemany(
            "DELETE FROM market_data_gaps WHERE source=:source AND venue=:venue "
            "AND inst_id=:inst_id AND bar=:bar AND open_time=:open_time",
            materialized)
    return {"inserted": inserted, "updated": updated,
            "unchanged": unchanged}


def record_run(conn: sqlite3.Connection, run: dict[str, Any]) -> None:
    fields = (
        "run_id", "started_at", "finished_at", "mode", "target_date",
        "requested_series", "successful_series", "failed_series",
        "received_rows", "confirmed_rows", "inserted_rows", "updated_rows",
        "invalid_rows", "status", "details",
    )
    payload = dict(run)
    payload["details"] = json.dumps(payload.get("details") or {},
                                    ensure_ascii=False, sort_keys=True)
    placeholders = ",".join("?" for _ in fields)
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO market_collection_runs ({','.join(fields)}) "
            f"VALUES ({placeholders})", [payload.get(name) for name in fields])


def sync_source_gaps(conn: sqlite3.Connection, inst_id: str, bar: str,
                     start_ms: int, end_ms: int,
                     missing_times: Iterable[int], *,
                     checked_at: float | None = None) -> int:
    """Persist independently rechecked source gaps without inventing candles."""
    checked = float(checked_at if checked_at is not None else time.time())
    missing = sorted({int(value) for value in missing_times})
    if missing:
        placeholders = ",".join("?" for _ in missing)
        available = {int(row[0]) for row in conn.execute(
            "SELECT open_time FROM klines_v2 WHERE source='okx' AND venue='swap' "
            "AND inst_id=? AND bar=? AND open_time IN (" + placeholders + ")",
            [inst_id, bar, *missing])}
        # OKX candles and history-candles can disagree on sparse synthetic
        # instruments. A confirmed bar already persisted from either endpoint
        # wins over an endpoint-absence exception.
        missing = [value for value in missing if value not in available]
    with conn:
        if missing:
            placeholders = ",".join("?" for _ in missing)
            conn.execute(
                "DELETE FROM market_data_gaps WHERE source='okx' AND venue='swap' "
                "AND inst_id=? AND bar=? AND open_time>=? AND open_time<? "
                f"AND open_time NOT IN ({placeholders})",
                [inst_id, bar, int(start_ms), int(end_ms), *missing])
        else:
            conn.execute(
                "DELETE FROM market_data_gaps WHERE source='okx' AND venue='swap' "
                "AND inst_id=? AND bar=? AND open_time>=? AND open_time<?",
                [inst_id, bar, int(start_ms), int(end_ms)])
        for open_time in missing:
            conn.execute(
                "INSERT INTO market_data_gaps(source,venue,time_zone,inst_id,bar,"
                "open_time,close_time,reason,first_observed_at,last_checked_at,"
                "check_count,evidence) VALUES('okx','swap','UTC',?,?,?,?,?,?,?,1,?) "
                "ON CONFLICT(source,venue,inst_id,bar,open_time) DO UPDATE SET "
                "last_checked_at=excluded.last_checked_at,"
                "check_count=market_data_gaps.check_count+1,evidence=excluded.evidence",
                [inst_id, bar, open_time, open_time + BAR_MS[bar],
                 "absent_from_okx_history_after_recheck", checked, checked,
                 "history-candles initial scan plus independent timestamp recheck"])
    return len(missing)


def audit_window(conn: sqlite3.Connection, symbols: Iterable[str],
                 bars: Iterable[str], start_ms: int,
                 end_ms: int) -> dict[str, Any]:
    """Audit continuity and invariants for a fully closed UTC window."""
    series = {}
    missing_total = source_gap_total = unexplained_total = bad_total = 0
    for inst_id in symbols:
        for bar in bars:
            expected = max(0, (int(end_ms) - int(start_ms)) // BAR_MS[bar])
            row = conn.execute(
                "SELECT COUNT(*) n,COUNT(DISTINCT open_time) distinct_n,"
                "COALESCE(SUM(CASE WHEN confirmed<>1 OR source<>'okx' OR "
                "venue<>'swap' OR time_zone<>'UTC' OR "
                "close_time<>open_time+? OR high<low OR "
                "high<MAX(open,close) OR low>MIN(open,close) OR open<=0 OR "
                "close<=0 OR volume<0 OR quote_volume<0 THEN 1 ELSE 0 END),0) bad "
                "FROM klines_v2 WHERE source='okx' AND venue='swap' AND "
                "inst_id=? AND bar=? AND open_time>=? AND open_time<?",
                [BAR_MS[bar], inst_id, bar, int(start_ms), int(end_ms)]).fetchone()
            count = int(row["n"] or 0)
            missing = max(0, int(expected) - count)
            source_gaps = int(conn.execute(
                "SELECT COUNT(*) FROM market_data_gaps g WHERE g.source='okx' "
                "AND g.venue='swap' AND g.time_zone='UTC' AND g.inst_id=? "
                "AND g.bar=? AND g.open_time>=? AND g.open_time<? AND NOT EXISTS "
                "(SELECT 1 FROM klines_v2 k WHERE k.source=g.source AND "
                "k.venue=g.venue AND k.inst_id=g.inst_id AND k.bar=g.bar "
                "AND k.open_time=g.open_time)",
                [inst_id, bar, int(start_ms), int(end_ms)]).fetchone()[0])
            unexplained = max(0, missing - source_gaps)
            bad = int(row["bad"] or 0) + max(0, count - int(row["distinct_n"] or 0))
            key = f"{inst_id}:{bar}"
            series[key] = {"expected": int(expected), "available": count,
                           "missing": missing, "source_gaps": source_gaps,
                           "unexplained_missing": unexplained, "bad": bad,
                           "coverage": count / expected if expected else 1.0}
            missing_total += missing
            source_gap_total += source_gaps
            unexplained_total += unexplained
            bad_total += bad
    return {"start_ms": int(start_ms), "end_ms": int(end_ms),
            "series": series, "missing": missing_total,
            "source_gaps": source_gap_total,
            "unexplained_missing": unexplained_total, "bad": bad_total,
            "complete": unexplained_total == 0 and bad_total == 0}

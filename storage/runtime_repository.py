"""Persistence interface for engine runtime telemetry and failures."""

from __future__ import annotations

import time
from typing import Any, Iterable

from storage import db


def record_engine_error(source: str, message: str, traceback_text: str = "", *,
                        db_path: str | None = None,
                        dedup_seconds: float = 300) -> bool:
    """Persist an engine error once per dedup window."""
    db.init_db(db_path)
    duplicate = db.q1(
        "SELECT id FROM engine_errors WHERE engine=? AND error=? "
        "AND ts>? ORDER BY id DESC LIMIT 1",
        [source, message, time.time() - dedup_seconds], db_path=db_path)
    if duplicate:
        return False
    db.x("INSERT INTO engine_errors (ts,engine,error,traceback) "
         "VALUES (?,?,?,?)",
         [time.time(), source, message, traceback_text], db_path=db_path)
    return True


def record_engine_error_prefix(source: str, message: str,
                               traceback_text: str = "", *,
                               db_path: str | None = None,
                               dedup_seconds: float = 300) -> bool:
    """Persist an error, deduplicating by its stable message prefix."""
    db.init_db(db_path)
    prefix = message[:120]
    duplicate = db.q1(
        "SELECT id FROM engine_errors WHERE engine=? AND error LIKE ? "
        "AND ts>? ORDER BY id DESC LIMIT 1",
        [source, prefix + "%", time.time() - dedup_seconds], db_path=db_path)
    if duplicate:
        return False
    db.x("INSERT INTO engine_errors (ts,engine,error,traceback) "
         "VALUES (?,?,?,?)",
         [time.time(), source, message, traceback_text], db_path=db_path)
    return True


def save_position_snapshot(positions: Iterable[Any], *,
                           db_path: str | None = None,
                           snapshot_ts: float | None = None) -> None:
    """Atomically append one exchange position snapshot."""
    timestamp = time.time() if snapshot_ts is None else float(snapshot_ts)
    rows = list(positions)
    db.init_db(db_path)
    with db.tx(db_path=db_path) as connection:
        if not rows:
            connection.execute(
                "INSERT INTO position_snapshots "
                "(ts,inst_id,side,contracts,base_qty,avg_px) "
                "VALUES (?,?,?,?,?,?)", [timestamp, "-", "-", 0, 0, 0])
        for position in rows:
            connection.execute(
                "INSERT INTO position_snapshots "
                "(ts,inst_id,side,contracts,base_qty,avg_px) "
                "VALUES (?,?,?,?,?,?)",
                [timestamp, position.inst_id, position.side,
                 position.contracts, round(position.base_qty, 8),
                 position.avg_px])

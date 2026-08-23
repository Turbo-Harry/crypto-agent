"""Persistence interface for the execution trade journal."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from storage import db


def initialize(db_path: str | None) -> None:
    db.init_db(db_path)


def load_trades(db_path: str | None) -> list[dict[str, Any]]:
    initialize(db_path)
    return db.q("SELECT * FROM trades ORDER BY entry_time", db_path=db_path)


def load_legacy_lessons(db_path: str | None) -> list[Any]:
    initialize(db_path)
    row = db.q1("SELECT value FROM kv WHERE key='legacy_journal_lessons'",
                db_path=db_path)
    return json.loads(row["value"]) if row else []


def _columns(row: Mapping[str, Any]) -> list[str]:
    return [name for name in row if name in db._TRADE_COLS]


def _value(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, (list, dict)) else value


def insert_trade(row: Mapping[str, Any], db_path: str | None) -> None:
    columns = _columns(row)
    db.x(f"INSERT INTO trades ({','.join(columns)}) "
         f"VALUES ({','.join('?' * len(columns))})",
         [_value(row[name]) for name in columns], db_path=db_path)


def update_trade(trade_id: str, fields: Mapping[str, Any],
                 db_path: str | None) -> None:
    columns = _columns(fields)
    if not columns:
        return
    assignments = ", ".join(f"{name}=?" for name in columns)
    db.x(f"UPDATE trades SET {assignments} WHERE id=?",
         [_value(fields[name]) for name in columns] + [trade_id],
         db_path=db_path)


def upsert_trades(rows: Iterable[Mapping[str, Any]],
                  db_path: str | None) -> None:
    for row in rows:
        columns = _columns(row)
        db.x(f"INSERT OR REPLACE INTO trades ({','.join(columns)}) "
             f"VALUES ({','.join('?' * len(columns))})",
             [_value(row[name]) for name in columns], db_path=db_path)

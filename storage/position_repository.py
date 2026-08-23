"""Persistence interface for the execution position-ownership ledger."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from storage import db


def load_ownership(db_path: str | None) -> dict[str, dict[str, Any]]:
    db.init_db(db_path)
    result = {}
    for row in db.q("SELECT * FROM ownership", db_path=db_path):
        result[row["key"]] = {
            "qty": row["qty"], "notional": row["notional"],
            "strategies": json.loads(row["strategies"] or "{}"),
            "updated_at": row["updated_at"],
        }
    return result


def upsert_ownership(records: Mapping[str, Mapping[str, Any]],
                     db_path: str | None) -> None:
    for key, record in records.items():
        db.x("INSERT OR REPLACE INTO ownership "
             "(key,qty,notional,strategies,updated_at) VALUES (?,?,?,?,?)",
             [key, record.get("qty", 0.0), record.get("notional", 0.0),
              json.dumps(record.get("strategies", {})),
              record.get("updated_at", time.time())], db_path=db_path)


def delete_ownership(key: str, db_path: str | None) -> None:
    db.x("DELETE FROM ownership WHERE key=?", [key], db_path=db_path)

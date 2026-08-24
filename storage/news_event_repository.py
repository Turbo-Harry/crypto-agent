"""Browser-captured OKX page evidence persistence.

This repository has no trading authority. It stores only user-visible page text
from tabs where the extension was explicitly enabled.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import config
from storage import db


def record_browser_page_event(payload: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    db.init_db(db_path)
    captured_ts = float(payload.get("captured_ts") or time.time())
    url = str(payload.get("url") or "")[:config.CHROME_CAPTURE_MAX_URL_CHARS]
    title = str(payload.get("page_title") or "")[:config.CHROME_CAPTURE_MAX_TITLE_CHARS]
    text = str(payload.get("visible_text") or "")[:config.CHROME_CAPTURE_MAX_TEXT_CHARS]
    source = str(payload.get("source") or "okx_chrome")[:64]
    tab_id = payload.get("tab_id")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    digest = hashlib.sha256(
        f"{source}\0{url}\0{title}\0{text}".encode("utf-8", errors="replace")
    ).hexdigest()
    before = db.q1("SELECT id FROM browser_page_events WHERE content_hash=?",
                   [digest], db_path=db_path)
    if before:
        return {"accepted": True, "created": False, "event_id": before["id"],
                "content_hash": digest}
    db.x(
        "INSERT INTO browser_page_events "
        "(content_hash,captured_ts,received_ts,url,page_title,visible_text,source,tab_id,metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [digest, captured_ts, time.time(), url, title, text, source, tab_id,
         json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))],
        db_path=db_path,
    )
    row = db.q1("SELECT id FROM browser_page_events WHERE content_hash=?",
                [digest], db_path=db_path)
    return {"accepted": True, "created": True, "event_id": row["id"],
            "content_hash": digest}


def list_browser_page_events(db_path: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    db.init_db(db_path)
    limit = max(1, min(int(limit), config.CHROME_CAPTURE_LIST_LIMIT))
    rows = db.q(
        "SELECT id,content_hash,captured_ts,received_ts,url,page_title,source,tab_id,metadata "
        "FROM browser_page_events ORDER BY captured_ts DESC LIMIT ?",
        [limit], db_path=db_path)
    for row in rows:
        try:
            row["metadata"] = json.loads(row.get("metadata") or "{}")
        except json.JSONDecodeError:
            row["metadata"] = {}
    return rows

"""OKX Chrome visible-page capture is local, idempotent and research-only."""

import os
import tempfile
import time

os.environ["CRYPTO_AGENT_MODE"] = "paper"

from fastapi.testclient import TestClient

from service.app import create_app
from storage.news_event_repository import list_browser_page_events


class _Runtime:
    def __init__(self, db_path):
        self.db_path = db_path

    def record_browser_page_event(self, payload):
        from storage.news_event_repository import record_browser_page_event
        return record_browser_page_event(dict(payload), db_path=self.db_path)


class _Trader:
    def __init__(self, db_path):
        self.service_api = _Runtime(db_path)


class _Worker:
    def __init__(self, db_path):
        self.trader = _Trader(db_path)

    def start(self):
        return None

    def stop(self):
        return True


def main():
    db_path = os.path.join(tempfile.mkdtemp(prefix="okx_capture_"), "capture.db")
    app = create_app(worker_factory=lambda: _Worker(db_path))
    payload = {
        "captured_ts": time.time(),
        "url": "https://www.okx.com/trade-swap/btc-usdt-swap",
        "page_title": "BTC-USDT-SWAP | OKX",
        "visible_text": "BTC-USDT-SWAP 60000.1 Order Book Trades",
        "source": "okx_chrome",
        "metadata": {"path": "/trade-swap/btc-usdt-swap"},
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post("/browser/okx/events", json=payload)
        second = client.post("/browser/okx/events", json=payload)
        rejected = client.post("/browser/okx/events", json={**payload, "url": "https://example.com"})
        listed = client.get("/browser/okx/events?limit=10")
    rows = list_browser_page_events(db_path, limit=10)
    assert first.status_code == 200 and first.json()["created"] is True
    assert second.status_code == 200 and second.json()["created"] is False
    assert first.json()["event_id"] == second.json()["event_id"]
    assert rejected.status_code == 422
    assert listed.status_code == 200 and len(listed.json()["events"]) == 1
    assert "visible_text" not in listed.json()["events"][0]
    assert len(rows) == 1 and rows[0]["source"] == "okx_chrome"
    print("结果: 7 通过, 0 失败")


if __name__ == "__main__":
    main()

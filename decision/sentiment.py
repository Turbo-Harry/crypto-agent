# -*- coding: utf-8 -*-
"""
消息面情感层（2026-08-23 用户要求"系统也要加上消息面的判断"）——
两个免费数据源合成一个 [-1, +1] 情感分:

  1. 恐惧贪婪指数(alternative.me,免 key,每日)——值 0-100 → (v/50-1)
  2. 新闻标题情感(Coindesk/Cointelegraph RSS)——词典法牛/熊词计数
     → (bull-bear)/(bull+bear)

合成: 0.6×F&G + 0.4×新闻。落 kv(sentiment_latest) + sentiment_snapshots 表。
决策层只读 kv 快照(绝不阻塞网络),无数据时门控自动放行。
词典是粗粒度代理,不宣称精确——诚实标注。
"""
import json
import os
import re
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FNG_URL = "https://api.alternative.me/fng/?limit=1"
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
BULL_WORDS = ("rally", "surge", "soar", "bull", "breakout", "record",
              "gain", "rebound", "adopt", "pump", "high", "rise", "recover")
BEAR_WORDS = ("crash", "plunge", "bear", "liquidat", "fear", "dump",
              "hack", "ban", "lawsuit", "selloff", "fall", "drop", "loss")


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _fng_score():
    try:
        d = json.loads(_get(FNG_URL))
        v = float(d["data"][0]["value"])
        return v / 50.0 - 1.0, v, d["data"][0].get("value_classification")
    except Exception:
        return None, None, None


def _news_score():
    """词典法标题情感。返回 (score, bull, bear, headlines_n)。"""
    texts = []
    for feed in RSS_FEEDS:
        try:
            xml = _get(feed)
            texts.extend(re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                                    xml, flags=re.S)[1:20])
        except Exception:
            continue
    low = " ".join(texts).lower()
    bull = sum(low.count(w) for w in BULL_WORDS)
    bear = sum(low.count(w) for w in BEAR_WORDS)
    if bull + bear == 0:
        return None, 0, 0, len(texts)
    return (bull - bear) / (bull + bear), bull, bear, len(texts)


def fetch_sentiment(db_path=None):
    """抓取并落库最新情感快照。返回 dict(可能部分字段 None)。"""
    fng_s, fng_v, fng_c = _fng_score()
    news_s, bull, bear, n_head = _news_score()
    parts = [x for x in (fng_s, news_s) if x is not None]
    composite = round(sum(parts) / len(parts), 4) if parts else None
    snap = {
        "ts": round(time.time(), 1),
        "composite": composite,
        "fng_value": fng_v,
        "fng_class": fng_c,
        "news_score": round(news_s, 4) if news_s is not None else None,
        "news_bull": bull, "news_bear": bear, "news_headlines": n_head,
    }
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("INSERT OR REPLACE INTO kv (key, value) VALUES ('sentiment_latest', ?)",
              [json.dumps(snap, ensure_ascii=False)], db_path=db_path)
        sdb.x("INSERT INTO sentiment_snapshots (ts, composite, fng_value, "
              "news_score, detail) VALUES (?,?,?,?,?)",
              [snap["ts"], composite, fng_v, snap["news_score"],
               json.dumps(snap, ensure_ascii=False)], db_path=db_path)
    except Exception:
        pass
    return snap


def latest_sentiment(db_path=None):
    """读最近一次快照(决策层用,零网络)。无数据 → None(门控放行)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        row = sdb.q1("SELECT value FROM kv WHERE key='sentiment_latest'",
                     db_path=db_path)
        if not row:
            return None
        snap = json.loads(row["value"])
        # 快照超过 24h 视为过期(消息面会变)
        if time.time() - snap.get("ts", 0) > 86400:
            return None
        return snap
    except Exception:
        return None


if __name__ == "__main__":
    s = fetch_sentiment()
    print(json.dumps(s, ensure_ascii=False, indent=1))

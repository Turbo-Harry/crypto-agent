"""
恐惧贪婪指数（Fear & Greed Index）数据层。
数据源：alternative.me（免费、免 key、每天更新）
API: https://api.alternative.me/fng/?limit=0 拉全量历史（自 2018-02）
字段：value(0-100，越高越贪婪)、value_classification、timestamp

用法：
  from data.fetch_fear_greed import fetch_fng
  fng = fetch_fng()  # 返回 [{date, value, classification}]
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CACHE_DIR, "cache_fng.json")


def fetch_fng(use_cache=True):
    """拉恐惧贪婪指数全量历史，返回升序 [{date, value, classification}]。"""
    if use_cache and os.path.exists(CACHE_FILE) and time.time() - os.path.getmtime(CACHE_FILE) < 3600:
        with open(CACHE_FILE) as f:
            return json.load(f)

    url = "https://api.alternative.me/fng/?limit=0"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())

    data = d.get("data", [])
    result = []
    for item in data:
        ts = int(item["timestamp"])
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        result.append({
            "date": date,
            "value": int(item["value"]),
            "classification": item.get("value_classification", ""),
        })
    result.sort(key=lambda x: x["date"])  # 升序
    with open(CACHE_FILE, "w") as f:
        json.dump(result, f)
    return result


def fng_at_date(fng_list, date_str):
    """取某日期的恐惧贪婪指数值，找不到返回 None。"""
    for item in fng_list:
        if item["date"] >= date_str:
            if item["date"] == date_str:
                return item["value"]
            break
    # 返回最近的历史值（日期可能非交易日）
    prev = None
    for item in fng_list:
        if item["date"] > date_str:
            break
        prev = item["value"]
    return prev


if __name__ == "__main__":
    fng = fetch_fng()
    print(f"恐惧贪婪指数 {len(fng)} 条，范围 {fng[0]['date']} ~ {fng[-1]['date']}")
    print("最近5条:")
    for item in fng[-5:]:
        print(f"  {item['date']}  {item['value']}  {item['classification']}")

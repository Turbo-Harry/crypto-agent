"""
经济日历 — 高影响宏观经济事件过滤。
高影响事件（CPI/FOMC/非农）公布时，加密市场剧烈波动，
资金费率套利应避开这些时点开仓（避免开在插针瞬间）。

数据：内置可配置的事件清单（日期提前公布，需定期更新）。
用法：
  from data.economic_calendar import is_high_impact_now
  in_window, event_name = is_high_impact_now()
"""
import json
import os
from datetime import datetime, timezone, timedelta

EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "economic_events.json")

# 高影响事件清单（示例，日期需根据官方公布更新）
# 格式：{"date": "YYYY-MM-DD", "time_utc": "HH:MM", "event": "事件名"}
DEFAULT_EVENTS = [
    {"date": "2026-08-19", "time_utc": "12:30", "event": "美国 CPI 消费者物价指数"},
    {"date": "2026-09-04", "time_utc": "12:30", "event": "美国非农就业 NFP"},
    {"date": "2026-09-16", "time_utc": "18:00", "event": "美联储 FOMC 利率决议"},
    {"date": "2026-10-13", "time_utc": "12:30", "event": "美国 CPI 消费者物价指数"},
    {"date": "2026-11-06", "time_utc": "13:30", "event": "美国非农就业 NFP"},
    {"date": "2026-12-09", "time_utc": "18:00", "event": "美联储 FOMC 利率决议"},
]

# 事件窗口：前后多少分钟不新开仓
EVENT_WINDOW_MINUTES = 30


def load_events():
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE) as f:
            return json.load(f)
    return DEFAULT_EVENTS


def save_events(events):
    with open(EVENTS_FILE, "w") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def is_high_impact_now(now=None, window_minutes=None):
    """
    判断当前是否在高影响事件窗口内。
    返回 (是否在窗口, 事件名或None)。
    """
    now = now or datetime.now(timezone.utc)
    window = window_minutes or EVENT_WINDOW_MINUTES
    for ev in load_events():
        try:
            dt = datetime.fromisoformat(f"{ev['date']}T{ev['time_utc']}+00:00")
        except Exception:
            continue
        delta = abs((now - dt).total_seconds()) / 60
        if delta <= window:
            return True, ev["event"]
    return False, None


def calendar_expired(now=None):
    """RES-13：事件清单是否已全部过期（日历风控门静默失效检测）。
    返回 (是否过期, 最近一个事件的时间)。空清单也视为过期。"""
    now = now or datetime.now(timezone.utc)
    events = load_events()
    if not events:
        return True, None
    latest = None
    for ev in events:
        try:
            dt = datetime.fromisoformat(f"{ev['date']}T{ev['time_utc']}+00:00")
        except Exception:
            continue
        if latest is None or dt > latest:
            latest = dt
    if latest is None:
        return True, None
    return now > latest + timedelta(minutes=EVENT_WINDOW_MINUTES), latest


def upcoming_events(days=7):
    """返回未来 N 天内的高影响事件。"""
    now = datetime.now(timezone.utc)
    out = []
    for ev in load_events():
        try:
            dt = datetime.fromisoformat(f"{ev['date']}T{ev['time_utc']}+00:00")
        except Exception:
            continue
        if 0 <= (dt - now).total_seconds() <= days * 86400:
            out.append(ev)
    return sorted(out, key=lambda e: (e["date"], e["time_utc"]))


if __name__ == "__main__":
    in_win, name = is_high_impact_now()
    print(f"当前是否在高影响事件窗口: {'是 - ' + name if in_win else '否'}")
    print(f"\n未来 7 天高影响事件:")
    for ev in upcoming_events(7):
        print(f"  {ev['date']} {ev['time_utc']} UTC  {ev['event']}")

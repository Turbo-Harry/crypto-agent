#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
结构化事件日志（2026-08-20 用户要求'框架健全性'缺口之三）——
stdout 文本日志靠 grep 排查(今日 15+ 事故皆如此),补一条 JSONL 审计流:
每行一条机器可读事件 {ts, type, payload},供脚本/看板/AI 直接消费。

原则:
  - 关键交易事件(trades/order_failures/anomalies/risk_events)仍是
    SQLite 表的权威记录;JSONL 是追加式审计流(不重复造表,只镜像关键点)。
  - 失败静默:日志问题绝不影响交易路径。
  - 轮转由 tools/ops_scripts.py rotate 一并处理(>20MB 归档)。

用法:
  from service.events import log_event
  log_event("open", {"symbol": "BTC", "dir": "long", ...})
  log_event("close", {...}) / ("order_fail", {...}) / ("halt", {...})
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_FILE = os.path.join(ROOT, "logs", "events.jsonl")


def log_event(event_type, payload=None):
    """追加一条结构化事件。返回 True/False(不影响调用方)。"""
    try:
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        rec = {"ts": round(time.time(), 3), "type": event_type,
               "payload": payload or {}}
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def tail_events(limit=50, event_type=None):
    """读最近 N 条事件(供 /status 或人工排查)。"""
    out = []
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if event_type and rec.get("type") != event_type:
                    continue
                out.append(rec)
    except Exception:
        return out
    return out[-limit:]

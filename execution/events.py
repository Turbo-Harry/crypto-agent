#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追加式 JSONL 事件审计流。

SQLite 仍是交易/异常/风控的权威事实源；本模块只镜像关键事件，供脚本、
看板和 AI 快速消费。事件属于执行层，不应由 engines 反向依赖 service。

隔离规则：
  1. CRYPTO_AGENT_EVENTS_FILE 显式指定时优先使用（CI/运维可控）；
  2. 传入 db_path 时写到 ``<db_path>.events.jsonl``（测试天然隔离）；
  3. 生产默认沿用 ``logs/events.jsonl``，保持既有运行语义。
"""
import json
import os
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_FILE = os.path.join(ROOT, "logs", "events.jsonl")


def event_path(db_path=None):
    """解析本次事件流路径；测试 db 与事件文件保持同生命周期。"""
    explicit = os.environ.get("CRYPTO_AGENT_EVENTS_FILE")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if db_path:
        return os.path.abspath(os.path.expanduser(str(db_path))) + ".events.jsonl"
    return EVENTS_FILE


def log_event(event_type, payload=None, db_path=None):
    """追加一条结构化事件。失败返回 False，绝不影响交易路径。"""
    try:
        path = event_path(db_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"ts": round(time.time(), 3), "type": event_type,
               "payload": payload or {}}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def tail_events(limit=50, event_type=None, db_path=None):
    """读取最近 N 条事件；坏行跳过，缺文件返回空列表。"""
    out = []
    try:
        with open(event_path(db_path), encoding="utf-8") as f:
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

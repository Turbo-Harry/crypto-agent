#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一异常中心 —— 所有异常的唯一入口（2026-08-17 用户要求:不要分散）。

规则:
  - 任何异常生产者（体检/下单失败/引擎异常/风控/其他）必须经 register() 落
    anomalies 表,禁止另立门户;
  - 30 分钟窗口内同 (source, title) 去重(防轰炸);
  - 严重级别: critical(风控熔断/服务崩溃) / error(下单失败/引擎异常/体检失败) /
    warning(数据质量等);
  - 消费端(飞书告警/session 注入/看板)只读本表,不接触各业务表。
"""
import time


def register(source, title, detail="", severity="error", db_path=None,
             dedup_window=1800):
    """登记一条异常。返回 (True=新登记, False=窗口内去重)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        dup = sdb.q1("SELECT id FROM anomalies WHERE source=? AND title=? "
                     "AND ts > ? ORDER BY id DESC LIMIT 1",
                     [source, title, time.time() - dedup_window],
                     db_path=db_path)
        if dup:
            return False
        sdb.x("INSERT INTO anomalies (ts, source, severity, title, detail, "
              "status) VALUES (?,?,?,?,?,?)",
              [time.time(), source, severity, title, detail[:500], "new"],
              db_path=db_path)
        return True
    except Exception:
        return False


def resolve(source, title, db_path=None):
    """标记已处理(值守流程修复后调用)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("UPDATE anomalies SET status='resolved' WHERE source=? "
              "AND title=? AND status='new'", [source, title], db_path=db_path)
    except Exception:
        pass


def list_new(db_path=None, limit=50):
    """未处理异常列表(消费端统一入口)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        return sdb.q("SELECT id, ts, source, severity, title, detail FROM "
                     "anomalies WHERE status='new' ORDER BY ts DESC LIMIT ?",
                     [limit], db_path=db_path)
    except Exception:
        return []

# -*- coding: utf-8 -*-
"""
连亏冷却（2026-08-23 用户指示"连亏 6 笔后应主动冷却，不硬接信号"）——
连续净亏 N 笔 → 主动冷却 LOSS_STREAK_COOL_HOURS 小时,期间扫描只记决策
不接信号;到期自动解除,也可手动 /cool/release。单笔盈利即重置连亏计数。
纯 kv 持久化(重启不丢),两实例各自统计各自的连亏(不跨库)。
"""
import time

import config


def _enabled():
    """模式门控(2026-08-23 用户指示"模拟盘去掉保持锁定"):
    模拟盘不冷却(激进采集),实盘保持锁定;总开关优先。"""
    if not getattr(config, "LOSS_STREAK_COOL_ENABLED", True):
        return False
    if getattr(config, "CRYPTO_MODE", "live") == "paper":
        return bool(getattr(config, "LOSS_STREAK_COOL_PAPER_ENABLED", False))
    return True


def streak(db_path=None):
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        row = sdb.q1("SELECT value FROM kv WHERE key='loss_streak'",
                     db_path=db_path)
        return int(float(row["value"])) if row else 0
    except Exception:
        return 0


def is_cooling(db_path=None):
    """冷却中? 到期自动解除(顺带清 kv)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        row = sdb.q1("SELECT value FROM kv WHERE key='cooling_until'",
                     db_path=db_path)
        if not row:
            return False
        until = float(row["value"])
        if time.time() < until:
            return True
        sdb.x("DELETE FROM kv WHERE key='cooling_until'", db_path=db_path)
    except Exception:
        pass
    return False


def cooling_remaining_hours(db_path=None):
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        row = sdb.q1("SELECT value FROM kv WHERE key='cooling_until'",
                     db_path=db_path)
        if row:
            rem = float(row["value"]) - time.time()
            return round(max(0.0, rem / 3600.0), 2)
    except Exception:
        pass
    return 0.0


def release(db_path=None):
    """手动解除冷却(用户/运维)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("DELETE FROM kv WHERE key='cooling_until'", db_path=db_path)
        sdb.x("INSERT OR REPLACE INTO kv (key, value) VALUES ('loss_streak', '0')",
              db_path=db_path)
        return True
    except Exception:
        return False


def on_close(db_path, net_pnl, notify=None):
    """平仓后步进: 净亏 streak+1,盈利归零;达阈值 → 启动冷却。
    返回 action: 'cooling_started' / 'none'。notify 为可选的告警回调。"""
    if not _enabled():
        return "none"
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        s = streak(db_path)
        if net_pnl < 0:
            s += 1
        else:
            s = 0
        sdb.x("INSERT OR REPLACE INTO kv (key, value) VALUES ('loss_streak', ?)",
              [str(s)], db_path=db_path)
        if s >= config.LOSS_STREAK_COOL_THRESHOLD:
            until = time.time() + config.LOSS_STREAK_COOL_HOURS * 3600
            sdb.x("INSERT OR REPLACE INTO kv (key, value) VALUES "
                  "('cooling_until', ?)", [f"{until:.1f}"], db_path=db_path)
            sdb.x("INSERT OR REPLACE INTO kv (key, value) VALUES "
                  "('loss_streak', '0')", db_path=db_path)
            msg = (f"🔥 连亏 {s} 笔,主动冷却 {config.LOSS_STREAK_COOL_HOURS} "
                   f"小时——不再硬接信号,到期自动恢复")
            if notify:
                try:
                    notify(msg)
                except Exception:
                    pass
            return "cooling_started"
    except Exception:
        pass
    return "none"

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运维脚本（2026-08-19 用户要求'提高健壮性'）:
  backup: 数据库日备份(保留 7 天)——交易样本是系统最宝贵资产,当前零备份
  rotate: 日志轮转(/tmp 引擎日志 >20MB 归档+截断,防无限增长)

用法:
  python3 tools/ops_scripts.py backup
  python3 tools/ops_scripts.py rotate
launchd 调度: com.crypto.backup(每日) / com.crypto.logrotate(每日)
"""
import gzip
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(ROOT, "backups")
ARCHIVE_DIR = os.path.join(ROOT, "logs")
KEEP_DAYS = 7
LOG_MAX_MB = 20
LOGS = ("/tmp/crypto-agent.out.log", "/tmp/crypto-agent.err.log",
        "/tmp/crypto-watchdog.out.log", "/tmp/crypto-healthcheck.out.log")


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    db = os.path.join(ROOT, "crypto_agent.db")
    if not os.path.exists(db):
        print("❌ 无数据库文件")
        return 1
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"crypto_agent_{stamp}.db.gz")
    with open(db, "rb") as src, gzip.open(dst, "wb") as out:
        shutil.copyfileobj(src, out)
    # 过期清理(保留 KEEP_DAYS 天)
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for fn in os.listdir(BACKUP_DIR):
        p = os.path.join(BACKUP_DIR, fn)
        if os.path.getmtime(p) < cutoff:
            os.remove(p)
            removed += 1
    size = os.path.getsize(dst)
    print(f"✅ 备份完成: {dst} ({size/1024:.0f} KB), 清理过期 {removed} 份")
    return 0


def rotate():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    n = 0
    for log in LOGS:
        if not os.path.exists(log):
            continue
        if os.path.getsize(log) < LOG_MAX_MB * 1024 * 1024:
            continue
        dst = os.path.join(ARCHIVE_DIR, f"{os.path.basename(log)}.{stamp}.gz")
        with open(log, "rb") as src, gzip.open(dst, "wb") as out:
            shutil.copyfileobj(src, out)
        # 截断原文件(运行中进程持有 fd,同 inode 截断安全)
        open(log, "w").close()
        n += 1
    # 归档保留 14 天
    cutoff = time.time() - 14 * 86400
    for fn in os.listdir(ARCHIVE_DIR):
        p = os.path.join(ARCHIVE_DIR, fn)
        if os.path.getmtime(p) < cutoff:
            os.remove(p)
    print(f"✅ 轮转 {n} 个日志(归档保留 14 天)")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backup"
    sys.exit({"backup": backup, "rotate": rotate}.get(mode, backup)())

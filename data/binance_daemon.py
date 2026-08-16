"""
币安源常驻采集进程 — 多周期调度，采集加密币+美股代币。
用于攒日内短线所需的分钟级/小时级历史数据。

调度：
  1m  每 60 秒
  15m 每 15 分钟
  1h  每 1 小时
  4h  每 4 小时
  1d  每 24 小时（采集后上传 COS）

用法：
  python3 data/binance_daemon.py
"""
import sys
import os
import time
import argparse
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COLLECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_binance.py")

SCHEDULE = {
    "1m": 60,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def run_collect(bar):
    try:
        r = subprocess.run([sys.executable, COLLECT, "--bar", bar, "--all"],
                           capture_output=True, text=True, timeout=600)
        return r.returncode == 0
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="各周期跑一次就退出")
    args = parser.parse_args()

    print("币安常驻采集进程启动:")
    for bar, sec in SCHEDULE.items():
        print(f"  {bar:<4} 每 {sec} 秒")
    print("标的: 加密观察池 + 美股代币, 存储 data/market.db")
    print("Ctrl+C 停止\n")

    last_run = {bar: 0 for bar in SCHEDULE}
    while True:
        now = time.time()
        for bar, interval in SCHEDULE.items():
            if now - last_run[bar] >= interval:
                ok = run_collect(bar)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] {bar} 采集 {'成功' if ok else '失败'}")
                last_run[bar] = now
        if args.once:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()

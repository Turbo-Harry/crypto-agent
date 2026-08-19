"""
常驻采集进程 — 多周期自动调度，采集全部标的（加密 + 美股）到 SQLite。

调度周期：
  1m  每 60 秒
  15m 每 15 分钟
  1H  每 1 小时
  4H  每 4 小时
  1D  每 24 小时（采集后自动上传 COS）

用法：
  python3 data/collect_daemon.py              # 全部周期 + 全部标的
  python3 data/collect_daemon.py --once       # 每个周期跑一次就退出
"""
import sys
import os
import time
import argparse
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COLLECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect.py")
UPLOAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload.py")

# 各周期的采集间隔（秒）
SCHEDULE = {
    "1m": 60,
    "15m": 900,
    "1H": 3600,
    "4H": 14400,
    "1D": 86400,
}


def run_collect(bar, top=None):
    """采集某周期的全部标的。返回是否成功。"""
    try:
        cmd = [sys.executable, COLLECT, "--bar", bar, "--all"]
        if top:
            cmd += ["--top", str(top)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return r.returncode == 0
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="每个周期跑一次就退出")
    parser.add_argument("--top", type=int, default=None,
                        help="加密币只采前 N 个（None=全部观察池）")
    args = parser.parse_args()

    print("常驻采集进程启动（多周期）:")
    for bar, sec in SCHEDULE.items():
        print(f"  {bar:<4} 每 {sec} 秒")
    print(f"标的: 加密观察池 + 美股代币，存储 data/market.db")
    print("Ctrl+C 停止\n")

    last_run = {bar: 0 for bar in SCHEDULE}
    last_upload_day = ""

    while True:
        # 2026-08-16 根因修复：循环体整体兜底——此前任何未捕获异常都会
        # 让常驻采集进程永久退出（两次整夜停更事故）。现在失败只丢一轮、不丢进程；
        # 配合 launchd KeepAlive（com.okx.collect）双保险。
        try:
            now = time.time()
            # 按调度采集各周期
            for bar, interval in SCHEDULE.items():
                if now - last_run[bar] >= interval:
                    ok = run_collect(bar, args.top)
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"[{ts}] {bar} 采集 {'成功' if ok else '失败'}",
                          flush=True)
                    last_run[bar] = now
                    # 日线采集后上传 COS
                    if bar == "1D" and ok:
                        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        if day != last_upload_day:
                            try:
                                subprocess.run([sys.executable, UPLOAD],
                                               timeout=180)
                                print(f"  数据已上传 COS ({day})", flush=True)
                                last_upload_day = day
                            except Exception:
                                pass
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"循环异常(已兜底,继续运行): {e}", flush=True)

        if args.once:
            break
        time.sleep(30)  # 每30秒检查一次调度


if __name__ == "__main__":
    main()

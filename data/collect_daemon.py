"""
常驻采集进程 — 多周期采集 confirmed SWAP K 线 + 每日终值对账。

调度周期：
  1m  每 60 秒
  15m 每 15 分钟
  1H  每 1 小时
  4H  每 4 小时
  1D  每 24 小时
  昨日 每个 UTC 日首次运行 history-candles 完整对账（成功后上传 COS）

用法：
  python3 data/collect_daemon.py              # 全部周期 + 全部标的
  python3 data/collect_daemon.py --once       # 每个周期跑一次就退出
"""
import sys
import os
import time
import argparse
import subprocess
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COLLECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect.py")
UPLOAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload.py")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")

# 各周期的采集间隔（秒）
SCHEDULE = {
    "1m": 60,
    "15m": 900,
    "1H": 3600,
    "4H": 14400,
    "1D": 86400,
}


def _run(cmd, timeout):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
        summary = (result.stdout or result.stderr or "").strip().splitlines()
        return result.returncode == 0, (summary[-1] if summary else "无输出")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run_collect(bar, top=None):
    cmd = [sys.executable, COLLECT, "--bar", bar, "--all", "--db", DB_PATH]
    if top:
        cmd += ["--top", str(top)]
    return _run(cmd, timeout=600)


def run_reconcile(date_text, top=None):
    cmd = [sys.executable, COLLECT, "--bars", ",".join(SCHEDULE), "--all",
           "--db", DB_PATH, "--reconcile-date", date_text]
    if top:
        cmd += ["--top", str(top)]
    return _run(cmd, timeout=1800)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="每个周期跑一次就退出")
    parser.add_argument("--top", type=int, default=None,
                        help="加密币只采前 N 个（None=全部观察池）")
    args = parser.parse_args()

    print("常驻采集进程启动（多周期）:")
    for bar, sec in SCHEDULE.items():
        print(f"  {bar:<4} 每 {sec} 秒")
    print("标的: OKX USDT SWAP 流动性池 + 美股合约，存储 klines_v2")
    print("Ctrl+C 停止\n")

    last_run = {bar: 0 for bar in SCHEDULE}
    last_reconcile_day = ""
    last_reconcile_attempt = 0.0

    while True:
        # 2026-08-16 根因修复：循环体整体兜底——此前任何未捕获异常都会
        # 让常驻采集进程永久退出（两次整夜停更事故）。现在失败只丢一轮、不丢进程；
        # 配合 launchd KeepAlive（com.okx.collect）双保险。
        try:
            now = time.time()
            # 按调度采集各周期
            for bar, interval in SCHEDULE.items():
                if now - last_run[bar] >= interval:
                    ok, detail = run_collect(bar, args.top)
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"[{ts}] {bar} 采集 {'成功' if ok else '失败'}",
                          flush=True)
                    print(f"  {detail}", flush=True)
                    last_run[bar] = now
            utc_now = datetime.now(timezone.utc)
            utc_day = utc_now.strftime("%Y-%m-%d")
            # 成功前不记完成；失败每 15 分钟重试。对账是最终值质量门，不能
            # 像旧逻辑一样只凭子进程退出就宣称成功。
            if (utc_day != last_reconcile_day and
                    now - last_reconcile_attempt >= 900):
                last_reconcile_attempt = now
                target = (utc_now.date() - timedelta(days=1)).isoformat()
                ok, detail = run_reconcile(target, args.top)
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                      f"昨日 {target} 对账 {'成功' if ok else '失败'}",
                      flush=True)
                print(f"  {detail}", flush=True)
                if ok:
                    last_reconcile_day = utc_day
                    upload_ok, upload_detail = _run(
                        [sys.executable, UPLOAD], timeout=180)
                    print(f"  COS 上传 {'成功' if upload_ok else '失败'}: "
                          f"{upload_detail}", flush=True)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"循环异常(已兜底,继续运行): {e}", flush=True)

        if args.once:
            break
        time.sleep(30)  # 每30秒检查一次调度


if __name__ == "__main__":
    main()

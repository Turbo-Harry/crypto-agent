"""
数据上传脚本 — 把采集的数据按日期分目录上传到腾讯云 COS 备份。
每天一个独立快照，可回溯、不覆盖历史。

用法：
  python3 data/upload.py                 # 上传当日快照
  python3 data/upload.py --full          # 同时打包上传历史缓存
  python3 data/upload.py --date 2026-08-15   # 指定日期（回溯补传）

上传路径结构：
  crypto-data/2026-08-16/market.db         # 每日采集快照
  crypto-data/history/cache_okx.tar.gz     # 6年历史日线缓存（一次性）
"""
import sys
import os
import subprocess
import argparse
import tarfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TCCLI = os.path.expanduser("~/.local/bin/tccli")
HOME_REDIRECT = "/Users/wuhai/Desktop/untitled folder/.tccli-home"
BUCKET = "clawdbot-1300609114"
REGION = "ap-beijing"
COS_PREFIX = "crypto-data"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_tccli(args, timeout=120):
    env = os.environ.copy()
    env["HOME"] = HOME_REDIRECT  # 重定向 HOME，解决 tccli 日志写权限
    cmd = [TCCLI] + args + ["--region", REGION]  # 显式指定 region，避免 NoSuchBucket
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0 and "Error" not in r.stderr
        if not ok:
            print(f"  [错误] {r.stderr.strip()[:200]}")
        return ok
    except Exception as e:
        print(f"  [异常] {e}")
        return False


def upload_file(local_path, cos_key):
    ok = run_tccli(["cos", "upload", "--bucket", BUCKET,
                    "--local_path", local_path, "--cos_key", cos_key])
    print(f"  上传 {os.path.basename(local_path)} → {cos_key}: "
          f"{'成功' if ok else '失败'}")
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="同时打包上传历史缓存")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认今天 UTC）")
    args = parser.parse_args(argv)
    results = []

    # 日期目录：默认今天 UTC，可指定
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"上传数据到 COS: {BUCKET}/{COS_PREFIX}/{date_str}/")

    # 1. 上传当日采集快照 → crypto-data/{date}/market.db
    db_path = os.path.join(ROOT, "data", "market.db")
    if os.path.exists(db_path):
        results.append(upload_file(
            db_path, f"{COS_PREFIX}/{date_str}/market.db"))
    else:
        print(f"  [错误] 数据库不存在: {db_path}")
        results.append(False)

    # 2. （可选）打包上传历史缓存 → crypto-data/history/cache_okx.tar.gz
    if args.full:
        cache_dir = os.path.join(ROOT, "data", "cache_okx")
        tar_path = os.path.join(ROOT, "crypto-data-okx-klines.tar.gz")
        if os.path.exists(cache_dir):
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(cache_dir, arcname="cache_okx")
            results.append(upload_file(
                tar_path, f"{COS_PREFIX}/history/cache_okx.tar.gz"))
            os.remove(tar_path)
        else:
            print(f"  [错误] 历史缓存不存在: {cache_dir}")
            results.append(False)
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

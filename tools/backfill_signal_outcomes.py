#!/usr/bin/env python3
"""模拟盘候选结果回填；默认 dry-run，--apply 才联网并写库。"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))


def pending_count(db_path):
    import storage.db as sdb
    sdb.init_db(db_path)
    row = sdb.q1(
        "SELECT COUNT(*) n FROM signal_samples s LEFT JOIN signal_outcomes o "
        "ON o.signal_id=s.signal_id WHERE o.signal_id IS NULL "
        "AND s.event_ts+s.horizon_hours*3600<=?", [time.time()], db_path=db_path)
    return int(row["n"] if row else 0)


def main():
    parser = argparse.ArgumentParser(description="回填到期候选的 24H 1m 路径标签")
    parser.add_argument("--db", default=os.environ.get("CRYPTO_AGENT_DB") or
                        "crypto_agent.db")
    parser.add_argument("--apply", action="store_true",
                        help="实际调用模拟盘行情并写 signal_outcomes")
    args = parser.parse_args()
    count = pending_count(args.db)
    if not args.apply:
        print(f"dry-run: {count} 个到期候选待结算；加 --apply 才执行")
        return 0
    if os.environ.get("CRYPTO_AGENT_MODE") != "paper":
        print("拒绝执行：--apply 必须显式 CRYPTO_AGENT_MODE=paper")
        return 2
    from engines.directional_trader import connect
    from decision.signal_outcomes import settle_pending
    result = settle_pending(connect(), db_path=args.db)
    print("结算结果: " + " ".join(f"{k}={v}" for k, v in result.items()))
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
进程入口 — 完整交易系统服务端（FastAPI + uvicorn）。

一个进程托管全部功能：
  - 方向性引擎（2s 止损监控 + 15min 信号扫描 + 每日候选刷新）
  - WebSocket 实时行情
  - 心跳文件沿用 watchdog 命名（heartbeat_directional）
（2026-08-16 用户决定：套利引擎移除并归档 legacy/，不再托管。）

HTTP（只绑本机 127.0.0.1）：
  GET  /docs               Swagger UI（AI 可读 API 文档）
  GET  /health /status /watchlist /journal /signals/{base} /realtime/{base}
  POST /pause /resume     暂停/恢复方向性开仓
  POST /scan/daily        手动触发全市场候选扫描

用法：
  python3 -m service.main                 # 前台
  python3 -m service.main --port 8090     # 指定端口
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from fastapi import FastAPI

from service.app import create_app, ensure_single_worker

DEFAULT_HOST = "127.0.0.1"   # 只绑本机 —— 交易控制面绝不暴露公网
DEFAULT_PORT = 8090


def build_service() -> FastAPI:
    """组装 FastAPI 应用；交易 worker 由 ASGI lifespan 管理。"""
    return create_app()


def main():
    parser = argparse.ArgumentParser(description="Crypto Agent 完整交易服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, default=1,
                        help="必须为 1：交易引擎不允许多 worker")
    args = parser.parse_args()

    # 审计 B-H1:控制面无鉴权,禁止绑定非回环地址(除非显式环境变量放行)
    if args.host not in ("127.0.0.1", "localhost") and \
            os.environ.get("CRYPTO_AGENT_ALLOW_REMOTE") != "1":
        print(f"❌ 拒绝绑定 {args.host}: 控制面无鉴权不暴露公网;"
              f"如确需,设 CRYPTO_AGENT_ALLOW_REMOTE=1 后重试")
        sys.exit(1)

    if args.workers != 1:
        print(f"❌ 拒绝启动: --workers={args.workers};交易引擎只允许单 worker")
        sys.exit(1)
    try:
        ensure_single_worker()
    except RuntimeError as exc:
        print(f"❌ {exc}")
        sys.exit(1)

    fastapi_app = build_service()
    print(f"🚀 完整交易服务启动: http://{args.host}:{args.port}  (API 文档 /docs)")
    uvicorn.run(fastapi_app, host=args.host, port=args.port,
                log_level="warning", workers=1)


if __name__ == "__main__":
    main()

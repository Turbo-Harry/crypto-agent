"""
PID 文件统一写入器 —— 消除"跨层共享状态文件被多层写入"(code_graph 既有告警)。

此前 engines/directional_trader.py 的 run() 与 service/worker.py 各自直接写
<name>.pid / 心跳文件,被 code_graph 判为跨层多层写入。本模块成为唯一写入点,
两层均调用 write_pid/remove_pid(文件名字面量只存在于此)。
"""
import os
import time

NAMES = ("directional",)          # 套利引擎已移除(2026-08-16)
PID_SUFFIX = ".pid"
HEARTBEAT_PREFIX = "heartbeat_"


def write_pid(name):
    """写 <name>.pid(进程守护 watchdog 用)。"""
    with open(f"{name}{PID_SUFFIX}", "w") as f:
        f.write(str(os.getpid()))


def write_heartbeat(name):
    """写 heartbeat_<name>.txt(时间戳)。"""
    with open(f"{HEARTBEAT_PREFIX}{name}.txt", "w") as f:
        f.write(str(time.time()))

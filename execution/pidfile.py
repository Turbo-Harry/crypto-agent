"""
PID 文件统一写入器 —— 消除"跨层共享状态文件被多层写入"(code_graph 既有告警)。

此前 engines/directional_trader.py 的 run() 与 service/worker.py 各自直接写
<name>.pid / 心跳文件,被 code_graph 判为跨层多层写入。本模块成为唯一写入点,
两层均调用 write_pid/remove_pid(文件名字面量只存在于此)。

2026-08-23 双实例: 实盘/模拟盘并行跑。调用方仍传角色名 "directional",
本模块按 CRYPTO_AGENT_MODE=paper 环境变量映射到 "paper",
两实例的心跳/PID/tick 文件互不覆盖(watchdog 按实例名监控)。
"""
import os
import time

NAMES = ("directional",)          # 套利引擎已移除(2026-08-16)
PID_SUFFIX = ".pid"
HEARTBEAT_PREFIX = "heartbeat_"
TICK_PREFIX = "tick_"


def _resolve(name):
    if name == "directional" and os.environ.get("CRYPTO_AGENT_MODE") == "paper":
        return "paper"
    return name


def runtime_path(filename):
    """测试/CI 可把全部 PID/心跳/tick 定向到独立目录；生产默认仍在 cwd。"""
    root = os.environ.get("CRYPTO_AGENT_RUNTIME_DIR")
    if not root:
        return filename
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, filename)


def write_pid(name):
    """写 <name>.pid(进程守护 watchdog 用)。"""
    with open(runtime_path(f"{_resolve(name)}{PID_SUFFIX}"), "w") as f:
        f.write(str(os.getpid()))


def write_heartbeat(name):
    """写 heartbeat_<name>.txt(时间戳)。"""
    with open(runtime_path(f"{HEARTBEAT_PREFIX}{_resolve(name)}.txt"), "w") as f:
        f.write(str(time.time()))


def write_tick(name):
    """写 tick_<name>.txt(主循环每拍完成时间戳)。

    2026-08-17 事故: 心跳线程与主循环解耦后,主循环被网络黑洞阻塞 51 分钟
    (20 币扫描 × 30s 超时)而心跳照常 → watchdog 全程失明,止损监控失明。
    tick 时间戳 = 主循环真实进度,watchdog 用它与进程年龄配合判真卡死。"""
    with open(runtime_path(f"{TICK_PREFIX}{_resolve(name)}.txt"), "w") as f:
        f.write(str(time.time()))

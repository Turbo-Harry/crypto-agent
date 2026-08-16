"""
进程看门狗（R2-4）——心跳超时识别僵尸进程并精确 kill，配合 launchd KeepAlive 自动重启。

设计（C 终审版）：
  1. 每个交易进程启动时写 <name>.pid；主循环每 tick 写 heartbeat_<name>.txt
  2. 本脚本由 launchd 每 60s 触发一次：
     - 心跳文件 stale（有文件但过期）→ 视为真卡死 → 立即按 PID 精确 kill（不 pkill -f）
     - 心跳文件【缺失】→ 先告警并计数，连续 MISSING_TOLERANCE 次才 kill（防磁盘满无限重启循环）
     - 无 PID 文件 → 未启动，不动作
  3. 交易进程的 launchd plist 用 KeepAlive=true（退出/被杀自动拉起）

用法（launchd）：
  watchdog 自身:  StartInterval=60
  交易进程:        KeepAlive=true
具体 plist 模板见 docs/ops/watchdog_launchd.md。
"""
import json
import os
import sys
import time

HEARTBEATS = {
    "directional": {"timeout": 30, "proc": "directional_trader.py"},
    "arb": {"timeout": 300, "proc": "trading_main.py"},
}
MISSING_TOLERANCE = 3          # 心跳文件连续缺失 N 次才 kill（去抖）
STATE_FILE = "watchdog_state.json"


def notify(msg):
    """飞书告警（复用 lark CLI；失败静默）。"""
    try:
        import subprocess
        subprocess.run(["/Users/wuhai/Desktop/untitled folder/lark", "im",
                        "+messages-send", "--as", "bot",
                        "--user-id", "ou_3c597d18937078f2587b56adb8b960d2",
                        "--text", msg], capture_output=True, timeout=20)
    except Exception:
        pass


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _read_pid(name):
    try:
        with open(f"{name}.pid") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def check():
    state = _load_state()
    now = time.time()
    for name, cfg in HEARTBEATS.items():
        pid = _read_pid(name)
        if not pid or not _pid_alive(pid):
            state.pop(name, None)     # 进程不在 → 清计数，不动作
            continue
        try:
            ts = float(open(f"heartbeat_{name}.txt").read().strip())
            state.pop(name, None)     # 心跳恢复 → 清零缺失计数
            stale = (now - ts) > cfg["timeout"]
        except Exception:
            stale = False
            state[name] = state.get(name, 0) + 1   # 缺失计数
        missing = state.get(name, 0)
        if stale or missing >= MISSING_TOLERANCE:
            notify(f"⚠️ {cfg['proc']} 心跳异常（stale={stale}, 缺失{missing}次），"
                   f"按 PID={pid} kill，launchd 将自动重启")
            try:
                os.kill(pid, 9)                     # 精确 kill（不 pkill -f）
            except Exception as e:
                print(f"kill 失败 {name} pid={pid}: {e}")
            state.pop(name, None)
    _save_state(state)


if __name__ == "__main__":
    if "--test" in sys.argv:
        # 自测模式：不触网
        print("watchdog 模块就绪（实际触发由 launchd StartInterval=60 调度）")
        sys.exit(0)
    check()

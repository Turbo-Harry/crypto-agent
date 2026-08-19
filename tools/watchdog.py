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
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.notify import notify

HEARTBEATS = {
    # 2026-08-16 采集加速后首轮扫描 18 币需数分钟（心跳随 tick 阻塞停更），
    # 30s 超时会误杀正在工作的引擎 → 放宽到 120s（配合 scan 内每币心跳刷新）。
    "directional": {"timeout": 120, "proc": "directional_trader.py"},
    # （2026-08-16 用户决定：套利引擎移除，"arb" 项已随 trading_main.py 归档删除）
}
MISSING_TOLERANCE = 3          # 心跳文件连续缺失 N 次才 kill（去抖）
# 2026-08-17: tick 进度判真卡死——心跳线程与主循环解耦后,主循环阻塞时心跳照常,
# watchdog 失明(51 分钟盲窗事故)。tick_<name>.txt 由主循环每拍完成时写入:
# tick 超时 + 进程年龄超宽限(启动扫描期豁免) → 判真卡死 kill。
TICK_TIMEOUT = 300             # tick 进度 5 分钟不动 = 主循环卡死
STARTUP_GRACE = 900            # 进程启动 15 分钟内不判 tick(首轮扫描需数分钟)
STATE_FILE = "watchdog_state.json"


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


def _pid_matches(name, pid):
    """审计 C-H2:PID 身份校验,防 PID 复用误杀无辜进程。
    服务模式命令行是 service/main.py;standalone 是 *_trader.py/trading_main.py。"""
    try:
        import subprocess
        out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    if not out.strip():
        return False
    return (HEARTBEATS[name]["proc"] in out) or ("service/main.py" in out)


def _pid_elapsed(pid):
    """进程年龄(秒)——tick 判死需豁免启动扫描期。"""
    try:
        import subprocess
        out = subprocess.run(["ps", "-p", str(pid), "-o", "etimes="],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.strip())
    except Exception:
        return 0


def _kill(name, pid, reason):
    notify(f"⚠️ {reason}，按 PID={pid} kill，launchd 将自动重启")
    try:
        os.kill(pid, signal.SIGTERM)        # 先优雅退出
        for _ in range(8):                  # 最多等 8s
            time.sleep(1)
            if not _pid_alive(pid):
                break
        if _pid_alive(pid):
            os.kill(pid, 9)                 # 超时仍存活才 SIGKILL
    except Exception as e:
        print(f"kill 失败 {name} pid={pid}: {e}")


def _storm_ok(name, state):
    """重启风暴防护(2026-08-19 用户要求健壮性): 15 分钟内 kill ≥3 次 →
    环境性故障(网络黑洞等)重启也修不好,停止自动 kill,告警人工介入。
    风暴窗口滚动后自动恢复。"""
    kills = state.setdefault("kills", [])
    now = time.time()
    kills = [t for t in kills if now - t < 900]
    state["kills"] = kills
    if len(kills) >= 3:
        notify(f"⛔ {name} 15 分钟内已被 kill {len(kills)} 次——疑似环境性故障,"
               f"watchdog 停止自动重启,请人工排查后清 watchdog_state.json 恢复")
        return False
    return True


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
        # 2026-08-17: tick 进度判真卡死（心跳解耦后的盲区补丁）。
        # tick 文件缺失=启动中(宽限期内豁免);tick 超时+进程够老 → 真卡死。
        tick_stale = False
        try:
            tts = float(open(f"tick_{name}.txt").read().strip())
            tick_stale = (now - tts) > TICK_TIMEOUT
        except Exception:
            tick_stale = _pid_elapsed(pid) > STARTUP_GRACE
        if tick_stale and _pid_elapsed(pid) > STARTUP_GRACE:
            if not _pid_matches(name, pid):
                notify(f"⚠️ {cfg['proc']} tick 进度卡死，但 PID={pid} 进程身份不匹配"
                       f"（防误杀），跳过 kill，请人工检查")
                state.pop(name, None)
                continue
            if not _storm_ok(name, state):
                continue
            _kill(name, pid, f"{cfg['proc']} 主循环 tick 卡死超过 {TICK_TIMEOUT}s"
                             f"（心跳正常，tick 进度停滞）")
            state.setdefault("kills", []).append(time.time())
            state.pop(name, None)
            continue
        if stale or missing >= MISSING_TOLERANCE:
            if not _pid_matches(name, pid):
                notify(f"⚠️ {cfg['proc']} 心跳异常，但 PID={pid} 进程身份不匹配"
                       f"（防误杀），跳过 kill，请人工检查")
                state.pop(name, None)
                continue
            if not _storm_ok(name, state):
                continue
            _kill(name, pid, f"{cfg['proc']} 心跳异常（stale={stale}, 缺失{missing}次）")
            state.setdefault("kills", []).append(time.time())
            state.pop(name, None)
    _save_state(state)


if __name__ == "__main__":
    if "--test" in sys.argv:
        # 自测模式：不触网
        print("watchdog 模块就绪（实际触发由 launchd StartInterval=60 调度）")
        sys.exit(0)
    check()

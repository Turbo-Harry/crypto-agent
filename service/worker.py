"""
后台交易线程 — 完整交易系统的引擎托管层。

托管常驻引擎（一个交易所适配器 + 一条 WebSocket 行情连接）：
  1. 方向性引擎 DirectionalTrader —— 2s 止损监控 + 15min 回踩信号扫描
     （tick() 由 run() 抽取，服务端复用同一逻辑，无重复实现）

（2026-08-16 用户决定：套利引擎不再需要，已整线归档 legacy/，本文件不再托管。
 详见 docs/plans/2026-08-16_self_evolution_design.md DEF-10 / T0.6。）

安全约束：
  - pause 只拦方向性【开仓信号扫描】；止损止盈监控永不暂停。
  - 心跳文件沿用 watchdog 命名（heartbeat_directional）；watchdog 的 arb 项已随引擎移除。
  - 引擎异常被 tick 捕获记入 last_error，不拖垮 HTTP 服务。
"""
import threading
import time
import traceback
import json

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from engines.directional_trader import DirectionalTrader, connect as connect_dir


class ServiceTrader(DirectionalTrader):
    """方向性引擎 + 暂停开关。暂停时跳过 scan_signals，监控照常。"""

    def __init__(self, exchange=None, rt=None, db_path=None):
        super().__init__(exchange=exchange, rt=rt, db_path=db_path)
        self._pause = threading.Event()   # set() = 暂停开仓
        self.last_error = ""

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def scan_signals(self):
        if self._pause.is_set():
            print(f"[{time.strftime('%H:%M:%S')}] ⏸️ 已暂停开仓（HTTP /pause），跳过信号扫描")
            return
        super().scan_signals()


class TraderWorker:
    """托管方向性引擎线程（2s tick）。套利引擎已按用户决定移除（归档 legacy/）。"""

    def __init__(self):
        # 共享依赖：一个适配器、一条 WebSocket（方向性引擎使用）
        self.exchange = connect_dir()
        try:
            from data.realtime_okx import OKXRealtime
            # 2026-08-17: WS 覆盖全回退池(此前硬编码 5 币,池外下单无秒级行情)
            self.rt = OKXRealtime(config.SYMBOLS).start()
            print("共享 WebSocket 实时行情已接入")
        except Exception as e:
            print(f"WebSocket 启动失败，REST 兜底: {e}")
            self.rt = None
        self.trader = ServiceTrader(exchange=self.exchange, rt=self.rt)
        self._threads = []
        self._stop = threading.Event()
        self.started_at = 0.0
        self.last_hb_dir = 0.0    # 方向性引擎心跳

    # ---------- 生命周期 ----------
    def start(self):
        if self._threads:
            return
        from engines.directional_trader import acquire_instance_lock
        self._lock_handle = acquire_instance_lock()
        if self._lock_handle is None:
            raise RuntimeError("已有交易引擎实例在运行（engine.lock 被持有），拒绝启动第二个实例")
        self._stop.clear()
        self.started_at = time.time()
        # PID 文件沿用 watchdog 命名（写入点统一走 execution/pidfile——
        # code_graph 跨层共享状态告警修复）
        from execution.pidfile import write_pid
        write_pid("directional")
        self._threads = [
            threading.Thread(target=self._dir_loop, name="engine-directional", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self, timeout=6.0):
        self._stop.set()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=timeout)
        return all(not t.is_alive() for t in self._threads)

    # ---------- 引擎循环 ----------
    def _save_positions_snapshot(self):
        """本地仓位快照：把交易所持仓（唯一事实源）定期落库（storage 层）。"""
        try:
            import storage.db as sdb
            sdb.init_db()
            for p in self.exchange.fetch_positions():
                sdb.x("INSERT INTO position_snapshots (ts,inst_id,side,contracts,"
                      "base_qty,avg_px) VALUES (?,?,?,?,?,?)",
                      [time.time(), p.inst_id, p.side, p.contracts,
                       round(p.base_qty, 8), p.avg_px])
        except Exception as e:
            print(f"仓位快照失败: {e}")

    def _dir_loop(self):
        t = self.trader
        t._last_scan = 0
        t._last_risk_update = 0
        t.signal_cool = {}
        self._last_snapshot = 0
        self._last_analysis = 0
        # 2026-08-16 结构性修复: 独立心跳线程——扫描/每日刷新等长阻塞期间
        # 心跳不断更(激进档首轮 20 币扫描 + 全市场初筛需数分钟,曾两次触发
        # watchdog 误杀崩溃循环)。心跳与 tick 彻底解耦。
        stop_hb = threading.Event()

        def _hb_loop():
            from execution.pidfile import write_heartbeat
            while not stop_hb.is_set():
                try:
                    write_heartbeat("directional")
                    self.last_hb_dir = time.time()
                except Exception:
                    pass
                stop_hb.wait(10)

        threading.Thread(target=_hb_loop, name="engine-heartbeat",
                         daemon=True).start()
        try:
            while not self._stop.is_set():
                try:
                    t.tick()                       # 心跳由独立线程负责
                    self.last_hb_dir = time.time()
                    # 本地仓位快照（每 60 秒，交易所持仓是唯一事实源）
                    if time.time() - self._last_snapshot >= 60:
                        self._last_snapshot = time.time()
                        self._save_positions_snapshot()
                    # 每日看账（自我进化：定期分析问题并反馈，≥24h 跑一次）
                    if time.time() - self._last_analysis >= 24 * 3600:
                        self._last_analysis = time.time()
                        try:
                            from decision.analyst import run_daily
                            run_daily()
                        except Exception as e:
                            print(f"每日分析失败: {e}")
                except Exception as e:
                    tb = traceback.format_exc(limit=3)
                    t.last_error = f"{time.strftime('%H:%M:%S')} {e}\n{tb}"
                    print(f"方向性引擎异常: {e}")
                    try:
                        import storage.db as sdb
                        sdb.init_db()
                        sdb.x("INSERT INTO engine_errors (ts, engine, error, traceback) VALUES (?,?,?,?)",
                              [time.time(), "directional", str(e), tb])
                        try:
                            from tools.anomalies import register as _reg
                            _reg("engine_error", f"方向性引擎异常: {e}",
                                 str(e)[:200], severity="error")
                        except Exception:
                            pass
                    except Exception:
                        pass
                time.sleep(1)   # 2026-08-17 提速: 1s 节拍止损监控(持仓快照仍 2s 节流)
        finally:
            stop_hb.set()

    # ---------- 只读状态快照（供 HTTP 层） ----------
    def heartbeat_age(self) -> float:
        if self.last_hb_dir <= 0:
            return -1.0
        return time.time() - self.last_hb_dir

    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

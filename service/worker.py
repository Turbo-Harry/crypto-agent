"""
后台交易线程 — 完整交易系统的引擎托管层。

托管两个常驻引擎（共享一个交易所适配器 + 一条 WebSocket 行情连接）：
  1. 方向性引擎 DirectionalTrader —— 2s 止损监控 + 15min 回踩信号扫描
     （tick() 由 run() 抽取，服务端复用同一逻辑，无重复实现）
  2. 套利引擎 TradingMain —— 60s 事件检测 + 费率告警 + 套利持仓管理
     （ENABLE_FUNDING_ARB=False 时仍跑监控/告警，开仓路径被配置拦截）

安全约束：
  - pause 只拦方向性【开仓信号扫描】；止损止盈监控永不暂停。
  - 心跳文件沿用 watchdog 命名（heartbeat_directional/heartbeat_arb），
    旧 launchd/watchdog 部署零改动。
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
from engines.trading_main import TradingMain


class ServiceTrader(DirectionalTrader):
    """方向性引擎 + 暂停开关。暂停时跳过 scan_signals，监控照常。"""

    def __init__(self, exchange=None, rt=None):
        super().__init__(exchange=exchange, rt=rt)
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
    """托管两个引擎线程：方向性（2s tick）+ 套利（60s tick）。"""

    def __init__(self):
        # 共享依赖：一个适配器、一条 WebSocket（两引擎复用）
        self.exchange = connect_dir()
        try:
            from data.realtime_okx import OKXRealtime
            self.rt = OKXRealtime(["BTC", "ETH", "SOL", "XRP", "DOGE"]).start()
            print("共享 WebSocket 实时行情已接入（两引擎复用）")
        except Exception as e:
            print(f"WebSocket 启动失败，REST 兜底: {e}")
            self.rt = None
        self.trader = ServiceTrader(exchange=self.exchange, rt=self.rt)
        self.arb = TradingMain(exchange=self.exchange, rt=self.rt)
        self._threads = []
        self._stop = threading.Event()
        self.started_at = 0.0
        self.last_hb_dir = 0.0    # 方向性引擎心跳
        self.last_hb_arb = 0.0    # 套利引擎心跳

    # ---------- 生命周期 ----------
    def start(self):
        if self._threads:
            return
        self._stop.clear()
        self.started_at = time.time()
        # PID 文件沿用 watchdog 命名（两个都写，服务进程 PID 相同）
        for name in ("directional_trader.pid", "trading_main.pid"):
            with open(name, "w") as f:
                f.write(str(os.getpid()))
        self._threads = [
            threading.Thread(target=self._dir_loop, name="engine-directional", daemon=True),
            threading.Thread(target=self._arb_loop, name="engine-arb", daemon=True),
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
        while not self._stop.is_set():
            try:
                t.tick()                       # 心跳在 tick 内写
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
                except Exception:
                    pass
            time.sleep(2)

    def _arb_loop(self):
        a = self.arb
        a.price_history = {}
        a.alert_cool = {}
        a.signal_state = {}
        a.decision_cool = {}
        while not self._stop.is_set():
            try:
                a.tick()                       # 心跳在 tick 内写
                self.last_hb_arb = time.time()
            except Exception as e:
                print(f"套利引擎异常: {e}")
                try:
                    import storage.db as sdb
                    sdb.init_db()
                    sdb.x("INSERT INTO engine_errors (ts, engine, error, traceback) VALUES (?,?,?,?)",
                          [time.time(), "arb", str(e), traceback.format_exc(limit=3)])
                except Exception:
                    pass
            time.sleep(60)

    # ---------- 只读状态快照（供 HTTP 层） ----------
    def heartbeat_age(self) -> float:
        if self.last_hb_dir <= 0:
            return -1.0
        return time.time() - self.last_hb_dir

    def arb_heartbeat_age(self) -> float:
        if self.last_hb_arb <= 0:
            return -1.0
        return time.time() - self.last_hb_arb

    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

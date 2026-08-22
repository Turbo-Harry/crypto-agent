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
            self.rt = OKXRealtime(
                config.SYMBOLS, fetch_candles=self.exchange.fetch_candles).start()
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
            with sdb.tx() as conn:
                positions = self.exchange.fetch_positions()
                if not positions:
                    # 2026-08-21: 空仓时写一行哨兵(inst_id='-')——
                    # 否则一行都不写,H9 拿不到新时间戳误报'快照不新鲜'。
                    conn.execute(
                        "INSERT INTO position_snapshots (ts,inst_id,side,contracts,"
                        "base_qty,avg_px) VALUES (?,?,?,?,?,?)",
                        [time.time(), "-", "-", 0, 0, 0])
                for p in positions:
                    conn.execute(
                        "INSERT INTO position_snapshots (ts,inst_id,side,contracts,"
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
        # 2026-08-19 线程分离: 止损监控独立线程 1s 节拍——长扫描/风控阻塞
        # 主循环时监控照跑(此前靠逐币插拍打补丁,现从根上分离)。
        stop_mon = threading.Event()

        def _mon_loop():
            while not stop_mon.is_set():
                try:
                    import config as _cfg
                    _cfg.maybe_reload()
                    with t._mutex:
                        t.monitor()
                except Exception as e:
                    # 2026-08-19: 监控线程异常不能静默——与主循环同款
                    # 5 分钟同文本节流落库,持续坏掉会通过 H6 突发口径报警。
                    try:
                        import storage.db as sdb
                        sdb.init_db()
                        dup = sdb.q1("SELECT id FROM engine_errors WHERE error LIKE ? "
                                     "AND ts > ? ORDER BY id DESC LIMIT 1",
                                     [f"monitor线程: {str(e)[:80]}%",
                                      time.time() - 300])
                        if not dup:
                            sdb.x("INSERT INTO engine_errors (ts, engine, error, traceback) "
                                  "VALUES (?,?,?,?)",
                                  [time.time(), "monitor", f"monitor线程: {e}", ""])
                    except Exception:
                        pass
                stop_mon.wait(1)

        _mon_thread = threading.Thread(target=_mon_loop, name="engine-monitor",
                                       daemon=True)
        self._threads.append(_mon_thread)   # 优雅停机 join 时一起收
        _mon_thread.start()
        try:
            while not self._stop.is_set():
                try:
                    # 2026-08-21 热重载: 每拍查 config.py mtime,改动即生效
                    import config as _cfg
                    _changed = _cfg.maybe_reload()
                    if _changed:
                        print(f"[config] 热重载生效: {_changed}")
                        try:
                            from service.events import log_event
                            log_event("config_reload", {"changed": _changed})
                        except Exception:
                            pass
                        # 刷新各引擎模块的参数别名(函数体裸名引用读模块全局)
                        for _m in ("engines.review_pipeline", "engines.signal_scan",
                                   "engines.daily_scan", "engines.position_mgmt",
                                   "decision.experience_scoring", "decision.experiments"):
                            try:
                                import importlib
                                importlib.import_module(_m)._refresh_config()
                            except Exception:
                                pass
                        try:
                            import engines.directional_trader as _dt
                            _dt._refresh_config()
                        except Exception:
                            pass
                    # 监控已由独立线程负责,主循环只做风控+扫描
                    t.tick(run_monitor=False)
                    # 2026-08-17: tick 进度标记——主循环被网络黑洞阻塞时心跳线程
                    # 照常写心跳会让 watchdog 失明(51 分钟盲窗事故),此标记反映
                    # 主循环真实进度,watchdog 据此判真卡死。
                    from execution.pidfile import write_tick
                    write_tick("directional")
                    self.last_hb_dir = time.time()
                    # 本地仓位快照（每 60 秒，交易所持仓是唯一事实源）
                    if time.time() - self._last_snapshot >= 60:
                        self._last_snapshot = time.time()
                        self._save_positions_snapshot()
                    # 2026-08-23 消息面: 每小时刷新情感快照(决策门控用,
                    # best-effort,失败保持旧快照)
                    if time.time() - getattr(self, "_last_sentiment", 0) >= 3600:
                        self._last_sentiment = time.time()
                        try:
                            from decision.sentiment import fetch_sentiment
                            snap = fetch_sentiment()
                            print(f"[sentiment] composite={snap.get('composite')} "
                                  f"F&G={snap.get('fng_value')}")
                        except Exception:
                            pass
                    # 2026-08-21 每小时对账巡查: 交易所故障期成交回报丢失会
                    # 产生无台账持仓(HBAR 幽灵仓案例),不必等重启才发现。
                    if time.time() - getattr(self, "_last_reconcile_sweep", 0) >= 3600:
                        self._last_reconcile_sweep = time.time()
                        try:
                            t._reconcile_startup()
                        except Exception:
                            pass
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
                    # 2026-08-17: 同文本错误 5 分钟节流——SSL 抖动时每请求一
                    # 行会灌爆 engine_errors/anomalies(今晚 6 条 blip 触发
                    # H6 假告警)。首条落库+注册,同文本窗口内只打印不落库。
                    err_key = str(e)[:120]
                    try:
                        import storage.db as sdb
                        sdb.init_db()
                        dup = sdb.q1("SELECT id FROM engine_errors WHERE error LIKE ? "
                                     "AND ts > ? ORDER BY id DESC LIMIT 1",
                                     [err_key + "%", time.time() - 300])
                        if not dup:
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
            stop_mon.set()

    # ---------- 只读状态快照（供 HTTP 层） ----------
    def heartbeat_age(self) -> float:
        if self.last_hb_dir <= 0:
            return -1.0
        return time.time() - self.last_hb_dir

    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

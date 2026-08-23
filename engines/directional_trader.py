"""
方向性交易 agent — 回踩确认信号 + 2:1 盈亏比 + 合理杠杆。

策略逻辑（做多示例，做空对称）：
  信号：1h 多头趋势（EMA20>EMA50）+ 回踩 EMA20 不破 + 拒绝K线（下影线）
  入场：回踩确认后入场（合约，2-3x 杠杆，逐仓）
  止损：入场价 - 1×ATR（单笔风险 1%）
  止盈：入场价 + 2×ATR（2:1 盈亏比）
  进化：每笔复盘，胜率/盈亏比追踪，连亏冷却

架构：只依赖 exchange.base.ExchangeAdapter 抽象接口（无 ccxt）。
交易所访问分层见 exchange/__init__.py；单测可注入 FakeAdapter。

2026-08-20 按功能拆分（行为零变化，方法逐行搬移）：
  engines/signal_scan.py      SignalScanMixin   信号扫描/候选池/额度/冷却
  engines/position_mgmt.py    PositionMixin     开仓/条件单/失败落库
  engines/risk_monitor.py     RiskMonitorMixin  止损监控/熔断强平
  engines/review_pipeline.py  ReviewMixin       平仓复盘链/阈值进化门
本文件保留：进程入口、__init__ 组装、启动对账、行情辅助、tick/run 主循环。
改任何功能块前先读对应模块 docstring；跨块共享状态一律经 self.*。

用法：
  python3 directional_trader.py --once   扫一轮信号
  python3 directional_trader.py          常驻运行
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
SYMBOLS = config.SYMBOLS


def _refresh_config():
    """2026-08-21 热重载: config.maybe_reload 后由 worker 调用,
    把本模块别名刷新为新值(函数体裸名引用在调用时读模块全局)。"""
    global SYMBOLS
    SYMBOLS = config.SYMBOLS



from decision.notify import notify, trade_notifications_enabled
from decision.self_evolving_trader import SelfEvolvingTrader
from execution.trade_journal import TradeJournal
from exchange.base import ExchangeAdapter, ExchangeError

# 策略参数统一维护于 config.py（2026-08-16 用户指示: 数值不再分散）。
# 2026-08-20 拆分后各功能块自带所需别名,本文件只留自己用到的。


def connect() -> ExchangeAdapter:
    """构建交易所适配器（OKX 模拟盘）。策略层只见 ExchangeAdapter 接口。
    2026-08-22: 用户指示改用 ccxt 交易库(config.EXCHANGE_BACKEND="ccxt"),
    native 手写传输层保留可回滚(EXCHANGE_BACKEND="native")。"""
    import config
    import os as _os
    live = config.LIVE_MODE
    cred = (_os.path.expanduser(config.LIVE_CRED_FILE)
            if live else "okx_config.json")
    cfg = json.load(open(cred))
    if config.EXCHANGE_BACKEND == "ccxt":
        from exchange.ccxt_adapter import CCXTAdapter
        return CCXTAdapter(cfg["apiKey"], cfg["secret"], cfg["password"],
                           sandbox=not live)
    from exchange.okx_adapter import OKXAdapter
    return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"],
                      sandbox=not live)


def acquire_instance_lock(timeout=300.0):
    """单实例互斥(审计:双实例事故):对项目根 engine.lock 加 flock。
    已被持有 → 等待持有者退出(热备用),超时仍未拿到 → 返回 None。
    拿到 → 返回文件句柄(进程存续期间持锁,退出自动释放)。
    2026-08-23 双实例: paper 环境用 engine_paper.lock,与实盘实例互不阻塞。"""
    lock_name = ("engine_paper.lock"
                 if os.environ.get("CRYPTO_AGENT_MODE") == "paper"
                 else "engine.lock")
    lock_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             lock_name)
    try:
        import fcntl
    except ImportError:
        return open(lock_path, "w")   # 平台无 flock:退化为普通文件
    deadline = time.time() + timeout
    while True:
        try:
            f = open(lock_path, "a+")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
            return f
        except OSError:
            try:
                f.close()
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(2)

# 功能块（2026-08-20 拆分,见文件头说明）
from engines.signal_scan import SignalScanMixin, _build_trade_conditions  # noqa: F401
from engines.position_mgmt import PositionMixin
from engines.risk_monitor import RiskMonitorMixin
from engines.review_pipeline import ReviewMixin

class _ExpAdapter:
    """把 ScoredExperience 适配成 SelfEvolvingTrader 期望的 ExperienceBank 接口。
    （审计 B6：此前两套经验库并存，evolver 用旧库、交易记录用新库，闭环断裂）"""

    def __init__(self, bank):
        self.bank = bank

    def relevant(self, symbol=None, category=None, conditions=None):
        # R2-3：只返回 trusted（带 id，供采纳追踪）；discarded 走 discarded() 单独查
        # Phase 4：system 级教训（symbol='*'，analyst 日度看账产出）也进入决策参考
        # 2026-08-17: 场景条件向量匹配 + 验证计数透传(聚合强度计算用)
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"], "good": l.get("good", 0),
                "bad": l.get("bad", 0), "regime": l.get("regime"),
                "conditions": l.get("conditions")}
               for l in self.bank.trusted(symbol, conditions=conditions)]
        out += [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                 "lesson": l["content"], "good": l.get("good", 0),
                 "bad": l.get("bad", 0), "regime": l.get("regime"),
                 "conditions": l.get("conditions")}
                for l in self.bank.trusted(None, conditions=conditions)
                if l.get("symbol") == "*"]
        if category:
            out = [l for l in out if l["category"] == category]
        return out

    def discarded(self, symbol=None, category=None, conditions=None):
        """被证伪的经验（3 次验证且 <40 分）。信号模式失效检查必须读这里——
        trusted 语义是'证明有用'，信号失效教训经亏损验证后只会进 discarded。"""
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"]}
               for l in self.bank.discarded(symbol, conditions=conditions)]
        if category:
            out = [l for l in out if l["category"] == category]
        return out

    def candidates(self, symbol=None, category=None):
        """待验证候选经验（Phase0 T0.2：平仓复盘一致性初筛通过、等待后续独立
        交易验证）。决策层只做低权重参考（写入理由+采纳追踪），不改变任何参数——
        这是打破 unverified 死锁的采纳通道（见设计文档 v0.2 §5.1 Q3）。"""
        out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
                "lesson": l["content"]}
               for l in self.bank.candidates(symbol)]
        if category:
            out = [l for l in out if l["category"] == category]
        return out


class DirectionalTrader(SignalScanMixin, PositionMixin,
                        RiskMonitorMixin, ReviewMixin):
    def __init__(self, exchange: ExchangeAdapter = None, rt=None, db_path=None, *,
                 journal=None, decision_engine=None, experience_bank=None,
                 position_ledger=None, risk_manager=None, notifier=None,
                 event_logger=None):
        self.exchange = exchange or connect()
        # 审计/事件输出共享同一隔离路径：测试 db_path → <db>.events.jsonl。
        self._db_path = db_path
        # 2026-08-19 线程分离: 监控线程与主循环共享 journal/ledger 突变,
        # RLock 保护(监控线程每 1s 拿锁跑 monitor,主循环只在开仓/强平
        # 突变点拿锁——监控不再被扫描阻塞,扫描也不被监控饿死)。
        import threading
        self._mutex = threading.RLock()
        # 2026-08-22 实盘快照: 只在启动时读取,运行中改 config.LIVE_MODE
        # 不会切换真实/模拟(防意外真钱交易)。实盘还需凭证文件存在。
        import config as _c
        # 2026-08-23: 实盘判定 = LIVE_MODE + 真实交易所适配器 + 凭证存在。
        # fake/测试适配器(name != okx*)永远不进实盘口径(测试隔离)。
        _ad_name = getattr(self.exchange, "name", "")
        self.live_mode = bool(_c.LIVE_MODE) and _ad_name in ("okx", "okx-ccxt")
        if self.live_mode:
            import os as _os
            if not _os.path.exists(_os.path.expanduser(_c.LIVE_CRED_FILE)):
                print("❌ LIVE_MODE=True 但无实盘凭证文件,退回模拟盘")
                self.live_mode = False
        # AI provider 只允许真实 OKX 适配器使用，FakeAdapter/离线测试永不访问
        # 外部模型。新 Harness 仅在 paper 注入，并固定 shadow；live 仍保持既有
        # legacy AI 把关，不因研究接线扩大模型权限。
        _real_okx = _ad_name in ("okx", "okx-ccxt")
        self.ai_judge_enabled = bool(_c.AGENT_JUDGE_ENABLED and _real_okx)
        # 用户明确要求“固定 2:1，先预测再开仓”。先在真实 OKX 模拟盘
        # fail-closed 落地；FakeAdapter 单测与未重启的实盘实例均不受影响。
        self.require_2to1_prediction = bool(
            _real_okx and not self.live_mode and
            _c.PAPER_REQUIRE_VALIDATED_2TO1_PREDICTION)
        self.agent_model_call = None
        self.agent_proposal_model_call = None
        if (_real_okx and not self.live_mode
                and getattr(_c, "AGENT_HARNESS_ENABLED", False)):
            try:
                from decision.agent_judge import (
                    harness_model_available, production_harness_model_call,
                )
                if harness_model_available():
                    self.agent_model_call = production_harness_model_call
                    if getattr(_c, "AGENT_PROPOSAL_SHADOW_ENABLED", False):
                        from decision.agent_proposals import \
                            production_proposal_model_call
                        self.agent_proposal_model_call = \
                            production_proposal_model_call
                    print("Agent Harness: paper shadow provider ready")
                else:
                    print("Agent Harness: no provider key, shadow fallback only")
            except Exception as e:
                print(f"Agent Harness: provider unavailable, shadow fallback only: {e}")
        # 2026-08-16 结构性修复: 测试/fake 适配器必须静音飞书通知——
        # 此前 test_decision_loop 等跑套件时把假开仓单真的发到了用户飞书
        # (与 DEF-8 生产库污染同类的泄漏,这次是通知通道)。
        _external_output = trade_notifications_enabled(_ad_name)
        self._notify = (notifier if notifier is not None else
                        (notify if _external_output else (lambda *a, **k: None)))
        if event_logger is not None:
            self._log_event = event_logger
        elif _external_output:
            from execution.events import log_event as _write_event
            self._log_event = lambda event_type, payload=None: _write_event(
                event_type, payload, db_path=self._db_path)
        else:
            self._log_event = lambda *a, **k: False
        # Phase0 T0.4：审计/日志表隔离。db_path=None → 生产共享库（默认）；
        # 测试必须传隔离路径（防 scan_decisions 等污染生产表，见 pitfalls）。
        self.journal = (journal if journal is not None
                        else TradeJournal(db_path=self._db_path))
        # 带评分的经验库（历史经验不一定对，用交易结果验证）
        from decision.experience_scoring import ScoredExperience
        self.exp_bank = (experience_bank if experience_bank is not None else
                         ScoredExperience(path=(self._db_path
                                                or "experience_scored.json")))
        # 2026-08-23 经验共享(用户指示): 启动时把对端实例的教训/归纳镜像进
        # 本库参与决策;镜像行 origin='peer',本实例只读不验证(防双重计数)。
        if getattr(config, "EXPERIENCE_SHARE_ENABLED", False) \
                and getattr(config, "EXPERIENCE_PEER_DB", ""):
            try:
                from decision.experience_scoring import sync_peer_lessons
                _a, _u, _r = sync_peer_lessons(
                    self.exp_bank.db_path, config.EXPERIENCE_PEER_DB)
                if _a or _u or _r:
                    self.exp_bank._load()
                    print(f"[经验共享] 启动同步: 新增 {_a} 条教训, "
                          f"更新 {_u} 条, 归纳 {_r} 条")
            except Exception as e:
                print(f"[经验共享] 启动同步失败: {e}")
        # 统一经验库：evolver 的决策也走 ScoredExperience（B6）。依赖通过
        # 构造接口注入，决策组件不再自行打开另一份全局 journal/经验库。
        exp_adapter = _ExpAdapter(self.exp_bank)
        self.evolver = (decision_engine if decision_engine is not None else
                        SelfEvolvingTrader(journal=self.journal,
                                           bank=exp_adapter))
        # 持仓所有权账本（R1-12）：组合总敞口 ≤600 + claim/release
        from execution.position_ownership import PositionLedger as _PL
        # 2026-08-22 实盘: 总敞口上限用 LIVE_MAX_TOTAL
        self.ledger = (position_ledger if position_ledger is not None else
                       _PL(path=(self._db_path or "position_ownership.json"),
                           max_total_notional=(
                               config.LIVE_MAX_TOTAL if self.live_mode
                               else config.MAX_TOTAL_NOTIONAL)))
        # 每日候选池（用户要求：每天扫全市场挑适合下单的币；评分用于动态笔数）
        from engines.daily_scan import load_watchlists
        _watch_pools = load_watchlists(db_path=self._db_path)
        self.crypto_watchlist = list(_watch_pools["crypto"])
        self.stock_watchlist = list(_watch_pools["stock"])
        self.watchlist = self.crypto_watchlist + self.stock_watchlist
        self.watch_scores = {**_watch_pools["crypto"], **_watch_pools["stock"]}
        self._watch_date = ""
        self._last_watch_refresh = 0
        print(f"今日加密候选池 {len(self.crypto_watchlist)} 个: "
              f"{self.crypto_watchlist}")
        print(f"今日美股候选池 {len(self.stock_watchlist)} 个: "
              f"{self.stock_watchlist}")
        for b in self.watchlist:
            print(f"  {b}: 当日评分 {self.watch_scores.get(b)} → 允许笔数 {self._trade_budget(b)}")
        # 阈值自适应（决策阈值用分数→盈亏分布校准）
        from decision.threshold_learning import ThresholdLearner
        # 2026-08-16 用户指示采集加速: 信号分门槛降到 50,阈值初始 70→45
        # (若保持 70,50<70 会让全部信号被拒)。影子分喂入后满 30 样本仍可校准。
        # 2026-08-20 DEF-5 闭环: gated=True——校准只产提案,变更必须过进化门;
        # db_path 透传(测试隔离,与 T0.4 同口径)。
        self.threshold_learner = ThresholdLearner(path="threshold_state_dir.json",
                                                  initial_threshold=config.THRESHOLD_INITIAL,
                                                  db_path=self._db_path, gated=True)
        # 阈值进化门（DEF-5）: 提案→影子验证→晋升生效→观察期退化回滚基线。
        # 状态文件随 db_path 隔离(测试进临时目录,不污染仓库根)。
        # 2026-08-23 双实例: paper 后缀 _paper,与实盘门状态文件互不覆盖。
        from decision.evolution_gate import EvolutionGate
        _sfx = "_paper" if os.environ.get("CRYPTO_AGENT_MODE") == "paper" else ""
        _gate_file = (os.path.join(os.path.dirname(os.path.abspath(self._db_path)),
                                   f"evolution_gate_threshold{_sfx}.json")
                      if self._db_path else f"evolution_gate_threshold{_sfx}.json")
        self.threshold_gate = EvolutionGate(
            "方向性阈值层", _gate_file,
            min_shadow_samples=config.GATE_MIN_SHADOW,
            min_edge=config.GATE_MIN_EDGE,
            batch_size=config.GATE_OBSERVE_BATCH,
            on_rollback=lambda: self.threshold_learner.apply_threshold(
                config.THRESHOLD_INITIAL))
        # 2026-08-23 策略保持一致(用户指示): 启动时合并对端实例的策略演化
        # 状态(阈值+校准样本+扫描尺子),两实例用同一套反哺产物做决策。
        if getattr(config, "STRATEGY_SYNC_ENABLED", False) \
                and getattr(config, "EXPERIENCE_PEER_DB", ""):
            try:
                from decision.strategy_sync import sync_strategy
                _res = sync_strategy(self._db_path, config.EXPERIENCE_PEER_DB)
                if _res["records_added"] or _res["threshold_updated"]:
                    self.threshold_learner._load()
                    print(f"[策略同步] 启动合并: 样本+{_res['records_added']}, "
                          f"阈值{'更新' if _res['threshold_updated'] else '保持'} "
                          f"→ {self.threshold_learner.threshold}, "
                          f"尺子kv {_res['kv_synced']} 条")
            except Exception as e:
                print(f"[策略同步] 启动合并失败: {e}")
        # 账户级风控（审计 CR-2：RiskManager 必须真正接线）
        from risk.risk_manager import RiskManager
        try:
            eq = self.exchange.fetch_balance().total_eq
        except Exception:
            eq = 0
        self.risk = (risk_manager if risk_manager is not None else
                     RiskManager(initial_equity=eq if eq > 0 else 4190))
        self._halt_notified = False
        # 审计 C1/H2:启动对账——交易所持仓 ∪ 未平仓 journal 为唯一事实源,
        # 幽灵 claim 物理释放;无台账的交易所持仓仅告警(不自动下单)。
        if getattr(self.exchange, "name", "") == "okx":
            self._reconcile_startup()
        # WebSocket 实时价格（tick 级止损止盈监控，替代 6 小时轮询 — OP-1）
        # 服务模式下由 service 注入共享 rt（与套利引擎共用一条 WS 连接）
        self.rt = rt
        if self.rt is None and getattr(self.exchange, "name", "") in ("okx", "okx-ccxt"):
            try:
                from data.realtime import make_realtime
                # 2026-08-23: 后端按 config.REALTIME_BACKEND 切换(ccxtpro/okx)
                self.rt = make_realtime(
                    SYMBOLS, fetch_candles=self.exchange.fetch_candles).start()
                print("实时价格已接入（止损止盈 tick 级监控）")
            except Exception as e:
                print(f"WebSocket 启动失败，止损监控退回 REST 轮询: {e}")

    @property
    def service_api(self):
        """Return the stable service-facing interface for this runtime.

        HTTP and other inbound adapters must use this boundary instead of
        reaching into the engine's mutable collaborators.  It is cached so one
        runtime has one adapter identity for its full lifetime.
        """
        api = getattr(self, "_service_api", None)
        if api is None:
            from engines.runtime_api import DirectionalRuntimeAPI
            api = DirectionalRuntimeAPI(self)
            self._service_api = api
        return api

    def _reconcile_startup(self):
        """启动对账(审计 C1/H2):幽灵 claim 物理释放;无台账持仓告警。"""
        try:
            positions = self.exchange.fetch_positions()
        except Exception as e:
            print(f"启动对账:无法获取交易所持仓,跳过: {e}")
            return
        open_journal = [t for t in self.journal.trades if t.get("status") == "open"]
        active = set()
        for p in positions:
            if p.base_qty <= 0:
                continue
            active.add(f"{p.base}/USDT:USDT:{p.side}")
        for t in open_journal:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            sym = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            active.add(f"{sym}:{t.get('direction') or 'long'}")
        released = self.ledger.reconcile(active)
        for k in released:
            print(f"🧹 启动对账:释放幽灵 claim {k}")
            self._notify(f"🧹 启动对账:释放幽灵 claim {k}")
        # DEF-11:账本缺失/不完整的未平仓 journal 交易补账——journal 是本策略
        # 事实源(pitfalls),重启后账本若丢 claim,组合敞口闸门会漏计既有持仓。
        # restore() 不走 600 闸门(恢复既有事实而非授予新敞口),语义=以聚合值
        # 覆盖(幂等);同 symbol+side 的多笔交易共享同一 key,必须先聚合再补。
        pending = {}
        for t in open_journal:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            sym = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            key = f"{sym}:{t.get('direction') or 'long'}"
            qty = float(t.get("size") or 0)
            notional = float(t.get("notional_usdt")
                             or (qty * float(t.get("entry_price") or 0)))
            if qty <= 0:
                continue
            p = pending.setdefault(key, {"qty": 0.0, "notional": 0.0})
            p["qty"] += qty
            p["notional"] += notional
        for key, p in pending.items():
            sym, side = key.rsplit(":", 1)
            # 以 journal 聚合值为准:部分补账残留也会被覆盖修正
            self.ledger.restore(sym, side, "dir", p["qty"], p["notional"])
            print(f"🔧 启动对账:补账 {key} qty={p['qty']} notional={p['notional']:.0f}")
        # 2026-08-23 反向对账: journal 开着的交易在交易所已无持仓(人工平仓/
        # 实盘切换/回报丢失)→ 按现价闭环台账。交易所是唯一事实源,持仓没了
        # 台账还开着才是幽灵(此前这些孤儿永悬,实盘切换前 4 笔 demo 即此例)。
        for t in open_journal:
            base = t["symbol"]
            has_pos = any(
                p.base == base and p.side == (t.get("direction") or "long")
                and p.base_qty > 0 for p in positions)
            if has_pos:
                continue
            try:
                px = self._ticker_last(base)
            except Exception:
                px = None
            if not px:
                continue
            closed = self.journal.log_exit(t["id"], px, "对账:交易所无持仓,闭环台账")
            if closed:
                try:
                    self.ledger.release(
                        f"{base}/USDT:USDT", t.get("direction") or "long",
                        "dir", float(t.get("size") or 0),
                        float(t.get("size") or 0) * float(t.get("entry_price") or 0))
                except Exception:
                    pass
                print(f"🧾 启动对账:闭环孤儿台账 {t['id']} {base} @ {px}")
                self._notify(f"🧾 启动对账:闭环孤儿台账 {t['id']} {base} @ {px}")
                try:
                    self._post_close_review(closed, t)
                except Exception:
                    pass
        for p in positions:
            if p.base_qty <= 0:
                continue
            has_journal = any(
                t["symbol"] == p.base and (t.get("direction") or "long") == p.side
                for t in open_journal)
            if not has_journal:
                print(f"⚠️ 启动对账:交易所存在无台账持仓 {p.inst_id} {p.side} "
                      f"qty={p.base_qty}(仅告警,人工处置)")
                self._notify(f"⚠️ 启动对账:无台账持仓 {p.inst_id} {p.side} qty={p.base_qty}")
                # 2026-08-21: 幽灵仓也进统一异常中心(交易所故障期成交回报丢失
                # 会产生无台账持仓,HBAR 案例)——值守 AI 与人工都能看到。
                try:
                    from storage.anomaly_repository import register as _reg
                    _reg("reconcile",
                         f"无台账持仓 {p.inst_id} {p.side} qty={p.base_qty}",
                         "交易所持仓无对应 journal 记录(疑似成交回报丢失),"
                         "仅告警待人工处置", severity="warning")
                except Exception:
                    pass

    # ---------- 场所/行情辅助（依赖 ExchangeAdapter 接口） ----------
    def _inst_id(self, base, venue="swap"):
        return f"{base}-USDT-SWAP" if venue == "swap" else f"{base}-USDT"

    def _fetch_klines_any(self, base, tf, limit):
        """K线获取（场所自适应）：优先合约（策略在合约腿执行），无合约回退现货。
        返回 raw OHLCV 列表（[ts,o,h,l,c,v]）或 None。"""
        venue = self.exchange.venue_for(base)
        if venue is None:
            return None
        try:
            candles = self.exchange.fetch_candles(self._inst_id(base, venue), tf, limit)
            if len(candles) >= 20:
                return [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in candles]
        except ExchangeError:
            return None
        return None

    def _ticker_last(self, base, prefer_swap=False):
        """最新价（场所自适应）。prefer_swap=True 时优先合约价格。"""
        venue = self.exchange.venue_for(base, prefer_swap=prefer_swap)
        if venue is None:
            return None
        try:
            return self.exchange.fetch_ticker_last(self._inst_id(base, venue))
        except ExchangeError:
            return None

    def run_once(self):
        self.scan_signals()
        self.monitor()


    @staticmethod
    def _dir_cn(direction):
        """方向中文标签: long→开多, short→开空(2026-08-18 用户要求通知可见方向)。"""
        return "开多" if direction == "long" else "开空"

    def effective_threshold(self):
        """实盘决策阈值(2026-08-23 用户指示'实盘阈值上调到40'):
        实盘实例在自适应阈值之上再设 40 分下限(真金更挑信号);
        模拟盘保持激进,直接用学习器阈值。阈值学习/策略同步照常合并,
        下限只在决策门处生效。"""
        base = self.threshold_learner.threshold
        if getattr(self, "live_mode", False):
            return max(base, getattr(config, "LIVE_THRESHOLD_FLOOR", 40))
        return base

    def tick(self, run_monitor=True):
        """单拍主循环体（服务模式由 service/worker 线程调用；独立模式由 run() 调用）。
        包含：心跳、每分钟账户风控、2s 止损监控、15min 信号扫描。
        run_monitor(2026-08-19 线程分离): 生产 worker 传 False——监控由专用
        线程 1s 节拍执行,长扫描不再阻塞止损监控;测试/standalone 保持 True。"""
        now = time.time()
        # R2-4: 心跳（watchdog 超时 30s 判定）。写入点统一走 execution/pidfile
        # （code_graph 跨层共享状态告警修复: 文件名字面量只在 pidfile 一处）。
        from execution.pidfile import write_heartbeat
        write_heartbeat("directional")
        # 0. 账户级风控：净值喂入 + 熔断检查（每分钟 — 审计 CR-2）
        if now - self._last_risk_update >= 60:
            self._last_risk_update = now
            eq = self.exchange.fetch_balance().total_eq
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
            if not self.risk.can_trade():
                if not self._halt_notified:
                    self._halt_notified = True
                    msg = (f"⛔ 风控熔断: {self.risk.halt_reason}\n"
                           f"正在强制平掉本策略全部持仓…")
                    self._log_event("halt", {"reason": self.risk.halt_reason,
                                             "equity": eq})
                    print(msg)
                    self._notify(msg)
                    self._log_risk_event("halt", self.risk.halt_reason, eq)
                # 审计 H4:熔断期间止损监控绝不暂停;强平失败每 30s 重试
                if now - getattr(self, "_last_liq_attempt", 0) >= 30:
                    self._last_liq_attempt = now
                    self._liquidate_all(self.risk.halt_reason)
                if run_monitor:
                    self.monitor()
                time.sleep(2)
                return
            elif self._halt_notified:
                self._halt_notified = False
                self._log_risk_event("recovery", "风控解除", eq)
                self._notify("✅ 风控解除，恢复信号扫描")
        # 1. tick 级止损止盈监控（每 2 秒 — OP-1，替代 6 小时轮询）
        if run_monitor:
            self.monitor()
        # 2. 信号扫描（间隔 config.SCAN_INTERVAL_MINUTES — 2026-08-21 用户改 5 分钟）
        if now - self._last_scan >= config.SCAN_INTERVAL_MINUTES * 60:
            self._last_scan = now
            self.scan_signals()

    def run(self):
        lock = acquire_instance_lock()
        if lock is None:
            print("❌ 已有交易引擎实例在运行（engine.lock 被持有），本进程退出")
            sys.exit(1)
        self._notify("🎯 方向性交易 agent 启动（回踩确认 + 2:1盈亏比 + 2-3x杠杆 + tick级止损）")
        # R2-4: PID + 心跳文件（watchdog 用）；写入点统一走 execution/pidfile
        from execution.pidfile import write_pid
        write_pid("directional")
        self._last_scan = 0
        self._last_risk_update = 0
        self.signal_cool = {}   # 同币信号冷却（SIGNAL_COOLDOWN_MINUTES）
        while True:
            try:
                self.tick()
            except Exception as e:
                print(f"异常: {e}")
            time.sleep(1)   # 2026-08-17 提速: 与 worker 一致的 1s 止损节拍


if __name__ == "__main__":
    dt = DirectionalTrader()
    if "--once" in sys.argv:
        dt.run_once()
    else:
        dt.run()

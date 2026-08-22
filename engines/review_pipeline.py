"""
复盘管道层（ReviewMixin）— 2026-08-20 从 directional_trader 按功能拆分。

职责：平仓后复盘链（deep_review → 插针反转采集 → 报告落盘 → 教训一致性
初筛 → 经验库 → 采纳验证 → 阈值进化门 → 风控净值 → 通知）、
阈值进化门接线（DEF-5：提案 → 影子验证 → 晋升/回滚）。
方法体与拆分前逐行一致（行为零变化）；宿主为 DirectionalTrader。
"""
import time

import config
SIGNAL_SCORE = config.SIGNAL_SCORE


def _refresh_config():
    """2026-08-21 热重载: config.maybe_reload 后由 worker 调用,
    把本模块别名刷新为新值(函数体裸名引用在调用时读模块全局)。"""
    global SIGNAL_SCORE
    SIGNAL_SCORE = config.SIGNAL_SCORE



from decision.review_engine import deep_review
from execution.trade_journal import realized_pnl_usdt

# 参数别名（统一维护于 config.py,本模块不私藏数值）


class ReviewMixin:
    """平仓复盘/进化门功能块。"""

    def _post_close_review(self, closed, t):
        """平仓后复盘链（R1-1/B6/R2-3/R2-6 + Phase0 T0.1/T0.2/T0.3）：
        deep_review → 插针反转采集 → 报告落盘 → 教训一致性初筛（candidate/dubious）
        → 经验库 → 采纳验证 → 阈值 → 风控净值。"""
        base = closed["symbol"]
        # Phase0 T0.3：止损出场后采集"是否反转"（post_exit_reverse，原死参数）。
        # 语义：long 止损后现价回到止损上方 / short 回到下方 = 被插针扫掉。
        post_rev = None
        try:
            if closed.get("exit_reason") and "止损" in closed["exit_reason"]:
                venue = t.get("venue") or "swap"
                last = self._ticker_last(base, prefer_swap=(venue == "swap"))
                stop = t.get("stop_loss")
                if last and stop:
                    direction = t.get("direction") or "long"
                    post_rev = (last > stop) if direction == "long" else (last < stop)
        except Exception:
            post_rev = None
        report = deep_review(closed,
                             atr_value=t.get("atr_value"),
                             signal_price=t.get("signal_price"),
                             post_exit_reverse=post_rev)   # Phase0 T0.3
        # 复盘报告落盘（写入 trade_journal.json 该笔记录的 review 字段，事后可查）
        self.journal.save_review(t["id"], report)
        # Phase 1: 离场特征落库（MFE/MAE 由 1m K 线高低点计算;采集失败不影响复盘链）
        try:
            kl1m = self._fetch_klines_any(base, "1m", 500)
            from engines.feature_collector import collect_close_features
            collect_close_features(t["id"], t, closed, kl1m, post_rev,
                                   db_path=self._db_path)
        except Exception:
            pass
        lessons = report.get("lessons", [])
        pnl = closed.get("pnl")
        # Phase 4: 教训带 regime 标签(取自本笔入场特征),供同环境结构化匹配
        regime_tag = None
        trend_slope = None
        try:
            import storage.db as sdb
            row = sdb.q1("SELECT regime_tag, trend_slope FROM trade_features "
                         "WHERE trade_id=?", [t["id"]], db_path=self._db_path)
            if row:
                regime_tag = row["regime_tag"]
                trend_slope = row.get("trend_slope")
        except Exception:
            regime_tag = None
        # 2026-08-17 场景条件向量: 方向+波动带+趋势+信号类型,与决策层逐维
        # 匹配(缺失维度通配)。regime 字段保留兼容旧数据。
        lesson_conditions = {
            "direction": t.get("direction") or "long",
            "vol_band": regime_tag or "",
            "trend": ("" if trend_slope is None
                      else "up" if trend_slope > 0.0005
                      else "down" if trend_slope < -0.0005 else "flat"),
            "signal_type": "pullback",
        }
        for l in lessons:
            # Phase0 T0.2 一级·一致性初筛：教训的归因方向（implies）与本笔结果一致
            # → candidate（可被决策层低权重采纳）；不一致 → dubious（不进采纳池）。
            # 注意：这只是初筛，不做评分——评分只来自后续独立交易（防循环论证，见
            # 设计文档 v0.2 §5.1 Q3 与 experience_scoring.validate）。
            implies = l.get("implies")
            if implies:
                consistent = (pnl > 0) if implies == "win" else (pnl < 0)
                status = "candidate" if consistent else "dubious"
            else:
                status = "unverified"
            # 2026-08-21 用户要求'经验从历史看是否有符合的': 教训诞生即查
            # 历史同场景先例(symbol+方向+波动带+趋势),先验只观测不进验证循环。
            hist = None
            try:
                from decision.experience_scoring import historical_evidence
                hist = historical_evidence(base, lesson_conditions.get("direction"),
                                           conditions=lesson_conditions,
                                           db_path=self._db_path)
            except Exception:
                hist = None
            self.exp_bank.add(base, l["category"], l["lesson"], t["id"],
                              status=status, regime=regime_tag,
                              conditions=lesson_conditions, hist_evidence=hist)
        # R2-3：只 validate 本笔实际采纳的经验（替换全量 trusted validate 回声）
        for lid in t.get("adopted_lesson_ids") or []:
            self.exp_bank.validate(lid, closed["pnl"])
        # 2026-08-21 用户洞察: 单条不盈利,combo 可能盈利——单条验证会误杀
        # 组合价值。记录组合试验(≥2 条教训同时采纳),只观测不生效:
        # combo 统计达标后走 experiments 提案通道,绝不自动改决策。
        try:
            from decision.experience_scoring import record_combo_trial
            record_combo_trial(t.get("id"), t.get("adopted_lesson_ids") or [],
                               closed, db_path=self._db_path)
        except Exception:
            pass
        # 阈值自适应：记录本次【真实】决策分数 + 结果。
        # 2026-08-20 DEF-5 闭环: 校准不再直接生效,走进化门(提案→影子→晋升/回滚)。
        score = t.get("score") or SIGNAL_SCORE
        self._threshold_gate_step(score, closed["pnl"])
        # 2026-08-18 用户要求: 平仓通知展示具体收益金额(USDT),不是只有百分比。
        # 2026-08-23 fix: pnl_usdt 提前到硬止损块之前——此前 live 首笔平仓会
        # UnboundLocalError 被吞,硬止损静默失效。
        pnl_usdt = realized_pnl_usdt(closed) or 0.0
        # 2026-08-22 实盘硬止损: 累计实亏达 LIVE_HARD_STOP_USDT → 停手。
        # 用 kv 持久化(重启不重置),与账户总权益无关(账户大时 1.5% 日线
        # 熔断金额会超过预算,此线才是可靠边界)。
        live_real = None
        if getattr(self, "live_mode", False):
            try:
                import storage.db as sdb
                sdb.init_db(self._db_path)
                cur = sdb.q1("SELECT value FROM kv WHERE key='live_realized'",
                             db_path=self._db_path)
                live_real = (float(cur["value"]) if cur else 0.0) + pnl_usdt
                sdb.x("INSERT OR REPLACE INTO kv (key, value) VALUES ('live_realized', ?)",
                      [f"{live_real:.6f}"], db_path=self._db_path)
                if live_real <= -config.LIVE_HARD_STOP_USDT:
                    self.risk.halted = True
                    self.risk.halt_reason = (
                        f"实盘硬止损: 累计实亏 {live_real:.2f} USDT "
                        f"≤ -{config.LIVE_HARD_STOP_USDT} USDT")
                    print(f"⛔ {self.risk.halt_reason},停止新开仓")
                    self._notify(f"⛔ {self.risk.halt_reason}\n实盘自动停手,请人工确认")
                    self._log_risk_event("live_hard_stop",
                                         self.risk.halt_reason, 0)
            except Exception:
                pass
        # 账户级风控：净值更新（平仓后）
        try:
            eq = self.exchange.fetch_balance().total_eq
            if eq > 0:
                self.risk.update_equity(eq, time.strftime("%Y-%m-%d"))
        except Exception:
            pass
        exit_reason_short = (closed.get("exit_reason") or "平仓")[:20]
        sign = "+" if pnl_usdt >= 0 else ""
        live_line = (f"\n实盘累计盈亏 **{live_real:+.2f} USDT**"
                     if live_real is not None else "")
        msg = (f"📊 平仓 {base} {self._dir_cn(t.get('direction') or 'long')}\n"
               f"盈亏 **{sign}{pnl_usdt:.2f} USDT**（{closed['pnl']*100:+.1f}%）\n"
               f"原因：{exit_reason_short}\n"
               f"复盘 {len(lessons)} 条新经验（待验证）· "
               f"验证了 {len(t.get('adopted_lesson_ids') or [])} 条\n"
               f"当前阈值 {self.threshold_learner.threshold}{live_line}")
        try:
            from service.events import log_event
            log_event("close", {"tid": t.get("id"), "symbol": base,
                                "dir": t.get("direction"),
                                "pnl_usdt": pnl_usdt,
                                "pnl_pct": closed.get("pnl"),
                                "reason": exit_reason_short,
                                "lessons": len(lessons)})
        except Exception:
            pass
        print(msg)
        self._notify(msg)

    def _threshold_gate_step(self, score, pnl):
        """阈值进化门接线（2026-08-20，DEF-5 闭环）。每笔真实平仓走一步：
          1) 学习器只记录样本（gated 模式，不自动改阈值）；
          2) 现役样本喂 gate（晋升后的观察期靠它检测退化→自动回滚基线）；
          3) 有候选阈值时，按候选的【反事实决策】记影子样本——候选会拒绝的
             交易记 0（=空仓），影子样本满 GATE_MIN_SHADOW 且期望优势
             ≥ GATE_MIN_EDGE 才晋升生效；
          4) 无候选时，用校准数学产提案（只算不改）。
        诚实声明：反事实只对【收紧】方向有证据力——放松方向新增的交易现实中
        未执行、无盈亏可依，影子期表现与现役恒相同，会被 min_edge>0 拒绝。
        这与"宁可做对，也不做错"一致：放宽门槛必须由用户拍板，不由机器自动。
        任何异常只打日志，不拖垮复盘链。"""
        try:
            self.threshold_learner.record(score, pnl)
            self.threshold_gate.record_incumbent(pnl)
            cand = self.threshold_gate.state.get("candidate")
            if cand:
                cand_thr = (cand.get("meta") or {}).get("threshold")
                shadow_pnl = pnl if (cand_thr is None or score >= cand_thr) else 0.0
                res = self.threshold_gate.record_shadow(shadow_pnl) or {}
                if res.get("action") == "promote" and cand_thr is not None:
                    old = self.threshold_learner.apply_threshold(cand_thr)
                    msg = (f"🧬 阈值进化门晋升: {old} → {cand_thr} "
                           f"(影子均值 {res.get('cand', 0):+.4f} vs "
                           f"现役 {res.get('inc', 0):+.4f})")
                    print(msg)
                    self._notify(msg)
                elif res.get("action") == "reject":
                    print(f"阈值进化门淘汰候选: 影子均值 {res.get('cand', 0):+.4f} "
                          f"未超越现役 {res.get('inc', 0):+.4f}")
            else:
                proposal = self.threshold_learner.propose()
                if proposal is not None:
                    self.threshold_gate.propose_candidate(
                        f"阈值{proposal}",
                        source="分数-盈亏桶校准(threshold_learning)",
                        meta={"threshold": proposal})
                    print(f"阈值进化门收到候选: {self.threshold_learner.threshold} "
                          f"→ {proposal}（进入影子验证,不改现役）")
        except Exception as e:
            print(f"阈值进化门异常(不影响复盘链): {e}")

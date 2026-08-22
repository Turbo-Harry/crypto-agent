"""
信号扫描层（SignalScanMixin）— 2026-08-20 从 directional_trader 按功能拆分。

职责：回踩确认信号（1h + 4h MTF 共振）、候选池扫描主循环、动态笔数额度、
信号冷却、扫描决策落库、长扫描逐币插拍（心跳/监控/快照）。
方法体与拆分前逐行一致（行为零变化）；宿主为 DirectionalTrader（MRO 组装）。
依赖宿主属性：exchange/journal/evolver/rt/watchlist/watch_scores/
threshold_learner/signal_cool/_db_path/_notify 等。
"""
import time

import config
MTF_ENABLED = config.MTF_ENABLED
SIGNAL_COOLDOWN_MINUTES = config.SIGNAL_COOLDOWN_MINUTES
SYMBOLS = config.SYMBOLS
SIGNAL_SCORE = config.SIGNAL_SCORE
FLAG_USE_SHADOW_SCORE_GATE = config.FLAG_USE_SHADOW_SCORE_GATE


def _refresh_config():
    """2026-08-21 热重载: config.maybe_reload 后由 worker 调用,
    把本模块别名刷新为新值(函数体裸名引用在调用时读模块全局)。"""
    global MTF_ENABLED
    MTF_ENABLED = config.MTF_ENABLED
    global SIGNAL_COOLDOWN_MINUTES
    SIGNAL_COOLDOWN_MINUTES = config.SIGNAL_COOLDOWN_MINUTES
    global SYMBOLS
    SYMBOLS = config.SYMBOLS
    global SIGNAL_SCORE
    SIGNAL_SCORE = config.SIGNAL_SCORE
    global FLAG_USE_SHADOW_SCORE_GATE
    FLAG_USE_SHADOW_SCORE_GATE = config.FLAG_USE_SHADOW_SCORE_GATE



from strategy.indicators import ema, atr

# 参数别名（统一维护于 config.py,本模块不私藏数值）


def _build_trade_conditions(sig):
    """信号/交易的场景条件向量(2026-08-17): direction + vol_band + trend +
    signal_type。regime 是 compute_regime 输出的 dict(含 tag/trend_slope)。"""
    from decision.experience_scoring import build_conditions
    return build_conditions(direction=sig.get("dir"),
                            regime_dict=sig.get("regime"),
                            signal_type="pullback")



class SignalScanMixin:
    """信号扫描功能块。"""

    # ---------- 信号：回踩确认（1 小时线 · 真日内短线） ----------
    def scan_signal(self, base, wick_ratio=None):
        """检查某币的回踩确认信号（1 小时 K 线，日内短线）。
        多周期共振过滤（MTF）：1h 信号方向必须与 4h 趋势同向——顺大势做小势，
        只抓高概率时点，不频繁交易。返回信号 dict 或 None。
        wick_ratio: 覆盖影线门槛（扫描影子用候选值）；默认读批准后的活体值。"""
        try:
            kl = self._fetch_klines_any(base, "1H", 100)
            if not kl:
                return None
            klines = [{"open": k[1], "high": k[2], "low": k[3], "close": k[4],
                       "volume": k[5]} for k in kl]
        except Exception:
            return None
        if len(klines) < 60:
            return None
        # MTF 共振：4h 趋势方向（做多要求 4h 多头，做空要求 4h 空头）
        tf4h_trend = 0   # 1=多, -1=空, 0=未知（数据不足时视为无共振，放弃信号）
        try:
            kl4 = self._fetch_klines_any(base, "4H", 60)
            if kl4:
                c4 = [k[4] for k in kl4]
                if len(c4) >= 50:
                    e20, e50 = ema(c4, 20), ema(c4, 50)
                    tf4h_trend = 1 if e20[-1] > e50[-1] else -1
        except Exception:
            tf4h_trend = 0
        closes = [k["close"] for k in klines]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        atr_val = atr(klines, 14)

        last = klines[-1]
        body = abs(last["close"] - last["open"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        upper_wick = last["high"] - max(last["open"], last["close"])
        # 日内入场参考价用实时 tick 价（市价单实际成交价），
        # 趋势/ATR 仍来自 1 小时线——避免"信号收盘价 vs 市价成交"错位（RES-11）
        entry_ref = self._ticker_last(base)
        if entry_ref is None:
            entry_ref = last["close"]

        def _shadow(price_near_ema, wick):
            """Phase 1 T1.3 影子连续分（0-100，只记录、不进决策——阈值逻辑零改动）:
            拒绝K线强度 34% + 回踩深度适中 33% + 1h 趋势离散度 33%。
            同时采集轻量 regime 标签（T1.4:波动率分位+趋势斜率,不用 HMM）。"""
            try:
                wick_s = min(wick / body, 3.0) / 3.0 if body > 0 else 0.0
                depth_s = (max(0.0, 1.0 - abs(price_near_ema - ema20[-1]) / atr_val)
                           if atr_val and atr_val > 0 else 0.0)
                trend_s = (min(abs(ema20[-1] - ema50[-1]) / (ema50[-1] * 0.02), 1.0)
                           if ema50[-1] else 0.0)
                score = round(100 * (0.34 * wick_s + 0.33 * depth_s
                                     + 0.33 * trend_s), 1)
                reg = None
                try:
                    from engines.feature_collector import compute_regime
                    reg = compute_regime(klines, locals().get("c4"))
                except Exception:
                    reg = None
                return score, reg
            except Exception:
                return None, None

        from decision.scan_evolve import effective_wick_ratio
        ratio = (wick_ratio if wick_ratio is not None
                 else effective_wick_ratio(getattr(self, "_db_path", None)))
        kline_ts = last.get("ts") if isinstance(last, dict) else None
        # last 来自 klines dict 无 ts；用原始 kl 最后一根
        if kline_ts is None and kl:
            kline_ts = kl[-1][0]

        # 做多信号：多头趋势 + 回踩 EMA20 不破 + 拒绝K线（下影线）
        if ema20[-1] > ema50[-1] and last["low"] <= ema20[-1] and last["close"] > ema20[-1]:
            if lower_wick >= body * ratio:  # 拒绝K线（下影线）
                # MTF 共振：4h 必须同向（未知/反向则放弃——抓最佳时机）
                if MTF_ENABLED and tf4h_trend != 1:
                    return None
                score, regime = _shadow(last["low"], lower_wick)
                return {"dir": "long", "entry": entry_ref,
                        "stop": entry_ref - config.STOP_ATR_MULT * atr_val,
                        "tp": entry_ref + config.TP_ATR_MULT * atr_val,
                        "atr": atr_val,
                        "shadow_score": score, "regime": regime,
                        "kline_ts": kline_ts}
        # 做空信号：空头趋势 + 反弹 EMA20 不破 + 拒绝K线（上影线）
        if ema20[-1] < ema50[-1] and last["high"] >= ema20[-1] and last["close"] < ema20[-1]:
            if upper_wick >= body * ratio:
                # MTF 共振：4h 必须同向（未知/反向则放弃——抓最佳时机）
                if MTF_ENABLED and tf4h_trend != -1:
                    return None
                score, regime = _shadow(last["high"], upper_wick)
                return {"dir": "short", "entry": entry_ref,
                        "stop": entry_ref + config.STOP_ATR_MULT * atr_val,
                        "tp": entry_ref - config.TP_ATR_MULT * atr_val,
                        "atr": atr_val,
                        "shadow_score": score, "regime": regime,
                        "kline_ts": kline_ts}
        return None

    # ---------- 主循环 ----------
    def _trade_budget(self, base):
        """该币今日允许笔数：按当日扫描评分动态给（用户要求：看币动态调整笔数）。"""
        from engines.daily_scan import trades_budget
        return trades_budget(self.watch_scores.get(base))

    def scan_signals(self):
        """扫一轮候选池信号（每 15 分钟，日内短线）。
        频率约束（用户要求：看币动态调整笔数）：每个币每天的允许笔数按其当日
        评分动态给（评分越高越值得多给机会）+ 同币信号冷却 SIGNAL_COOLDOWN_MINUTES。"""
        # 每日刷新候选池（跨天自动重扫全市场）——screen_daily 耗时 1-2 分钟,
        # 期间心跳停更会被 watchdog 误杀:先刷一次心跳再进阻塞段(2026-08-16 事故)。
        if time.time() - self._last_watch_refresh >= 24 * 3600 or \
                time.strftime("%Y-%m-%d") != getattr(self, "_watch_date", ""):
            try:
                from execution.pidfile import write_heartbeat
                write_heartbeat("directional")
                from engines.daily_scan import screen_daily
                # 2026-08-17: 全市场筛选同样逐币插拍监控/心跳/tick——网络慢时
                # 60 币筛选会阻塞主循环数十分钟(今晚 23:08 复现: tick 卡 5 分钟
                # 停更,H9 报警),与 51 分钟盲窗同源。回调 = 每币一次。
                w = screen_daily(progress_cb=self._long_scan_progress,
                                 exchange=self.exchange, db_path=self._db_path)
                if w:
                    self.watchlist = [c["base"] for c in w]
                    self.watch_scores = {c["base"]: c["score"] for c in w}
                    self._watch_date = time.strftime("%Y-%m-%d")
                    self._last_watch_refresh = time.time()
                    self._notify(
                        "🔍 每日候选池刷新\n"
                        + " · ".join(self.watchlist)
                        + f"\n共 {len(self.watchlist)} 个")
            except Exception as e:
                print(f"候选池刷新失败，沿用旧池: {e}")
        # 2026-08-16 采集加速（用户指示）：扫描池 = 当日候选池 ∪ 回退主流池
        # （10 个主流币始终参与信号扫描,额度/冷却约束照常适用）
        scan_pool = list(dict.fromkeys(
            self.watchlist + [s for s in SYMBOLS if s not in self.watchlist]))
        # 2026-08-20: 黑名单币不进信号扫描(旧 watchlist 残留或回退池误入时
        # 省掉 K 线请求;名额过滤在 daily_scan,这里是第二道)。
        from engines.daily_scan import untradable_bases
        _blocked = untradable_bases(self._db_path)
        if _blocked:
            scan_pool = [b for b in scan_pool if b not in _blocked]
        n_from_watch = sum(1 for b in scan_pool if b in self.watchlist)
        print(f"\n=== 方向性信号扫描 [{time.strftime('%H:%M:%S')}] "
              f"候选池 {n_from_watch} 个 + 回退池 {len(scan_pool) - n_from_watch} 个 ===")
        if config.SCAN_EVOLVE_ENABLED:
            try:
                from decision.scan_evolve import tick as scan_evolve_tick
                scan_evolve_tick(self)
            except Exception as e:
                print(f"扫描进化步进异常(不影响扫描): {e}")
        today = time.strftime("%Y-%m-%d")
        # 2026-08-20 交易所故障退避: 下单遇 50001/503 后暂停开仓 N 秒,
        # 避免故障期间每轮扫描都刷失败行/告警(OKX 沙盘全灭案例)。
        # 逐币实时读(不能轮前快照——同轮首个币失败后,后续币还会再试)。
        for base in scan_pool:
            if time.time() < getattr(self, "_open_backoff_until", 0):
                self._log_scan_decision(base, False, "", "exchange_backoff",
                                        "交易所下单 API 故障,退避中")
                continue
            # 2026-08-16: 长扫描期间每币刷新心跳——18 币扫描需数分钟,
            # 心跳停更 >30s 会被 watchdog 误杀（exit -15 崩溃循环事故）。
            # 2026-08-17 事故: 网络黑洞让 20 币扫描 × 30s 超时阻塞主循环 51 分钟,
            # 期间 tick() 无法执行 → 止损监控失明。逐币插拍(监控+心跳+tick+
            # 60s 仓位快照): 盲窗≤单币网络超时,慢速但有进展的扫描不算卡死。
            self._long_scan_progress()
            # 0. 动态笔数：该币今天已开几笔？按当日评分给额度（看币动态调整）
            opened_base = [t for t in self.journal.trades
                           if t.get("symbol") == base and t.get("entry_time")
                           and time.strftime("%Y-%m-%d", time.localtime(t["entry_time"])) == today]
            budget = self._trade_budget(base)
            if len(opened_base) >= budget:
                print(f"⏸️ {base}: 今日已开 {len(opened_base)} 笔 ≥ 额度 {budget}（评分给额），跳过")
                self._log_scan_decision(base, False, "", "budget",
                                        f"今日已开 {len(opened_base)} 笔 ≥ 额度 {budget}")
                continue
            # 1. 同币信号冷却（3 小时，1h 线 3 根K线）
            if time.time() - self.signal_cool.get(base, 0) < SIGNAL_COOLDOWN_MINUTES * 60:
                self._log_scan_decision(base, False, "", "cooldown", "信号冷却中")
                continue
            sig = self.scan_signal(base)
            if sig:
                self.signal_cool[base] = time.time()
                # 阈值决策（审计 CR-6 + Phase3 T3.1）：默认用常量 SIGNAL_SCORE 卡门槛
                # （影子分未过假设 A3 检验前不得影响决策——防过拟合红线）；
                # FLAG_USE_SHADOW_SCORE_GATE=True 在 A3 通过后由人工开启。
                gate_score = SIGNAL_SCORE
                if FLAG_USE_SHADOW_SCORE_GATE:
                    gate_score = sig.get("shadow_score")
                    if gate_score is None:
                        gate_score = SIGNAL_SCORE
                # 2026-08-23 用户指示"实盘阈值上调到40": 实盘按真实信号分
                # (shadow_score 0-100)卡 effective_threshold(≥40),只做强信号;
                # 模拟盘保持激进,原逻辑不变(SIGNAL_SCORE 平级卡学习器阈值)。
                if getattr(self, "live_mode", False):
                    gate_score = sig.get("shadow_score") or SIGNAL_SCORE
                _thr = self.effective_threshold()
                if gate_score < _thr:
                    print(f"{base}: 信号分 {gate_score} < 决策阈值 {_thr}，观望")
                    self._log_scan_decision(base, True, sig["dir"], "reject",
                                            f"信号分 {gate_score} < 阈值 {_thr}")
                    continue
                # 决策（经验库，统一 ScoredExperience — B6）
                dec = self.evolver.decide(base, SIGNAL_SCORE, "回踩确认", 0, 0, 0.02, 0.05, 0,
                                          journal=self.journal,
                                          conditions=_build_trade_conditions(sig))
                if dec["trade"]:
                    # 2026-08-20: 先下单,成交入账后才记 open。此前先记 open 再
                    # 调 open_position,下单失败(51001 等)会虚增"开仓"——看账
                    # 开仓 159 vs 台账 24 笔(ALLO 当天即复现)。
                    reason = "; ".join(dec.get("reason") or ["信号达标"])
                    tid = self.open_position(
                        base, sig,
                        score=sig.get("shadow_score") or SIGNAL_SCORE,
                        stop_adj=dec.get("stop_adj", 0.0),
                        size_factor=dec.get("size_factor", 1.0),
                        adopted_ids=dec.get("adopted_lesson_ids", []))
                    if tid:
                        self._log_scan_decision(base, True, sig["dir"], "open",
                                                reason)
                    # 未成交: open_position 已记 reject_* / open_failed,此处不补 open
                else:
                    print(f"{base}: 有信号但拒绝 - {'; '.join(dec['reason'])}")
                    self._log_scan_decision(base, True, sig["dir"], "reject",
                                            "; ".join(dec["reason"]))
            else:
                print(f"{base}: 无回踩确认信号")
                self._log_scan_decision(base, False, "", "no_signal", "")
                self._maybe_wick_shadow(base)
            # Phase 4 T3.3 策略 B 影子（突破/动量确认）: 只记录假设性交易、
            # 绝不下单/不发飞书/不占额度——与策略 A 真实样本分表对照。
            if config.STRATEGY_B_SHADOW_ENABLED:
                try:
                    from engines.strategy_b import breakout_signal, record_shadow
                    kl_b = self._fetch_klines_any(base, "1H", 130)
                    if kl_b:
                        sig_b = breakout_signal(kl_b)
                        if sig_b:
                            if record_shadow(base, "B_breakout", sig_b,
                                             db_path=self._db_path,
                                             klines_1h=kl_b):
                                print(f"  👻 影子信号 B_breakout {base} "
                                      f"{sig_b['dir']} @ {sig_b['entry']:.4f} "
                                      f"(score {sig_b['shadow_score']})")
                        # 2026-08-17 用户建议: 未触发信号也要复盘"为什么没触发"。
                        # 复用本轮已取的 kl_b 算四环节画像(趋势/触线/影线/量能),
                        # 零额外 API 调用;瓶颈与近失证据进 signal_profiles。
                        if sig is None:
                            from engines.strategy_b import profile_from_klines, \
                                record_profile
                            prof = profile_from_klines(kl_b, db_path=self._db_path)
                            if prof:
                                record_profile(base, prof,
                                               db_path=self._db_path)
                except Exception:
                    pass

    def _maybe_wick_shadow(self, base):
        """现役没信号时用候选影线比再扫一次；命中只记影子，绝不下单/不占冷却。"""
        if not config.SCAN_EVOLVE_ENABLED:
            return
        try:
            from decision.scan_evolve import active_candidate
            from engines.strategy_b import record_shadow
            cand = active_candidate(self._db_path)
            if not cand:
                return
            sig = self.scan_signal(base, wick_ratio=cand["wick"])
            if not sig:
                return
            if record_shadow(base, config.SCAN_EVOLVE_STRATEGY, sig,
                             db_path=self._db_path):
                print(f"  👻 扫描影子 A_wick {base} {sig['dir']} "
                      f"@ {sig['entry']:.4f}（候选影线比 {cand['wick']}，不下单）")
        except Exception:
            pass

    def _is_auto_untradable(self, base):
        """查动态黑名单(untradable_symbols 表)——下单失败 51001/51087 自动登记,
        避免同符号每轮扫描反复下单失败。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            row = sdb.q1("SELECT 1 FROM untradable_symbols WHERE base=?",
                         [base], db_path=self._db_path)
            return bool(row)
        except Exception:
            return False

    def _long_scan_progress(self):
        """长扫描逐币进度回调(2026-08-17): 插拍止损监控 + 心跳/tick 进度 +
        60s 仓位快照——screen_daily/scan_signals 的长循环不再造成监控盲窗,
        watchdog tick 判死也看到真实进度(单调用死锁仍会被抓)。"""
        try:
            from execution.pidfile import write_heartbeat, write_tick
            write_heartbeat("directional")
            write_tick("directional")
            self.monitor()
            now = time.time()
            if now - getattr(self, "_last_snap_progress", 0) >= 60:
                self._last_snap_progress = now
                import storage.db as sdb
                sdb.init_db(self._db_path)
                with sdb.tx(db_path=self._db_path) as conn:
                    for p in self.exchange.fetch_positions():
                        conn.execute(
                            "INSERT INTO position_snapshots (ts,inst_id,side,"
                            "contracts,base_qty,avg_px) VALUES (?,?,?,?,?,?)",
                            [time.time(), p.inst_id, p.side, p.contracts,
                             round(p.base_qty, 8), p.avg_px])
        except Exception:
            pass

    def _log_scan_decision(self, base, has_signal, direction, decision, reason=""):
        """信号决策过程落库（self-evolution 看账数据）：每币每轮扫都记一条。
        Phase0 T0.4：落库目标 = self._db_path（生产 None=共享库；测试传隔离路径，
        防测试进程写生产表——DEF-8 溯源：test_decision_loop 曾把 阈值85 行写进生产库）。"""
        try:
            import storage.db as sdb
            sdb.init_db(self._db_path)
            sdb.x("INSERT INTO scan_decisions (ts, base, venue, has_signal, direction, "
                  "threshold, decision, reason) VALUES (?,?,?,?,?,?,?,?)",
                  [time.time(), base, self.exchange.venue_for(base), 1 if has_signal else 0,
                   direction or "", self.effective_threshold(), decision, reason],
                  db_path=self._db_path)
        except Exception:
            pass

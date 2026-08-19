"""
风控监控层（RiskMonitorMixin）— 2026-08-20 从 directional_trader 按功能拆分。

职责：tick 级止损止盈监控（WS 价格优先 + REST 兜底 + 51169 双语义判定）、
熔断强平（复盘链闭环）、持仓消失反查、51169 节流落账、风控事件落库。
方法体与拆分前逐行一致（行为零变化）；宿主为 DirectionalTrader。
安全不变量：监控永不暂停;平仓路径必须配对撤条件单 + 释放账本认领。
"""
import time


class RiskMonitorMixin:
    """止损监控/熔断强平功能块。"""

    # ---------- 监控：止损止盈（tick 级，WebSocket 价格 + REST 兜底） ----------
    def monitor(self):
        """检查持仓，触发止损/止盈则平仓 + 复盘。每 1 秒调用一次。
        2026-08-17 提速: WS 价格检查每拍执行(秒级);交易所持仓 REST 快照
        每 2 秒刷新一次(避免 1s 节拍的 REST 频率过高);快照未刷新的拍次
        只做价格判定,平仓执行等下一拍持仓就绪(≤1s 延迟)。"""
        now = time.time()
        if now - getattr(self, "_last_pos_fetch", 0) >= 2.0:
            self._last_pos_fetch = now
            try:
                positions = self.exchange.fetch_positions()
            except Exception:
                positions = None
        else:
            positions = None
        open_trades = [t for t in self.journal.trades if t["status"] == "open"]
        if not open_trades:
            return
        for t in open_trades:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            inst_id = self._inst_id(base, venue)
            sym_ledger = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            # 价格：WebSocket 实时优先（stale>60s 剔除），REST 兜底
            price = None
            if self.rt is not None:
                price = self.rt.get(base, max_age=60).get("price")
            if not price:
                price = self._ticker_last(base, prefer_swap=(venue == "swap"))
                if price is None:
                    continue
            # 检查止损/止盈（按方向：空头 stop 在入场上方、tp 在下方）
            hit_exit = False
            direction = t.get("direction") or "long"
            if direction == "short":
                hit_exit = (t["stop_loss"] and price >= t["stop_loss"]) or \
                           (t["take_profit"] and price <= t["take_profit"])
            else:
                hit_exit = (t["stop_loss"] and price <= t["stop_loss"]) or \
                           (t["take_profit"] and price >= t["take_profit"])
            if hit_exit:
                # ===== 现货路径（美股代币）：按余额持有量卖出现货平仓 =====
                if venue == "spot":
                    try:
                        held = self.exchange.spot_holding(base)
                        if held >= float(t["size"]) * 0.99:
                            res = self.exchange.place_market_order(
                                inst_id, "sell", abs(float(t["size"])), venue="spot")
                            if not res.ok:
                                print(f"  现货平仓失败: {res.message}")
                                self._log_order_failure(base, inst_id, "sell",
                                                        abs(float(t["size"])),
                                                        "close", res.message)
                                continue
                        else:
                            # 现货已被外部卖光 → 按当前价记账平仓
                            print(f"  {base} 现货已不在账户（外部平仓），按现价记账")
                        # 账本认领释放与台账闭环同层(2026-08-17 同合约路径修复:
                        # 外部已平仓时跳过释放会造成 H2 对账失败)
                        self.ledger.release(sym_ledger, "long", "dir",
                                            float(t["size"]),
                                            float(t["size"]) * float(t.get("entry_price") or 0))
                    except Exception as e:
                        print(f"  现货平仓失败: {e}")
                        # 2026-08-20 拆分时修复: 此处原引用未定义的 qty(NameError
                        # 潜伏 bug,现货平仓抛异常时会二次报错吞掉真实原因)
                        self._log_order_failure(base, inst_id, "sell",
                                                abs(float(t["size"])), "close", e)
                        continue
                    closed = self.journal.log_exit(t["id"], price, "止损/止盈")
                    if closed:
                        self._post_close_review(closed, t)
                    continue
                # ===== 合约路径（原有） =====
                # 平仓（R1-12 最小止血：按本策略 journal 数量平 + reduceOnly，
                # 不再按交易所合并持仓全额平——防止误平同 symbol 同 posSide 的套利腿）
                if positions is None:
                    continue   # 持仓快照未刷新拍: 下一拍(≤1s)执行平仓
                pos = next((p for p in positions
                            if p.inst_id == inst_id and p.base_qty > 0
                            and p.side == (t.get("direction") or "long")), None)
                if pos:
                    try:
                        side = "sell" if pos.side == "long" else "buy"
                        close_qty = min(abs(float(t["size"])), pos.base_qty)
                        res = self.exchange.place_market_order(
                            inst_id, side, close_qty, venue="swap",
                            pos_side=pos.side, reduce_only=True)
                        if not res.ok:
                            # 2026-08-19 51169 双语义: ①条件单已抢先平仓(仓位
                            # 消失) ②下单层问题如 tdMode 不匹配(仓位还在)。
                            # 必须反查持仓确认——盲目落账留幽灵仓,盲目重试刷
                            # 失败行(ETH 案例: 7 连败 51169 即 tdMode 不匹配)。
                            if "51169" in str(res.message):
                                if self._pos_gone(inst_id, pos.side):
                                    print(f"{base}: 交易所已无持仓(51169 确认),"
                                          f"按已平仓闭环")
                                else:
                                    self._log_51169_throttled(base, inst_id,
                                                              res.message)
                                    continue
                            else:
                                print(f"平仓失败: {res.message}")
                                self._log_order_failure(base, inst_id, side, close_qty, "close", res.message)
                                continue
                        else:
                            # R1-1：平仓成功后取消交易所侧条件停损单（防幽灵单残留）
                            self._cancel_stop_orders(base, "止损/止盈平仓")
                    except Exception as e:
                        if "51169" in str(e):
                            if self._pos_gone(inst_id, (t.get("direction") or "long")):
                                print(f"{base}: 交易所已无持仓(51169 异常路径确认),"
                                      f"按已平仓闭环")
                            else:
                                self._log_51169_throttled(base, inst_id, e)
                                continue
                        else:
                            print(f"平仓失败: {e}")
                            self._log_order_failure(base, inst_id, "close", 0, "close", e)
                            continue
                # R1-12：释放账本认领（2026-08-17 修复: 此前只在 `if pos` 分支内
                # 释放,交易所侧条件单已平仓时 pos=None → 跳过分支 → 认领永存 →
                # H2 对账失败(ADA/LTC 双双中招)。台账闭环就必须释放,与谁执行
                # 平仓无关——引擎平仓成功 or 交易所条件单已平,都要释放)。
                try:
                    self.ledger.release(sym_ledger, t.get("direction") or "long", "dir",
                                        float(t["size"]),
                                        float(t["size"]) * float(t.get("entry_price") or 0))
                except Exception:
                    pass
                closed = self.journal.log_exit(t["id"], price, "止损/止盈")
                if closed:
                    self._post_close_review(closed, t)

    # ---------- 熔断强平（只平本策略的 journal 持仓，不动套利对冲仓） ----------
    def _liquidate_all(self, reason):
        """风控熔断时强制平掉本策略所有持仓。"""
        for t in [x for x in self.journal.trades if x["status"] == "open"]:
            base = t["symbol"]
            venue = t.get("venue") or "swap"
            inst_id = self._inst_id(base, venue)
            sym_ledger = f"{base}/USDT" if venue == "spot" else f"{base}/USDT:USDT"
            # ===== 现货路径（美股代币）：卖出现货强平 =====
            if venue == "spot":
                try:
                    held = self.exchange.spot_holding(base)
                    if held >= float(t["size"]) * 0.99:
                        res = self.exchange.place_market_order(
                            inst_id, "sell", abs(float(t["size"])), venue="spot")
                        if not res.ok:
                            print(f"现货强平失败 {base}: {res.message}（保持 open，下轮重试）")
                            continue
                        self.ledger.release(sym_ledger, "long", "dir",
                                            float(t["size"]),
                                            float(t["size"]) * float(t.get("entry_price") or 0))
                    px = self._ticker_last(base)
                    if px is None:
                        print(f"强平失败 {base}: 无法获取价格")
                        continue
                    closed = self.journal.log_exit(t["id"], px, f"熔断强平: {reason}")
                    # Phase0 T0.1：熔断强平同样走复盘链（DEF-1——此前唯一平仓
                    # 样本零复盘）。review 落盘 + 教训初筛 + 采纳验证 + 阈值。
                    if closed:
                        self._post_close_review(closed, t)
                except Exception as e:
                    print(f"强平失败 {base}: {e}")
                continue
            # ===== 合约路径（原有） =====
            try:
                positions = self.exchange.fetch_positions()
                pos = next((p for p in positions
                            if p.inst_id == inst_id and p.base_qty > 0
                            and p.side == (t.get("direction") or "long")), None)
                if pos:
                    side = "sell" if pos.side == "long" else "buy"
                    # R1-12 最小止血：按 t["size"] + reduceOnly，只减本策略部分
                    close_qty = min(abs(float(t["size"])), pos.base_qty)
                    res = self.exchange.place_market_order(
                        inst_id, side, close_qty, venue="swap",
                        pos_side=pos.side, reduce_only=True)
                    if not res.ok:
                        # 审计 H4:平仓失败不得落账"已平"——保持 open,下一轮重试
                        print(f"强平失败 {base}: {res.message}（保持 open，下轮重试）")
                        continue
                    # R1-1：强平后取消交易所侧条件停损单
                    self._cancel_stop_orders(base, "熔断强平")
                else:
                    # 交易所已无该持仓(可能被条件单平掉):闭环台账即可
                    print(f"{base}: 交易所已无持仓(可能已由条件单平仓)，仅闭环台账")
                # 账本释放：无论交易所持仓是否存在都必须执行（journal 是本策略事实源；
                # 此前放在 if pos 分支内，持仓已归零时跳过 → 账本残留，见 pitfalls）
                try:
                    self.ledger.release(sym_ledger, t.get("direction") or "long", "dir",
                                        float(t["size"]),
                                        float(t["size"]) * float(t.get("entry_price") or 0))
                except Exception:
                    pass
                px = self._ticker_last(base, prefer_swap=True)
                if px is None:
                    print(f"强平失败 {base}: 无法获取价格")
                    continue
                closed = self.journal.log_exit(t["id"], px, f"熔断强平: {reason}")
                # Phase0 T0.1：熔断强平同样走复盘链（DEF-1）。review 落盘 +
                # 教训初筛 + 采纳验证 + 阈值。
                if closed:
                    self._post_close_review(closed, t)
            except Exception as e:
                print(f"强平失败 {base}: {e}")

    def _pos_gone(self, inst_id, side):
        """反查交易所持仓是否已消失(51169 双语义判定用)。
        查询失败按'还在'处理——宁可下拍重试,不留幽灵仓。"""
        try:
            positions = self.exchange.fetch_positions()
            return not any(p.inst_id == inst_id and p.side == side
                           and p.base_qty > 0 for p in positions)
        except Exception:
            return False

    def _log_51169_throttled(self, base, inst_id, msg):
        """51169 且仓位仍在(下单层问题): 60 秒节流记录一条失败,
        避免每拍一行灌爆台账(ETH 7 连败即此场景)。"""
        now = time.time()
        log = getattr(self, "_51169_log", None)
        if log is None:
            log = {}
            self._51169_log = log
        key = f"{base}:{inst_id}"
        if now - log.get(key, 0) >= 60:
            log[key] = now
            self._log_order_failure(base, inst_id, "close", 0, "close", msg)

    def _log_risk_event(self, kind, reason, eq):
        """风控事件落库（复盘用）：halt/recovery 各记一条，含净值快照。"""
        try:
            import storage.db as sdb
            sdb.init_db()
            open_n = sum(1 for t in self.journal.trades if t.get("status") == "open")
            sdb.x("INSERT INTO risk_events (ts, kind, reason, equity, open_trades) "
                  "VALUES (?,?,?,?,?)",
                  [time.time(), kind, reason, round(eq, 2), open_n])
        except Exception:
            pass

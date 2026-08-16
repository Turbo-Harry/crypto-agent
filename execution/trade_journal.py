"""
自驱动进化交易系统 — 交易日志 + 自动复盘 + 经验库。

核心思想（用户提出）：
  信号层（策略/模型）只是触发交易的"信号"，不是命令。
  真正的决策 + 进化靠"复盘"：每笔交易结束后反思"为什么下单、
  止损位对不对、为什么没及时止损"，把教训存入经验库，驱动下次更好的决策。

四层：信号层 → 决策层 → 执行层 → 复盘层（→经验库→反馈决策层）

用法：
  journal.log_entry(...)   开仓记录（含信号、理由、参数）
  journal.log_exit(...)    平仓记录（含结果）
  journal.review(...)      自动复盘，生成教训
  journal.get_lessons()    读经验库（决策时参考）
"""
import json
import os
import time


class TradeJournal:
    def __init__(self, path="trade_journal.json"):
        self.path = path
        self.trades = []
        self.lessons = []
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                d = json.load(f)
                self.trades = d.get("trades", [])
                self.lessons = d.get("lessons", [])
                self._backfill_notional()

    def _backfill_notional(self):
        """旧记录缺投注额字段时按 size×price 回填（只补一次，落盘）。"""
        dirty = False
        for t in self.trades:
            if "notional_usdt" not in t:
                size = float(t.get("size") or 0)
                entry = float(t.get("entry_price") or 0)
                stop = float(t.get("stop_loss") or entry)
                t["notional_usdt"] = round(size * entry, 2)
                t["risk_usdt"] = round(abs(entry - stop) * size, 2)
                dirty = True
        if dirty:
            self._save()

    def _save(self):
        with open(self.path, "w") as f:
            json.dump({"trades": self.trades, "lessons": self.lessons},
                      f, ensure_ascii=False, indent=2)

    # ---------- 开仓记录 ----------
    def log_entry(self, symbol, signal, reason, entry_price, stop_loss,
                  take_profit, size, entry_time=None, direction="long", score=None,
                  adopted_lesson_ids=None, atr_value=None, signal_price=None,
                  venue="swap"):
        """记录开仓：信号是什么、为什么下单、参数如何。
        direction: "long"/"short"，用于正确计算空头盈亏。
        score: 本次决策的综合分（供阈值自适应 record 使用）。"""
        trade = {
            "id": f"txn_{len(self.trades)+1:03d}",
            "symbol": symbol,
            "signal": signal,          # 信号描述（哪个策略/模型触发）
            "reason": reason,          # 为什么下单（决策理由）
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "size": size,
            "direction": direction,
            "venue": venue,   # "swap"（合约）/ "spot"（现货，美股代币等无合约标的）
            "score": score,
            "adopted_lesson_ids": adopted_lesson_ids or [],   # R2-3：本笔实际采纳的经验
            "atr_value": atr_value,                            # R2-6：复盘用 ATR
            "signal_price": signal_price,                      # R2-6：复盘用信号价
            # 投注记录（显式落盘，不靠 size×price 反推——API/复盘/统计直接读）
            "notional_usdt": round(size * entry_price, 2),     # 名义投注额（USDT）
            "risk_usdt": round(abs(entry_price - stop_loss) * size, 2),  # 止损风险额（USDT）
            "entry_time": entry_time or time.time(),
            "status": "open",
            "exit_price": None,
            "pnl": None,
            "review": None,
        }
        self.trades.append(trade)
        self._save()
        return trade["id"]

    # ---------- 平仓记录 ----------
    def log_exit(self, trade_id, exit_price, exit_reason):
        """记录平仓：结果如何。空头盈亏 = (入场-出场)/入场。"""
        for t in self.trades:
            if t["id"] == trade_id and t["status"] == "open":
                t["status"] = "closed"
                t["exit_price"] = exit_price
                t["exit_reason"] = exit_reason
                if t.get("direction") == "short":
                    t["pnl"] = (t["entry_price"] - exit_price) / t["entry_price"]
                else:
                    t["pnl"] = (exit_price - t["entry_price"]) / t["entry_price"]
                self._save()
                return t
        return None

    # ---------- 自动复盘 ----------
    def review(self, trade_id):
        """复盘一笔已平仓的交易，生成教训。"""
        t = next((x for x in self.trades if x["id"] == trade_id), None)
        if not t or t["status"] != "closed":
            return None

        lessons = []
        entry, exit_px, stop = t["entry_price"], t["exit_price"], t["stop_loss"]
        pnl = t["pnl"]

        # 1. 信号是否有效（入场后朝预期方向走了吗）
        #    简化判断：盈利 = 信号有效，亏损 = 信号无效或止损问题
        # 2. 止损位是否合理
        stop_distance = (entry - stop) / entry  # 止损距离
        if pnl < 0 and abs(pnl) > stop_distance * 1.5:
            lessons.append(f"止损未保护：实际亏损 {pnl*100:.1f}% > 预设止损 {stop_distance*100:.1f}%（跳空/滑点），需在止损位加缓冲")
        elif pnl < 0 and abs(pnl) < stop_distance * 0.5:
            lessons.append(f"止损可能太宽：实际只亏 {pnl*100:.1f}%，但止损设了 {stop_distance*100:.1f}%，可收紧")
        # 3. 盈亏比
        tp_distance = (t["take_profit"] - entry) / entry
        rr = tp_distance / stop_distance if stop_distance > 0 else 0
        if rr < 2.0:
            lessons.append(f"盈亏比不足：{rr:.1f}（目标应 ≥2），入场前就该放弃")

        # 4. 结果复盘
        if pnl > 0:
            lessons.append(f"盈利交易 {pnl*100:.1f}%：信号有效，复盘入场时机是否可更早")
        else:
            lessons.append(f"亏损交易 {pnl*100:.1f}%：检查信号质量、止损位、是否追高")

        t["review"] = lessons
        self.lessons.extend({"trade_id": trade_id, "lesson": l, "ts": time.time()}
                            for l in lessons)
        self._save()
        return lessons

    # ---------- 读经验库 ----------
    def get_lessons(self, symbol=None, limit=10):
        """读经验库（决策时参考）。可过滤某币种。"""
        if symbol:
            return [l for l in self.lessons[-limit:] if symbol in l.get("trade_id", "")]
        return self.lessons[-limit:]

    # ---------- 决策辅助 ----------
    def should_trade(self, signal_score):
        """决策辅助：结合历史教训，判断是否值得交易。
        信号分数 + 经验库中类似失败的惩罚 → 最终决策。"""
        # 简化：如果最近有连亏，降低仓位或拒绝
        recent = [t for t in self.trades if t["status"] == "closed"][-5:]
        losses = [t for t in recent if t["pnl"] is not None and t["pnl"] < 0]
        if len(losses) >= 3:
            return {"trade": False, "reason": f"最近 {len(losses)} 连亏，建议冷却"}
        if signal_score < 75:
            return {"trade": False, "reason": f"信号分 {signal_score} < 75，不够"}
        # 有连亏历史时，减半仓位
        if len(losses) == 2:
            return {"trade": True, "size_factor": 0.5,
                    "reason": "信号达标，但近期有连亏，建议半仓"}
        return {"trade": True, "size_factor": 1.0, "reason": "信号达标，可正常仓位"}


if __name__ == "__main__":
    # 演示完整的"交易 → 复盘 → 进化"循环
    j = TradeJournal()
    # 模拟一笔交易
    tid = j.log_entry(
        symbol="BTC/USDT:USDT",
        signal="回踩确认（score 78）",
        reason="BTC 4H 多头，1H 回踩 EMA20 出现拒绝K线",
        entry_price=63000, stop_loss=61500, take_profit=66000, size=100)
    j.log_exit(tid, 60500, "止损")
    lessons = j.review(tid)
    print(f"交易 {tid} 复盘教训:")
    for l in lessons:
        print(f"  • {l}")
    # 决策辅助
    print("\n下次决策:", j.should_trade(78))

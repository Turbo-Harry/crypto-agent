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
import config
import os
import secrets
import time

from storage.db import _TRADE_COLS

# 旧记录单位回填用的合约面值表（legacy size 是"张"时换算币数；新代码不再依赖此表）
LEGACY_CT_VAL = config.LEGACY_CT_VAL


class TradeJournal:
    def __init__(self, path="trade_journal.json", db_path=None):
        # path 兼容保留：默认值走共享库 crypto_agent.db；显式传路径（测试隔离）时
        # 该路径即 SQLite 文件。旧 JSON 由 storage.init_db 一次性迁移。
        self.path = path
        self.db_path = db_path or (None if path == "trade_journal.json" else path)
        self.trades = []
        self.lessons = []
        self._load()

    def _load(self):
        import storage.db as sdb
        # 兼容：显式路径下若存在旧 JSON 文件（非 SQLite），先当 JSON 读入，
        # 转库后移除原 JSON（迁移语义：JSON 被消费进 DB）。
        legacy_json = None
        if self.db_path and os.path.exists(self.db_path):
            try:
                with open(self.db_path) as f:
                    head = f.read(1)
                if head == "{":
                    with open(self.db_path) as f:
                        legacy_json = json.load(f)
            except Exception:
                pass
        if legacy_json is not None:
            self.trades = legacy_json.get("trades", [])
            self.lessons = legacy_json.get("lessons", [])
            os.remove(self.db_path)          # 清掉 JSON，避免被当成 SQLite 打开
            sdb.init_db(self.db_path)
            self._backfill_notional()
            self._save()
            return
        sdb.init_db(self.db_path)
        rows = sdb.q("SELECT * FROM trades ORDER BY entry_time", db_path=self.db_path)
        self.trades = [self._row_to_trade(r) for r in rows]
        kv = sdb.q1("SELECT value FROM kv WHERE key='legacy_journal_lessons'",
                    db_path=self.db_path)
        self.lessons = json.loads(kv["value"]) if kv else []
        self._backfill_notional()

    @staticmethod
    def _row_to_trade(r):
        t = dict(r)
        for k in ("adopted_lesson_ids", "review"):
            v = t.get(k)
            if isinstance(v, str):
                try:
                    t[k] = json.loads(v)
                except Exception:
                    pass
        return t

    @staticmethod
    def _sql_val(v):
        """list/dict 落库走 JSON 字符串，其余原样（含 None→NULL）。"""
        return json.dumps(v) if isinstance(v, (list, dict)) else v

    @staticmethod
    def _new_trade_id():
        """时间戳 + 4 位随机 hex，避免多进程/重启后按内存长度撞号。
        旧数据 txn_001 这类序号 ID 继续共存，本方法不改写历史行。"""
        return f"txn_{int(time.time())}_{secrets.token_hex(2)}"

    def _insert_trade(self, t):
        """新开仓：纯 INSERT。主键冲突抛错，绝不覆盖已有行。"""
        import storage.db as sdb
        cols = [k for k in t if k in _TRADE_COLS]
        sdb.x(f"INSERT INTO trades ({','.join(cols)}) "
              f"VALUES ({','.join('?' * len(cols))})",
              [self._sql_val(t[k]) for k in cols], db_path=self.db_path)

    def _update_trade(self, trade_id, fields):
        """按 id 增量 UPDATE 指定列（值仍走 _sql_val 序列化）。"""
        import storage.db as sdb
        cols = [k for k in fields if k in _TRADE_COLS]
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        sdb.x(f"UPDATE trades SET {sets} WHERE id=?",
              [self._sql_val(fields[k]) for k in cols] + [trade_id],
              db_path=self.db_path)

    def _save(self):
        """全量快照写库（INSERT OR REPLACE 逐笔）。

        只允许两个场景调用：
        1. `_load` 的旧 JSON → SQLite 一次性迁移
        2. `_backfill_notional` 的 legacy 字段一次性回填
        日常 log_entry / log_exit / save_review / review 必须走增量
        INSERT/UPDATE，禁止走本方法（全量重写既慢，又会在撞号时覆盖）。
        兼容：position_mgmt 在 TP 挂失败打标时仍调用（tp_missing 非表列，
        REPLACE 不持久化该标记；本轮调用方零改动，故保留）。
        """
        import storage.db as sdb
        for t in self.trades:
            cols = [k for k in t if k in _TRADE_COLS]
            sdb.x(f"INSERT OR REPLACE INTO trades ({','.join(cols)}) "
                  f"VALUES ({','.join('?' * len(cols))})",
                  [self._sql_val(t[k]) for k in cols], db_path=self.db_path)

    def _backfill_notional(self):
        """旧记录缺 size_unit/notional 时回填（只补一次，落盘）。

        单位坑（2026-08-16 实盘对账发现）：旧版 journal 的 size 存的是【合约张数】
        而非基础币数（0.53 张 = 0.053 ETH）。用交易所持仓对账印证：3 笔合计 1.22 张
        = 交易所 1.22 张。故 legacy 回填按 张 × ctVal × price 计算名义额，
        并打 size_unit="contracts(legacy)" 标注；新代码从此只写币数。
        """
        dirty = False
        for t in self.trades:
            if "size_unit" not in t:
                t["size_unit"] = "contracts(legacy)"
                dirty = True
            if "notional_usdt" not in t or (t.get("size_unit") == "contracts(legacy)"
                                             and t.get("notional_calc") != "legacy"):
                size = float(t.get("size") or 0)
                entry = float(t.get("entry_price") or 0)
                stop = float(t.get("stop_loss") or entry)
                ct_val = float(t.get("ct_val") or LEGACY_CT_VAL.get(t.get("symbol"), 1.0))
                t["ct_val"] = ct_val
                t["notional_usdt"] = round(size * ct_val * entry, 2)
                t["risk_usdt"] = round(abs(entry - stop) * size * ct_val, 2)
                t["notional_calc"] = "legacy"
                dirty = True
        if dirty:
            self._save()

    # ---------- 开仓记录 ----------
    def log_entry(self, symbol, signal, reason, entry_price, stop_loss,
                  take_profit, size, entry_time=None, direction="long", score=None,
                  adopted_lesson_ids=None, atr_value=None, signal_price=None,
                  venue="swap"):
        """记录开仓：信号是什么、为什么下单、参数如何。
        direction: "long"/"short"，用于正确计算空头盈亏。
        score: 本次决策的综合分（供阈值自适应 record 使用）。"""
        trade = {
            "id": self._new_trade_id(),
            "symbol": symbol,
            "signal": signal,          # 信号描述（哪个策略/模型触发）
            "reason": reason,          # 为什么下单（决策理由）
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "size": size,
            "size_unit": "base",   # 新代码统一存基础币数量（旧记录是 contracts(legacy)）
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
        self._insert_trade(trade)
        self.trades.append(trade)
        return trade["id"]

    # ---------- 平仓记录 ----------
    def log_exit(self, trade_id, exit_price, exit_reason):
        """记录平仓：结果如何。空头盈亏 = (入场-出场)/入场。"""
        for t in self.trades:
            if t["id"] == trade_id and t["status"] == "open":
                t["status"] = "closed"
                t["exit_price"] = exit_price
                t["exit_reason"] = exit_reason
                # Phase 1: 平仓时间落盘（持仓时长/MFE/MAE 特征依赖,此前缺失）
                t["exit_time"] = time.time()
                if t.get("direction") == "short":
                    t["pnl"] = (t["entry_price"] - exit_price) / t["entry_price"]
                else:
                    t["pnl"] = (exit_price - t["entry_price"]) / t["entry_price"]
                self._update_trade(trade_id, {
                    "status": t["status"],
                    "exit_price": t["exit_price"],
                    "exit_reason": t["exit_reason"],
                    "exit_time": t["exit_time"],
                    "pnl": t["pnl"],
                })
                return t
        return None

    # ---------- 复盘报告落盘 ----------
    def save_review(self, trade_id, report):
        """把 deep_review 的结构化复盘报告存进该笔交易记录（review 字段）。
        复盘报告与平仓结果同处一文件——事后可查"这笔交易当时复盘说了什么"。"""
        t = next((x for x in self.trades if x["id"] == trade_id), None)
        if not t:
            return False
        t["review"] = report
        t["review_ts"] = time.time()
        self._update_trade(trade_id, {"review": t["review"], "review_ts": t["review_ts"]})
        return True

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
        # lessons 只在内存追加（历史落在 kv.legacy_journal_lessons，本路径不扩写）
        self._update_trade(trade_id, {"review": t["review"]})
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

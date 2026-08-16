"""
经验评分系统 — 历史经验不一定对，每条经验用实际交易结果验证。
好经验存活（分数上升），坏经验淘汰（分数下降，弃用）。

机制（v2 — 审计 CR-8 修复）：
  经验初始分 50（中性，未验证）
  ↓ 被采纳（决策时参考）
  交易执行，结果出来
  ↓ 验证
  盈利 → +10 分；亏损 → −10 分（对称化，v1 的 +10/−15 使 2胜1负=55 被误杀）
  ↓ 自然选择（40 与 60 真正分离，v1 的 40-60 区间被强行归 discarded）
  分数 ≥60 且被验证 3 次 → trusted（可信，正常参考）
  分数 <40 且被验证 3 次 → discarded（弃用，可能错误）
  40-60 → unverified（继续验证，不强行淘汰）
  ↓ 时间衰减：分数向 50 回归（市场 regime 变化，旧经验权重下降）
  ↓ 复活：discarded 60 天后自动回到 unverified（重新检验）
"""
import json
import os
import time

DECAY_HALFLIFE_DAYS = 30   # 分数向 50 回归的半衰期
REVIVE_DAYS = 60           # discarded 经验 N 天后复活为 unverified


class ScoredExperience:
    """带评分的经验库（v2：对称评分、40/60 分离、时间衰减、discarded 复活）。
    存储：SQLite（storage 层 lessons 表）；path 仅兼容保留（测试隔离用）。"""

    def __init__(self, path="experience_scored.json"):
        self.path = path
        self.db_path = None if path == "experience_scored.json" else path
        self.lessons = []
        self._load()

    def _decay(self, score, last_ts, now=None):
        """分数向 50 回归（半衰期 DECAY_HALFLIFE_DAYS）。市场变了，旧经验权重下降。"""
        now = now or time.time()
        days = max(0.0, (now - (last_ts or now)) / 86400.0)
        k = 0.5 ** (days / DECAY_HALFLIFE_DAYS)
        return 50 + (score - 50) * k

    def _load(self):
        import storage.db as sdb
        sdb.init_db(self.db_path)
        rows = sdb.q("SELECT * FROM lessons ORDER BY id", db_path=self.db_path)
        self.lessons = [dict(r) for r in rows]
        now = time.time()
        for l in self.lessons:
            l.setdefault("ts", now)
            l.setdefault("last_update", l["ts"])
            # 时间衰减
            l["score"] = round(self._decay(l.get("score", 50), l.get("last_update", now), now), 2)
            # discarded 复活：冷却期满回到 unverified，重新检验
            if l.get("status") == "discarded" and \
                    now - l.get("last_update", now) > REVIVE_DAYS * 86400:
                l["status"] = "unverified"
                l["score"] = 50
            # 状态与分数一致性（40/60 分离）
            self._sync_status(l)

    def _sync_status(self, l):
        """状态只由 分数+验证次数 决定：≥3次且≥60=trusted；≥3次且<40=discarded；其余 unverified。"""
        if l.get("adoptions", 0) >= 3:
            if l["score"] >= 60:
                l["status"] = "trusted"
            elif l["score"] < 40:
                l["status"] = "discarded"
            else:
                l["status"] = "unverified"   # 40-60 保持未验证（v1 曾强行 discarded）

    def _save(self):
        import storage.db as sdb
        for l in self.lessons:
            sdb.x("INSERT OR REPLACE INTO lessons (id,symbol,category,content,score,"
                  "adoptions,good,bad,status,source_trade,ts,last_update) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  [l.get("id"), l.get("symbol"), l.get("category"), l.get("content"),
                   l.get("score", 50), l.get("adoptions", 0), l.get("good", 0),
                   l.get("bad", 0), l.get("status", "unverified"), l.get("source_trade"),
                   l.get("ts"), l.get("last_update")], db_path=self.db_path)

    # ---------- 经验生命周期 ----------
    def add(self, symbol, category, content, source_trade):
        """新增经验（初始分 50，未验证）。"""
        now = time.time()
        lesson = {
            "id": len(self.lessons) + 1,
            "symbol": symbol,
            "category": category,
            "content": content,
            "score": 50,
            "adoptions": 0,
            "good": 0,
            "bad": 0,
            "status": "unverified",
            "source_trade": source_trade,
            "ts": now,
            "last_update": now,
        }
        self.lessons.append(lesson)
        self._save()
        return lesson["id"]

    def validate(self, lesson_id, trade_pnl):
        """用一笔交易的结果验证经验。盈利+10，亏损−10（对称化）。
        状态按分数+验证次数重新判定（discarded 也可复活）。"""
        now = time.time()
        for l in self.lessons:
            if l["id"] == lesson_id:
                # 先做时间衰减再加减分
                l["score"] = self._decay(l.get("score", 50), l.get("last_update", now), now)
                l["adoptions"] += 1
                if trade_pnl > 0:
                    l["good"] += 1
                    l["score"] = min(100, round(l["score"] + 10, 2))
                else:
                    l["bad"] += 1
                    l["score"] = max(0, round(l["score"] - 10, 2))
                l["last_update"] = now
                self._sync_status(l)
                self._save()
                return l
        return None

    # ---------- 查询 ----------
    def trusted(self, symbol=None, min_score=60):
        """可信经验（分数达标，正常参考）。"""
        out = [l for l in self.lessons
               if l["status"] == "trusted" and l["score"] >= min_score]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        return out

    def unverified(self, symbol=None):
        """未验证经验（仅提示，不强制）。"""
        out = [l for l in self.lessons if l["status"] == "unverified"]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        return out

    def discarded(self, symbol=None):
        """已弃用经验（证明是错的，不参考）。"""
        out = [l for l in self.lessons if l["status"] == "discarded"]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        return out

    def summary(self):
        return {
            "total": len(self.lessons),
            "trusted": len(self.trusted()),
            "unverified": len(self.unverified()),
            "discarded": len(self.discarded()),
            "avg_score": sum(l["score"] for l in self.lessons) / len(self.lessons) if self.lessons else 0,
        }


def experience_score_for_decision(bank, symbol):
    """
    决策时的经验分（0-100，两面化 — 审计 CR-8）：
    无经验 = 60（中性）；可信经验加分；弃用经验【减分】（有坏教训要警惕）。
    v1 只加分不减分，形成"经验越多分越高"的正反馈回声室。
    """
    trusted = bank.trusted(symbol)
    discarded = bank.discarded(symbol)
    score = 60 + min(30, len(trusted) * 5) - min(20, len(discarded) * 5)
    return max(20, score)


if __name__ == "__main__":
    bank = ScoredExperience("experience_scored_demo.json")
    # 模拟：一条经验从"未验证"到"可信"或"弃用"的过程
    print("=" * 60)
    print("经验评分系统 — 演示（自然选择）")
    print("=" * 60)

    # 经验 A：止损太紧（可能对）
    a = bank.add("BTC", "止损", "止损 1.5×ATR 太紧，放宽到 2.0×ATR", "txn_1")
    # 经验 B：追高不是问题（可能错）
    b = bank.add("BTC", "入场时机", "追高 3% 没关系，趋势会继续", "txn_2")

    print(f"\n经验 A (止损放宽) id={a}, 初始分 50")
    print(f"经验 B (追高无妨) id={b}, 初始分 50")

    # 模拟 3 笔交易验证
    results = [+0.03, +0.02, -0.01]  # A 的验证（2盈1亏 → 60分 trusted，v1 会误杀）
    for r in results:
        l = bank.validate(a, r)
        print(f"  经验 A 验证: 盈亏 {r*100:+.1f}% → 分 {l['score']}, 状态 {l['status']}")

    results_b = [-0.04, -0.03, -0.01]  # B 的验证（3亏 → 20分 discarded）
    for r in results_b:
        l = bank.validate(b, r)
        print(f"  经验 B 验证: 盈亏 {r*100:+.1f}% → 分 {l['score']}, 状态 {l['status']}")

    print(f"\n最终: {bank.summary()}")
    print(f"\n决策时参考:")
    print(f"  可信经验: {[l['content'][:20] for l in bank.trusted('BTC')]}")
    print(f"  弃用经验: {[l['content'][:20] for l in bank.discarded('BTC')]}")
    print(f"  经验分: {experience_score_for_decision(bank, 'BTC')}")
    os.remove("experience_scored_demo.json")

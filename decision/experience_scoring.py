import config
DECAY_HALFLIFE_DAYS = config.DECAY_HALFLIFE_DAYS
REVIVE_DAYS = config.REVIVE_DAYS


def _refresh_config():
    """2026-08-21 热重载: config.maybe_reload 后由 worker 调用,
    把本模块别名刷新为新值(函数体裸名引用在调用时读模块全局)。"""
    global DECAY_HALFLIFE_DAYS
    DECAY_HALFLIFE_DAYS = config.DECAY_HALFLIFE_DAYS
    global REVIVE_DAYS
    REVIVE_DAYS = config.REVIVE_DAYS



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
        """状态由 分数+验证次数 决定：≥3次且≥60=trusted；≥3次且<40=discarded；
        其余保持现状（unverified/candidate/dubious 在凑满 3 次独立验证前不动——
        Phase0 T0.2：candidate 是打破死锁的采纳通道，不能被 40/60 规则提前改写）。"""
        if l.get("adoptions", 0) >= 3:
            if l["score"] >= 60:
                l["status"] = "trusted"
            elif l["score"] < 40:
                l["status"] = "discarded"
            else:
                l["status"] = "unverified"

    def _save(self):
        import storage.db as sdb
        for l in self.lessons:
            cond = l.get("conditions")
            cond_s = cond if isinstance(cond, str) else \
                json.dumps(cond or {}, ensure_ascii=False)
            sdb.x("INSERT OR REPLACE INTO lessons (id,symbol,category,content,score,"
                  "adoptions,good,bad,status,source_trade,regime,conditions,hist_evidence,ts,last_update) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  [l.get("id"), l.get("symbol"), l.get("category"), l.get("content"),
                   l.get("score", 50), l.get("adoptions", 0), l.get("good", 0),
                   l.get("bad", 0), l.get("status", "unverified"), l.get("source_trade"),
                   l.get("regime"), cond_s,
                   l.get("hist_evidence") if isinstance(l.get("hist_evidence"), str)
                   else json.dumps(l.get("hist_evidence") or {}, ensure_ascii=False),
                   l.get("ts"), l.get("last_update")],
                  db_path=self.db_path)

    # ---------- 经验生命周期 ----------
    def add(self, symbol, category, content, source_trade, status="unverified",
            regime=None, conditions=None, hist_evidence=None):
        """新增经验（初始分 50）。status 默认 unverified；平仓复盘链按一致性初筛
        传入 candidate/dubious（Phase0 T0.2，见 directional_trader._post_close_review）。
        regime: 教训产生的市场环境标签（Phase 4，兼容旧字段）。
        conditions: 场景条件向量 dict（2026-08-17，direction/vol_band/trend/
        signal_type），JSON 落库;无则空(全维度通配)。
        hist_evidence(2026-08-21 用户要求'经验从历史看是否有符合的'):
        教训诞生时的历史先验(同场景历史交易表现),只观测不进 ±10 验证循环。"""
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
            "status": status,
            "source_trade": source_trade,
            "regime": regime,
            "conditions": json.dumps(conditions, ensure_ascii=False)
                          if conditions else "",
            "hist_evidence": json.dumps(hist_evidence, ensure_ascii=False)
                             if hist_evidence else "",
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
    def trusted(self, symbol=None, min_score=60, regime=None, conditions=None):
        """可信经验（分数达标，正常参考）。
        regime(兼容旧调用): 给定标签时只匹配同环境教训。
        conditions(2026-08-17): 场景条件向量匹配,逐维比对(见 conditions_match);
        教训无标签维度 = 通配。"""
        out = [l for l in self.lessons
               if l["status"] == "trusted" and l["score"] >= min_score]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        if regime and not conditions:
            conditions = {"vol_band": regime}
        if conditions:
            out = [l for l in out if conditions_match(l, conditions)]
        return out

    def unverified(self, symbol=None):
        """未验证经验（仅提示，不强制）。"""
        out = [l for l in self.lessons if l["status"] == "unverified"]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        return out

    def candidates(self, symbol=None):
        """候选经验（Phase0 T0.2：平仓复盘一致性初筛通过，等待后续独立交易验证）。
        决策层低权重参考（只写理由+采纳追踪，不改参数）。"""
        out = [l for l in self.lessons if l["status"] == "candidate"]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        return out

    def dubious(self, symbol=None):
        """存疑经验（一致性初筛未通过：教训归因与本笔结果矛盾）。不进采纳池，仅观察。"""
        out = [l for l in self.lessons if l["status"] == "dubious"]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        return out

    def discarded(self, symbol=None, regime=None, conditions=None):
        """已弃用经验（证明是错的，不参考）。regime/conditions 过滤语义同 trusted()。"""
        out = [l for l in self.lessons if l["status"] == "discarded"]
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        if regime and not conditions:
            conditions = {"vol_band": regime}
        if conditions:
            out = [l for l in out if conditions_match(l, conditions)]
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


def build_conditions(direction=None, regime_dict=None, signal_type="pullback"):
    """教训/信号的【场景条件向量】(2026-08-17 用户要求'经验要有适用场景维度'):
    - direction: long/short
    - vol_band: regime tag(low_vol/mid_vol/high_vol)
    - trend: trend_slope 符号 → up/down/flat
    - signal_type: pullback/breakout
    所有维度可选;有值的维度才参与匹配。纯数据,中文不参与。"""
    cond = {}
    if direction:
        cond["direction"] = direction
    if signal_type:
        cond["signal_type"] = signal_type
    if regime_dict:
        tag = regime_dict.get("tag")
        if tag:
            cond["vol_band"] = tag
        ts = regime_dict.get("trend_slope")
        if ts is not None:
            cond["trend"] = ("up" if ts > 0.0005
                             else "down" if ts < -0.0005 else "flat")
    return cond


def conditions_match(lesson, conditions):
    """教训与当前场景是否匹配: conditions 中每个【有值】维度都必须一致;
    教训缺失该维度 = 通配(旧数据)。旧教训无 conditions 仅有 regime 时,
    按 vol_band 迁移匹配。"""
    if not conditions:
        return True
    lc = None
    raw = lesson.get("conditions")
    if isinstance(raw, str) and raw:
        try:
            lc = json.loads(raw)
        except Exception:
            lc = None
    elif isinstance(raw, dict):
        lc = raw
    if not lc and lesson.get("regime"):
        lc = {"vol_band": lesson.get("regime")}
    if not lc:
        return True
    for k, v in conditions.items():
        if not v:
            continue
        if lc.get(k) and lc[k] != v:
            return False
    return True


def _evidence_weight(lesson, now=None):
    """单条教训的证据权重 = 净验证次数钳制 × 时间衰减(2026-08-20 FinMem 式):
    权重 = clamp(good-bad, 0, CAP) × 0.5^(距上次验证天数 / EVIDENCE_HALFLIFE_DAYS)。
    为什么衰减: 市场 regime 会漂移,半衰期前验证的证据不应永久满权重——
    教训必须被新交易持续验证才能保持强度(分数衰减已有,此处补证据衰减)。"""
    now = now or time.time()
    net = int(lesson.get("good", 0) or 0) - int(lesson.get("bad", 0) or 0)
    net = max(0, min(config.EVIDENCE_CAP_PER_LESSON, net))
    age_days = max(0.0, (now - (lesson.get("last_update") or now)) / 86400.0)
    return net * (0.5 ** (age_days / config.EVIDENCE_HALFLIFE_DAYS))


def evidence_strength(bank, symbol, category, conditions=None, now=None):
    """教训的【数据验证强度】聚合(2026-08-17 用户要求'教训聚合生效'):
    只聚合 trusted;每条教训的权重 = 净验证次数(good - bad),钳制在
    [0, config.EVIDENCE_CAP_PER_LESSON]——单条教训再强也有上限(防独裁),
    多条独立验证的教训线性叠加,再乘时间衰减(2026-08-20,见 _evidence_weight)。
    中文 content 完全不参与计算。
    conditions: 场景条件向量,只聚合匹配当前场景的教训。"""
    total = 0.0
    for l in bank.trusted(symbol, conditions=conditions):
        if l.get("category") != category:
            continue
        total += _evidence_weight(l, now)
    return round(total, 2)


def rollup_lessons(bank=None, db_path=None):
    """场景归纳(2026-08-17 用户要求'多维度经验总结'):
    同 symbol+类别+场景条件的 trusted 教训 ≥ config.ROLLUP_MIN_MEMBERS 时,
    沉淀一条归纳教训(lesson_rollups 表)。归纳层【只读汇总】——strength 是
    成员权重的和,成员各自仍走 ±10 验证循环,归纳不参与验证(防回声)。
    返回本轮归纳列表。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    if bank is None:
        bank = ScoredExperience(db_path or "experience_scored.json")
    groups = {}
    for l in bank.trusted():
        cond = l.get("conditions")
        if isinstance(cond, str):
            try:
                cond = json.loads(cond) if cond else {}
            except Exception:
                cond = {}
        key = (l.get("symbol"), l.get("category"),
               json.dumps(cond, ensure_ascii=False, sort_keys=True))
        g = groups.setdefault(key, {"lessons": [], "conditions": cond})
        g["lessons"].append(l)
    now = time.time()
    out = []
    live_keys = set()
    for (symbol, category, _), g in groups.items():
        members = g["lessons"]
        if len(members) < config.ROLLUP_MIN_MEMBERS:
            continue
        # 2026-08-20: 归纳强度与 evidence_strength 同一衰减口径(防两套数学打架)
        strength = round(sum(_evidence_weight(l, now) for l in members), 2)
        cond_s = json.dumps(g["conditions"], ensure_ascii=False, sort_keys=True)
        live_keys.add((symbol, category, cond_s))
        sdb.x("INSERT OR REPLACE INTO lesson_rollups "
              "(symbol, category, conditions, strength, member_count, member_ids, ts, last_update) "
              "SELECT ?,?,?,?,?,?,COALESCE((SELECT ts FROM lesson_rollups WHERE symbol=? "
              "AND category=? AND conditions=?), ?), ?",
              [symbol, category, cond_s, strength, len(members),
               json.dumps([m["id"] for m in members]),
               symbol, category, cond_s, now, now], db_path=db_path)
        out.append({"symbol": symbol, "category": category,
                    "conditions": g["conditions"], "strength": strength,
                    "member_count": len(members)})
    # 清理: 成员掉到门槛以下的旧归纳行删除
    for r in sdb.q("SELECT id, symbol, category, conditions FROM lesson_rollups",
                   db_path=db_path):
        if (r["symbol"], r["category"], r["conditions"]) not in live_keys:
            sdb.x("DELETE FROM lesson_rollups WHERE id=?", [r["id"]], db_path=db_path)
    return out


def get_rollup(db_path, symbol, category, conditions=None):
    """查场景归纳教训(决策层审计注释用;数学不依赖它,evidence_strength 才是权威)。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    if conditions:
        cond_s = json.dumps(conditions, ensure_ascii=False, sort_keys=True)
        row = sdb.q1("SELECT * FROM lesson_rollups WHERE symbol=? AND category=? "
                     "AND conditions=?", [symbol, category, cond_s], db_path=db_path)
        if row:
            return row
    rows = sdb.q("SELECT * FROM lesson_rollups WHERE symbol=? AND category=?",
                 [symbol, category], db_path=db_path)
    if not rows:
        return None
    for r in rows:
        cond = {}
        try:
            cond = json.loads(r["conditions"]) if r["conditions"] else {}
        except Exception:
            cond = {}
        if conditions_match({"conditions": cond}, conditions):
            return r
    return None


def historical_evidence(symbol, direction, conditions=None, db_path=None):
    """教训的历史先验(2026-08-21 用户要求'经验从历史看看是否有符合的'):
    在【历史已平仓交易】里找同场景先例(symbol+方向+波动带+趋势)——
    返回 {samples, win_rate, mean_r, matched_trades}。只观测,不进决策。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    rows = sdb.q("SELECT id, symbol, direction, pnl, entry_price, stop_loss "
                 "FROM trades WHERE status='closed' AND symbol=? AND direction=?",
                 [symbol, direction], db_path=db_path)
    feats = {r["trade_id"]: r for r in sdb.q(
        "SELECT trade_id, regime_tag, trend_slope FROM trade_features",
        db_path=db_path)}
    cond = conditions or {}
    matched = []
    for r in rows:
        f = feats.get(r["id"]) or {}
        if cond.get("vol_band") and f.get("regime_tag")                 and f["regime_tag"] != cond["vol_band"]:
            continue
        ts = f.get("trend_slope")
        if cond.get("trend") and ts is not None:
            t_now = "up" if ts > 0.0005 else ("down" if ts < -0.0005 else "flat")
            if t_now != cond["trend"]:
                continue
        matched.append(r)
    if len(matched) < 1:
        return {"samples": 0, "win_rate": None, "mean_r": None,
                "matched_trades": []}
    wins = sum(1 for r in matched if (r["pnl"] or 0) > 0)
    rs = []
    for r in matched:
        e, s, pnl = r["entry_price"], r["stop_loss"], r["pnl"]
        if e and s and pnl is not None and abs(e - s) > 0:
            sd = abs(e - s) / e
            if sd > 0:
                rs.append(pnl / sd)
    return {"samples": len(matched),
            "win_rate": round(wins / len(matched), 3),
            "mean_r": round(sum(rs) / len(rs), 3) if rs else None,
            "matched_trades": [r["id"] for r in matched]}


def record_combo_trial(trade_id, adopted_ids, closed, db_path=None):
    """记录组合试验(2026-08-21 用户洞察'单条不盈利,combo 可能盈利'):
    本笔实际采纳的教训 ≥2 条时,按升序 id 拼签名记一行真实结果。
    只观测——combo 统计达标后走 experiments 提案,决策层零改动。"""
    ids = sorted(set(int(x) for x in (adopted_ids or []) if str(x).isdigit()))
    if len(ids) < 2:
        return None
    import storage.db as sdb
    sdb.init_db(db_path)
    sig = ",".join(str(i) for i in ids)
    pnl = closed.get("pnl")
    r_mult = None
    try:
        entry = closed.get("entry_price")
        stop = closed.get("stop_loss")
        if entry and stop and pnl is not None and abs(entry - stop) > 0:
            sd = abs(entry - stop) / entry
            r_mult = round(pnl / sd, 4) if sd > 0 else None
    except Exception:
        r_mult = None
    notional = closed.get("notional_usdt")
    pnl_usdt = round(pnl * notional, 4) if pnl is not None and notional else None
    sdb.x("INSERT INTO combo_trials (trade_id, signature, member_ids, pnl, "
          "pnl_usdt, r_multiple, ts) VALUES (?,?,?,?,?,?,?)",
          [trade_id, sig, json.dumps(ids), pnl, pnl_usdt, r_mult, time.time()],
          db_path=db_path)
    return sig


def combo_stats(db_path=None, min_samples=3):
    """组合试验统计(只读): 每个签名 ≥min_samples 笔的胜率/期望 R。
    用于 experiments 提案与看板展示,绝不自动改决策。"""
    import storage.db as sdb
    sdb.init_db(db_path)
    rows = sdb.q("SELECT signature, member_ids, pnl, r_multiple FROM combo_trials",
                 db_path=db_path)
    groups = {}
    for r in rows:
        g = groups.setdefault(r["signature"], {"ids": r["member_ids"],
                                               "pnls": [], "rs": []})
        if r["pnl"] is not None:
            g["pnls"].append(r["pnl"])
        if r["r_multiple"] is not None:
            g["rs"].append(r["r_multiple"])
    out = []
    for sig, g in groups.items():
        n = len(g["pnls"])
        if n < min_samples:
            continue
        wins = sum(1 for p in g["pnls"] if p > 0)
        mean_r = (sum(g["rs"]) / len(g["rs"])) if g["rs"] else None
        out.append({"signature": sig, "member_ids": g["ids"],
                    "samples": n, "win_rate": round(wins / n, 3),
                    "mean_r": round(mean_r, 3) if mean_r is not None else None})
    out.sort(key=lambda x: (x["mean_r"] is None, -(x["mean_r"] or 0)))
    return out


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

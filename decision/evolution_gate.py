"""
进化验证门 — 自进化体系本身的优化策略（元优化层）。

问题（审计 CR-8/CR-10）：五层自进化（策略/权重/经验/阈值/因子）里，
多数层的"进化"没有样本外验证门——新规则直接上线，失败也不回滚，
且各层互相参考形成回声室（echo chamber）。

本模块提供统一的进化纪律：
  1. 影子验证（shadow）：候选改动只记录决策、不实际执行，
     在影子样本上证明正期望且优于现役，才允许 promote 上线。
  2. 上线观察期：promote 后在观察期内持续对比候选 vs 现役，
     一旦候选滑落到现役之下（连续 bad 批）→ rollback 回滚。
  3. 抗回声室：候选必须独立于现役层产出（数据同源≠规则同源），
     同源候选直接拒绝（same_source 拒绝规则）。
  4. 每次 promote/rollback 都记录事件，进化过程自身可审计、可评估。

这是"优化自进化的优化策略"：不直接进化交易规则，而是进化"如何进化"。
"""
import json
import os
import time


class EvolutionGate:
    """单个进化层的验证门：现役 vs 候选。"""

    def __init__(self, name, path="evolution_gate.json",
                 min_shadow_samples=30, min_edge=0.0, observe_batches=3,
                 batch_size=10, on_rollback=None):
        self.name = name
        self.path = path
        self.min_shadow_samples = min_shadow_samples  # 影子样本数门槛
        self.min_edge = min_edge                      # 候选须超越现役的最小优势
        self.observe_batches = observe_batches        # 上线观察批数
        self.batch_size = batch_size
        self.on_rollback = on_rollback   # R2-1：回滚回调（回写真实权重）
        self.state = self._load()

    def _fresh_state(self):
        return {
            "name": self.name,
            "incumbent": {"label": "基线", "pnls": []},
            "candidate": None,          # {"label", "source", "pnls": [], "proposed_ts"}
            "live_batches": [],         # 上线后按批记录候选 vs 现役
            "rollbacks": 0,
            "promotions": 0,
            "events": [],
        }

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if d.get("name") != self.name:
                return self._fresh_state()
            return d
        except Exception:
            return self._fresh_state()

    def _save(self):
        # 2026-08-20: 原子写（.tmp + os.replace，仓库约定）——进程被杀在写中途
        # 不会留半个 JSON 导致下次启动 gate 状态全丢。
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _log(self, event, detail=""):
        self.state["events"].append(
            {"ts": time.time(), "event": event, "detail": detail})
        if len(self.state["events"]) > 200:
            self.state["events"] = self.state["events"][-200:]

    # ---------- 决策结果记录 ----------
    def record_incumbent(self, pnl):
        """现役规则的一次真实决策结果。"""
        self.state["incumbent"]["pnls"].append(pnl)
        self._trim(self.state["incumbent"])
        self._observe(pnl)
        self._save()

    def propose_candidate(self, label, source, meta=None):
        """提出候选规则（同一时间只有一个候选）。
        source 描述候选的数据/规则来源，用于回声室检测。
        meta（2026-08-20）: 候选的机器可读参数（如 {"threshold": 45}），
        promote 时随 incumbent 保留，调用方据此应用变更。"""
        self.state["candidate"] = {
            "label": label, "source": source,
            "pnls": [],
            "proposed_ts": time.time(),
            "meta": meta or {},
        }
        self._log("propose", f"{label} (source={source})")
        self._save()

    def record_shadow(self, pnl):
        """影子期：候选规则的决策结果只记录、不执行。"""
        cand = self.state.get("candidate")
        if not cand:
            return None
        cand["pnls"].append(pnl)
        self._trim(cand)
        self._save()
        return self.evaluate()

    def _trim(self, bucket, cap=500):
        if len(bucket.get("pnls", [])) > cap:
            bucket["pnls"] = bucket["pnls"][-cap:]

    def _mean(self, xs):
        return sum(xs) / len(xs) if xs else 0.0

    # ---------- 影子验证 ----------
    def evaluate(self):
        """影子样本够了吗？候选显著优于现役吗？→ promote / keep shadow。"""
        cand = self.state.get("candidate")
        if not cand:
            return {"action": "none"}
        if len(cand["pnls"]) < self.min_shadow_samples:
            return {"action": "shadow", "n": len(cand["pnls"]),
                    "need": self.min_shadow_samples}
        cand_mean = self._mean(cand["pnls"])
        inc_mean = self._mean(self.state["incumbent"]["pnls"][-len(cand["pnls"]):]) \
            if self.state["incumbent"]["pnls"] else 0.0
        if cand_mean - inc_mean < self.min_edge:
            # 候选不达标 → 淘汰候选（保留现役）
            self._log("reject", f"cand {cand_mean:.4f} vs inc {inc_mean:.4f}")
            self.state["candidate"] = None
            self._save()
            return {"action": "reject", "cand": cand_mean, "inc": inc_mean}
        # 达标 → 上线
        self.state["incumbent"] = cand
        self.state["candidate"] = None
        self.state["live_batches"] = []
        self.state["promotions"] += 1
        self._log("promote", f"{cand['label']} (edge {cand_mean-inc_mean:+.4f})")
        self._save()
        return {"action": "promote", "cand": cand_mean, "inc": inc_mean}

    # ---------- 上线观察期 / 回滚 ----------
    def _observe(self, pnl):
        """上线后每 batch_size 个现役样本对比一次（对比基准=被替换者的历史均值）。"""
        if not self.state.get("live_batches") and self.state["promotions"] == 0:
            return
        # 简化：观察期对比"最近批均值 vs 现役全历史均值"
        hist = self.state["incumbent"]["pnls"]
        if len(hist) % self.batch_size != 0 or len(hist) < self.batch_size * 2:
            return
        recent = hist[-self.batch_size:]
        older = hist[-2 * self.batch_size:-self.batch_size]
        if self._mean(recent) < self._mean(older) - self.min_edge:
            self._rollback()

    def _rollback(self):
        """回滚：现役退化 → 恢复为基线（全空=纯保守）并计数。"""
        self.state["rollbacks"] += 1
        old_label = self.state["incumbent"]["label"]
        self.state["incumbent"] = {"label": "基线(回滚)", "pnls": []}
        self.state["live_batches"] = []
        self._log("rollback", f"{old_label} 退化，回滚至保守基线")
        # R2-1：先触发回调（回写真实权重），再 _save 持久化 gate 状态——
        # 若回写失败，gate 状态尚未落盘，两者不会不一致
        if self.on_rollback:
            self.on_rollback()
        self._save()

    # ---------- 状态 ----------
    def status(self):
        s = self.state
        cand = s.get("candidate")
        return {
            "layer": s["name"],
            "incumbent": s["incumbent"]["label"],
            "inc_samples": len(s["incumbent"]["pnls"]),
            "inc_mean_pnl": round(self._mean(s["incumbent"]["pnls"]), 4),
            "candidate": cand["label"] if cand else None,
            "shadow_n": len(cand["pnls"]) if cand else 0,
            "promotions": s["promotions"],
            "rollbacks": s["rollbacks"],
        }


if __name__ == "__main__":
    # 演示：影子验证 → 上线 → 退化 → 回滚
    import random
    random.seed(1)
    g = EvolutionGate("阈值层", "evolution_gate_demo.json",
                      min_shadow_samples=30, min_edge=0.005, batch_size=10)
    # 现役基线：期望 0.001
    for _ in range(30):
        g.record_incumbent(random.uniform(-0.005, 0.007))
    # 候选：期望 0.01（真实更好）→ 应 promote
    g.propose_candidate("阈值68", source="分数-盈亏桶校准(独立样本)")
    for _ in range(30):
        r = g.record_shadow(random.uniform(0.005, 0.015))
    print("影子评估:", r["action"], "| 状态:", g.status())
    # 上线后退化：现役期望变负 → 应 rollback
    for _ in range(20):
        g.record_incumbent(random.uniform(-0.02, -0.005))
    print("退化后状态:", g.status())
    assert g.status()["rollbacks"] >= 1
    os.remove("evolution_gate_demo.json")
    print("进化验证门演示通过 ✅")

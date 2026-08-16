"""
评分权重学习 — 五层自进化中"权重层"的数据闭环（元优化，审计 CR-7）。

此前 ARB_WEIGHTS 是拍脑袋的静态权重，没有任何数据依据。
本模块：
  1. 记录每次套利决策的 6 个子分数 + 平仓后的实际盈亏
  2. 定期评估各因子的"分数→盈亏"贡献（高分数段 vs 低分数段盈亏差）
  3. 生成候选权重（只保留贡献为正的因子，重新归一化）
  4. 候选经 EvolutionGate 影子验证（以分数-盈亏 IC 为门指标）：
     候选 IC 超越现役 IC 才 promote；不达标 reject；上线退化 rollback
  5. 样本不足时保持现役权重不动——"不进化也是进化的一种"（防小样本噪声）

用法：
  wl = WeightLearner(weights=ARB_WEIGHTS, min_samples=40)
  wl.record(scores_dict, pnl)    # 每次平仓后调用
  decide 时用 wl.weights 做 composite（替代静态 ARB_WEIGHTS）
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import numpy as np

from decision.evolution_gate import EvolutionGate


def spearman(xs, ys):
    """Spearman 相关。样本太少返回 0。"""
    xs = np.array(xs, dtype=float)
    ys = np.array(ys, dtype=float)
    if len(xs) < 10 or np.std(xs) == 0 or np.std(ys) == 0:
        return 0.0
    rx = np.argsort(np.argsort(xs))
    ry = np.argsort(np.argsort(ys))
    return float(np.corrcoef(rx, ry)[0, 1])


class WeightLearner:
    def __init__(self, weights, path="weight_state.json",
                 min_samples=40, min_positive_contrib=0.0,
                 gate_min_shadow=20, gate_min_edge=0.01, max_history=500):
        self.base_weights = dict(weights)          # 初始静态权重（回滚兜底）
        self.weights = dict(weights)               # 现役权重
        self.path = path
        self.min_samples = min_samples
        self.min_positive_contrib = min_positive_contrib
        self.gate = EvolutionGate("评分权重层", "weight_gate.json",
                                  min_shadow_samples=gate_min_shadow,
                                  min_edge=gate_min_edge,
                                  on_rollback=self.rollback_to_base)  # R2-1：回滚回写
        self.records = []   # {"scores": {...}, "pnl": float}
        self.version = 0
        self.rolled_back_at = None   # R2-1：回滚时间戳（审计）
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.weights = d.get("weights", self.weights)
            self.records = d.get("records", [])
            self.version = d.get("version", 0)
        except Exception:
            pass

    def _save(self):
        try:
            # R1-3: 原子写（先写 .tmp 再 os.replace，崩溃不留半截 JSON）
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"weights": self.weights, "records": self.records,
                           "version": self.version}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _composite(scores, w):
        total = sum(w[k] * scores.get(k, 50) for k in w)
        wsum = sum(w.values())
        return total / wsum if wsum else 50.0

    # ---------- 记录决策结果 ----------
    def record(self, scores, pnl, pnl_estimated=False):
        """记录一次已了结决策：子分数 + 净盈亏（占名义比例）。
        pnl_estimated=True 的估算样本不参与贡献/IC 评估（防噪声标签）。"""
        self.records.append({"scores": dict(scores), "pnl": float(pnl),
                             "pnl_estimated": bool(pnl_estimated),
                             "ts": time.time()})   # R2-2：时间戳（train/valid 切分用）
        if len(self.records) > 500:
            self.records = self.records[-500:]
        self._save()
        self.maybe_evolve()

    # ---------- 因子贡献评估 ----------
    def _factor_contribution(self, key, records=None):
        """该因子分数与盈亏的关系：高分数段均值盈亏 − 低分数段均值盈亏。
        估算样本（pnl_estimated）不参与。"""
        recs = records if records is not None else self.records
        pairs = [(r["scores"].get(key, 50), r["pnl"]) for r in recs
                 if not r.get("pnl_estimated")]
        if len(pairs) < 10:
            return 0.0
        med = sorted(s for s, _ in pairs)[len(pairs) // 2]
        hi = [p for s, p in pairs if s >= med]
        lo = [p for s, p in pairs if s < med]
        if not hi or not lo:
            return 0.0
        return (sum(hi) / len(hi)) - (sum(lo) / len(lo))

    # ---------- 进化（过验证门） ----------
    def maybe_evolve(self):
        """R2-2：按时间切分——train(前70%)生成候选、valid(后30%)算 IC（样本外）。
        旧无 ts 样本打 legacy 标不参与切分。"""
        # legacy 样本（无 ts）打标排除，避免污染时间顺序
        legacy = [r for r in self.records if r.get("ts") is None]
        for r in legacy:
            r["_legacy"] = True
        recs = sorted((r for r in self.records if not r.get("_legacy")),
                      key=lambda r: r.get("ts", 0))
        n = len(recs)
        n_train = int(n * 0.7)
        train, valid = recs[:n_train], recs[n_train:]
        if len(train) < self.min_samples or len(valid) < self.gate.min_shadow_samples:
            return {"action": "wait", "n": n, "need": self.min_samples,
                    "need_valid": self.gate.min_shadow_samples, "legacy": len(legacy)}
        # 候选：只用 train 段生成（只保留贡献为正的因子，重新归一化）
        contrib = {k: self._factor_contribution(k, train) for k in self.weights}
        pos = {k: w for k, w in self.weights.items()
               if contrib[k] > self.min_positive_contrib}
        if not pos or set(pos.keys()) == set(self.weights.keys()):
            return {"action": "no_change",
                    "contrib": {k: round(v, 4) for k, v in contrib.items()}}
        cand = {k: pos.get(k, 0.0) for k in self.weights}
        tot = sum(cand.values())
        cand = {k: v / tot for k, v in cand.items()}

        # IC 只吃 valid 段（候选在 train 生成，valid 是样本外）
        vpnls = [r["pnl"] for r in valid if not r.get("pnl_estimated")]
        v_inc = [self._composite(r["scores"], self.weights) for r in valid
                 if not r.get("pnl_estimated")]
        v_cand = [self._composite(r["scores"], cand) for r in valid
                  if not r.get("pnl_estimated")]
        ic_inc = spearman(v_inc, vpnls)
        ic_cand = spearman(v_cand, vpnls)

        if self.gate.state.get("candidate") is None:
            self.gate.propose_candidate(
                f"权重v{self.version+1}", source=f"贡献评估(train段):{contrib}")
        self.gate.record_incumbent(ic_inc)
        r = self.gate.record_shadow(ic_cand)
        if r and r.get("action") == "promote":
            self.weights = cand
            self.version += 1
            self._save()
            print(f"✅ 权重层进化通过样本外验证门 → v{self.version}: {cand}")
        elif r and r.get("action") == "reject":
            print("⛔ 权重候选被样本外验证门拒绝（IC 未超越现役），保持现役权重")
        return r or {"action": "shadow"}

    def rollback_to_base(self):
        """回滚到初始静态权重（R2-1：version 自增 + 时间戳，可审计进化/回滚次数）。"""
        self.weights = dict(self.base_weights)
        self.version += 1            # 回滚也是版本事件，不归 0
        self.rolled_back_at = time.time()
        self._save()
        print(f"⛔ 权重层验证门触发回滚 → 已回写 weights=base_weights (v{self.version})")

    def status(self):
        return {"version": self.version, "weights": self.weights,
                "records": len(self.records), "gate": self.gate.status()}


if __name__ == "__main__":
    # 演示：因子A有效、因子B无效 → 候选权重去掉B → 验证门决定
    import random
    random.seed(5)
    wl = WeightLearner({"a": 0.5, "b": 0.5}, "weight_demo.json",
                       min_samples=40, gate_min_shadow=20, gate_min_edge=0.01)
    for i in range(60):
        scores = {"a": random.uniform(20, 100), "b": random.uniform(20, 100)}
        # 真实规律：盈亏只由 a 决定，b 是噪声
        pnl = 0.02 * (scores["a"] - 60) / 40 + random.uniform(-0.004, 0.004)
        wl.record(scores, pnl)
    print("最终:", wl.status())
    os.remove("weight_demo.json")
    os.remove("weight_gate.json") if os.path.exists("weight_gate.json") else None
    print("权重学习演示完成 ✅")

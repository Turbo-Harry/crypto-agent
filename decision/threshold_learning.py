"""
阈值自适应 — 决策阈值也不是真理，用历史决策结果动态校准。

机制：
  1. 记录每次决策：综合分 + 实际结果（盈亏）
  2. 按分数分桶，统计每桶的平均盈亏
  3. 找到"盈亏平衡分数"（期望从负转正的转折点）
  4. 阈值 = 转折点 + 安全边际（动态更新）

这样阈值不是拍脑袋的 70，而是数据校准出来的。
样本越多，阈值越准；市场变了，阈值跟着变。
"""
import json
import os
import time

import numpy as np


class ThresholdLearner:
    def __init__(self, path="threshold_state.json", initial_threshold=70,
                 min_samples=30, bucket_width=5, safety_margin=5,
                 min_bucket_samples=8, min_threshold=60, max_threshold=90,
                 max_history=500, db_path=None, gated=False):
        # db_path=None → 共享库（key=path，生产：threshold_state_dir/arb.json）；
        # db_path 显式传（测试隔离）→ 独立库。防测试临时 key 污染生产 thresholds 表。
        # gated=True（2026-08-20 DEF-5）: record() 只记录不自动校准——
        # 阈值变更由 EvolutionGate 影子验证通过后经 apply_threshold() 生效。
        self.path = path
        self.db_path = db_path
        self.gated = gated
        self.threshold = initial_threshold
        self.min_samples = min_samples
        self.bucket_width = bucket_width
        self.safety_margin = safety_margin
        self.min_bucket_samples = min_bucket_samples  # 每桶最少样本数（防噪声桶带偏）
        self.min_threshold = min_threshold            # 阈值下限夹逼（防全局放行）
        self.max_threshold = max_threshold            # 阈值上限夹逼
        self.max_history = max_history                # 只保留最近 N 条（旧 regime 衰减）
        self.decisions = []  # {score, pnl}
        self._load()

    def _load(self):
        # SQLite 后端（storage 层）：key=path（dir/arb 各自独立状态）
        import storage.db as sdb
        sdb.init_db(self.db_path)
        row = sdb.q1("SELECT threshold, records FROM thresholds WHERE key=?",
                     [self.path], db_path=self.db_path)
        if row:
            self.threshold = row["threshold"]
            try:
                self.decisions = json.loads(row["records"] or "[]")
            except Exception:
                self.decisions = []
            if len(self.decisions) > self.max_history:
                self.decisions = self.decisions[-self.max_history:]

    def _save(self):
        # SQLite 事务写（原子），替代 .tmp + os.replace
        import storage.db as sdb
        sdb.x("INSERT OR REPLACE INTO thresholds (key, threshold, records, updated_at) "
              "VALUES (?,?,?,?)",
              [self.path, self.threshold, json.dumps(self.decisions), time.time()],
              db_path=self.db_path)

    # ---------- 记录决策结果 ----------
    def record(self, score, pnl, pnl_estimated=False):
        """记录一次决策（综合分 + 实际盈亏）。
        pnl_estimated=True 的估算样本不参与校准（防噪声标签污染阈值）。"""
        self.decisions.append({"score": score, "pnl": pnl,
                               "pnl_estimated": bool(pnl_estimated)})
        if len(self.decisions) > self.max_history:
            self.decisions = self.decisions[-self.max_history:]
        # gated 模式（DEF-5）: 只记录,校准提案由进化门驱动,不在此自动生效
        if self.gated:
            self._save()
            return
        # 样本够了就自动校准
        if len([d for d in self.decisions if not d.get("pnl_estimated")]) >= self.min_samples:
            self.calibrate()
        else:
            self._save()

    # ---------- 校准阈值 ----------
    def _calibration_target(self):
        """校准数学（纯计算，不改任何状态）：返回 (建议阈值 or None, 诊断 dict)。
        统计防护：桶样本数不足不参与、要求连续多桶非负（防噪声桶）、阈值夹逼、
        放松方向闸（新放行分数段必须正期望）。2026-08-20 从 calibrate 抽出，
        供 propose()（进化门提案）与 calibrate()（旧直接生效路径）复用。"""
        if len(self.decisions) < self.min_samples:
            return None, {"unchanged": self.threshold, "samples": len(self.decisions)}

        scores = np.array([d["score"] for d in self.decisions
                           if not d.get("pnl_estimated")])
        pnls = np.array([d["pnl"] for d in self.decisions
                         if not d.get("pnl_estimated")])
        if len(scores) < self.min_samples:
            return None, {"unchanged": self.threshold,
                          "samples": len(self.decisions),
                          "reason": "有效样本不足（估算样本不参与校准）"}

        # 分桶：每 bucket_width 分一桶，算平均盈亏
        buckets = {}
        for s, p in zip(scores, pnls):
            b = int(s // self.bucket_width) * self.bucket_width
            buckets.setdefault(b, []).append(p)

        # 只有样本数达标的桶才参与校准
        qual = {b: float(np.mean(v)) for b, v in buckets.items()
                if len(v) >= self.min_bucket_samples}
        if len(qual) < 2:
            return None, {"unchanged": self.threshold, "samples": len(self.decisions),
                          "reason": f"样本达标桶不足（<2，需每桶≥{self.min_bucket_samples}样本）"}

        # 找"盈亏平衡分数"：本桶及更高分至少 2 个连续桶都非负
        sorted_b = sorted(qual)
        break_even = None
        for b in sorted_b:
            higher = [bb for bb in sorted_b if bb >= b]
            if len(higher) >= 2 and all(qual[bb] >= 0 for bb in higher[:2]):
                break_even = b
                break

        if break_even is not None:
            new_threshold = min(self.max_threshold,
                                max(self.min_threshold, break_even + self.safety_margin))
            # 元优化方向闸：放松阈值（new < old）时，新放行的分数段必须正期望
            # （防噪声桶把阈值越拉越低=全局放行；收紧方向无此风险，直接允许）
            if new_threshold < self.threshold:
                band = [p for s, p in zip(scores, pnls)
                        if new_threshold <= s < self.threshold]
                if len(band) >= self.min_bucket_samples and float(np.mean(band)) <= 0:
                    return None, {"unchanged": self.threshold,
                                  "samples": len(self.decisions),
                                  "reason": (f"放松阈值被方向闸拒绝：新放行段"
                                             f"[{new_threshold},{self.threshold})"
                                             f"平均盈亏 {np.mean(band):+.4f} ≤ 0")}
            return new_threshold, {"old": self.threshold, "new": new_threshold,
                                   "break_even": break_even,
                                   "samples": len(self.decisions)}
        return None, {"unchanged": self.threshold, "samples": len(self.decisions),
                      "reason": "未找到稳定的盈亏平衡点（分数→盈亏非单调）"}

    def calibrate(self):
        """旧直接生效路径（非 gated）：算出目标阈值立即应用。返回值语义不变。"""
        target, info = self._calibration_target()
        if target is None or target == self.threshold:
            self._save()
            if target == self.threshold and "new" in info:
                return {"unchanged": self.threshold, "samples": len(self.decisions)}
            return info
        self.threshold = target
        self._save()
        return info

    # ---------- 进化门接口（2026-08-20 DEF-5 闭环） ----------
    def propose(self):
        """gated 模式提案：只算不改。有值得变更的目标阈值 → 返回该值,否则 None。
        供 EvolutionGate 发起候选,影子验证通过后由 apply_threshold 生效。"""
        target, _ = self._calibration_target()
        if target is not None and target != self.threshold:
            return target
        return None

    def apply_threshold(self, new_threshold):
        """进化门晋升/回滚时的唯一写入口。返回旧阈值（供日志）。
        不做 min/max 夹逼：提案值在 _calibration_target 已夹逼过；
        回滚基线 THRESHOLD_INITIAL(35) 低于学习器下限(60),夹逼会
        把回滚值偷偷抬高——回滚必须精确恢复用户拍板的基线。"""
        old = self.threshold
        self.threshold = new_threshold
        self._save()
        return old

    # ---------- 诊断 ----------
    def profile(self):
        """当前分数→盈亏分布画像。"""
        if not self.decisions:
            return {}
        buckets = {}
        for d in self.decisions:
            b = int(d["score"] // self.bucket_width) * self.bucket_width
            buckets.setdefault(b, []).append(d["pnl"])
        return {f"{b}-{b+self.bucket_width}分": {
            "样本": len(v), "平均盈亏": float(np.mean(v))} for b, v in sorted(buckets.items())}

    def decide(self, score):
        """用当前阈值决策。"""
        return score >= self.threshold


if __name__ == "__main__":
    import random
    random.seed(42)
    # 模拟：30 次决策，分数和结果（假设真实盈亏平衡在 65 分附近）
    tl = ThresholdLearner("threshold_demo.json", initial_threshold=70)
    print("模拟 30 次决策（真实盈亏平衡约在 65 分）:")
    for i in range(30):
        score = random.uniform(50, 90)
        # 模拟：分数 > 65 的决策大概率盈利
        if score > 65:
            pnl = random.uniform(0, 0.03)
        else:
            pnl = random.uniform(-0.03, 0)
        tl.record(score, pnl)

    print(f"\n初始阈值 70 → 校准后阈值: {tl.threshold}")
    print("\n分数→盈亏画像:")
    for k, v in tl.profile().items():
        print(f"  {k}: 样本{v['样本']} 平均盈亏{v['平均盈亏']*100:+.2f}%")
    os.remove("threshold_demo.json")

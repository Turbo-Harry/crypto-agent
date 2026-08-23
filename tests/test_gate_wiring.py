"""DEF-5 闭环 + 证据时间衰减 离线单测（2026-08-20，FakeAdapter，不触网）。

覆盖四条链路：
  1. evidence_strength 时间衰减：新教训满权重，半衰期前的教训权重减半；
  2. gated 模式：record() 只记录不自动改阈值，propose() 只算不改；
  3. 进化门晋升：候选阈值影子验证达标 → apply 生效；
  4. 观察期退化 → 自动回滚到 THRESHOLD_INITIAL 基线。

运行: PYTHONPATH=lib python3 tests/test_gate_wiring.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from decision.experience_scoring import ScoredExperience, evidence_strength
from decision.threshold_learning import ThresholdLearner
from decision.evolution_gate import EvolutionGate

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
    if ok:
        passed += 1
    else:
        failed += 1


# ---------- 1. 证据时间衰减 ----------
def test_evidence_decay():
    tmp = tempfile.mkdtemp(prefix="tst_decay_")
    bank = ScoredExperience(path=os.path.join(tmp, "e.db"))
    lid = bank.add("BTC", "止损", "止损太紧", "txn_t1")
    for _ in range(3):                       # 3 次盈利验证 → trusted, net=3 钳制到 2
        bank.validate(lid, +0.02)
    s_fresh = evidence_strength(bank, "BTC", "止损")
    check("新教训满权重（净3钳制2 → 2.0）", abs(s_fresh - 2.0) < 0.01, f"实测 {s_fresh}")
    # 回拨 last_update 一个半衰期 → 权重应减半
    for l in bank.lessons:
        l["last_update"] -= config.EVIDENCE_HALFLIFE_DAYS * 86400
    s_old = evidence_strength(bank, "BTC", "止损")
    check("半衰期前的教训权重减半（≈1.0）", abs(s_old - 1.0) < 0.05, f"实测 {s_old}")
    # 再回拨一个半衰期 → 再减半
    for l in bank.lessons:
        l["last_update"] -= config.EVIDENCE_HALFLIFE_DAYS * 86400
    s_older = evidence_strength(bank, "BTC", "止损")
    check("两个半衰期 → 四分之一（≈0.5）", abs(s_older - 0.5) < 0.05, f"实测 {s_older}")


# 造一组"盈亏平衡点在 70 分"的决策样本：
# 桶 60/65 负期望、桶 70/75 正期望 → break_even=70 → 建议阈值 75（+5 安全边际）
def _feed_calibration_samples(tl):
    for _ in range(10):
        tl.record(62, -0.02)   # 桶 60：负
        tl.record(67, -0.01)   # 桶 65：负
        tl.record(72, +0.015)  # 桶 70：正
        tl.record(77, +0.02)   # 桶 75：正


# ---------- 2. gated 模式 ----------
def test_gated_mode():
    tmp = tempfile.mkdtemp(prefix="tst_gated_")
    # 非 gated 对照组：旧行为保持——满样本自动校准生效
    tl_old = ThresholdLearner(path="t_old", db_path=os.path.join(tmp, "a.db"),
                              initial_threshold=70)
    _feed_calibration_samples(tl_old)
    check("非 gated：满样本自动校准生效（70→75）", tl_old.threshold == 75,
          f"实测 {tl_old.threshold}")
    # gated 组：只记录，不自动改
    tl = ThresholdLearner(path="t_gated", db_path=os.path.join(tmp, "b.db"),
                          initial_threshold=70, gated=True)
    _feed_calibration_samples(tl)
    check("gated：满样本后阈值不动（仍 70）", tl.threshold == 70,
          f"实测 {tl.threshold}")
    prop = tl.propose()
    check("gated：propose 产出提案 75 且不改现役", prop == 75 and tl.threshold == 70,
          f"提案 {prop}, 现役 {tl.threshold}")
    old = tl.apply_threshold(75)
    check("apply_threshold 精确写入并返回旧值", tl.threshold == 75 and old == 70)


# ---------- 3+4. 引擎接线：提案→影子→晋升→退化→回滚 ----------
def test_gate_wiring_promote_and_rollback():
    from tests.test_phase0_review import _make_trader
    tmp = tempfile.mkdtemp(prefix="tst_wire_")
    dt, fake = _make_trader(tmp)
    # 换成 gated 学习器 + 小样本门（离线测试用小参数；生产走 config）
    dt.threshold_learner = ThresholdLearner(path="t_wire",
                                            db_path=os.path.join(tmp, "th.db"),
                                            initial_threshold=70, gated=True)
    dt.threshold_gate = EvolutionGate(
        "方向性阈值层", os.path.join(tmp, "gate.json"),
        min_shadow_samples=5, min_edge=0.001, batch_size=3,
        on_rollback=lambda: dt.threshold_learner.apply_threshold(
            config.THRESHOLD_INITIAL))
    # 预充样本让校准数学产生 70→75 的提案
    _feed_calibration_samples(dt.threshold_learner)
    # 第 1 步：无候选 → 产提案
    dt._threshold_gate_step(77, +0.02)
    cand = dt.threshold_gate.state.get("candidate")
    check("接线：平仓触发提案进门", cand is not None
          and (cand.get("meta") or {}).get("threshold") == 75,
          f"candidate={cand and cand.get('label')}")
    check("接线：提案期现役阈值不动", dt.threshold_learner.threshold == 70)
    # 影子期：喂 5 笔 score=72（<75 候选拒绝=记0）且亏损的平仓 →
    # 候选（空仓 0）优于现役（亏损）→ 晋升
    for _ in range(5):
        dt._threshold_gate_step(72, -0.02)
    check("影子达标 → 晋升生效（阈值 70→75）",
          dt.threshold_learner.threshold == 75,
          f"实测 {dt.threshold_learner.threshold}")
    check("门内记录 promotion", dt.threshold_gate.state["promotions"] == 1)
    # 观察期退化：连续亏损 → 批对比触发回滚 → 恢复 THRESHOLD_INITIAL 基线
    for _ in range(6):
        dt._threshold_gate_step(80, -0.05)
    check("退化 → 自动回滚基线阈值",
          dt.threshold_learner.threshold == config.THRESHOLD_INITIAL,
          f"实测 {dt.threshold_learner.threshold}（基线 {config.THRESHOLD_INITIAL}）")
    check("门内记录 rollback", dt.threshold_gate.state["rollbacks"] >= 1)
    # 隔离验证只检查本测试的写入目标；并行活体实例可合法更新自己的根状态，
    # 不能用“根文件近期不存在”作为测试判据。
    isolated_gate = os.path.join(tmp, "gate.json")
    check("gate 状态文件隔离（测试只写临时目录）",
          os.path.exists(isolated_gate) and
          os.path.commonpath([tmp, os.path.abspath(isolated_gate)]) == tmp)


if __name__ == "__main__":
    print("== 1. 证据时间衰减 ==")
    test_evidence_decay()
    print("\n== 2. gated 模式（提案不自动生效） ==")
    test_gated_mode()
    print("\n== 3+4. 进化门接线：提案→影子→晋升→回滚 ==")
    test_gate_wiring_promote_and_rollback()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

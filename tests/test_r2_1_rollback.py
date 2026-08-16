"""R2-1 离线单测：验证门回滚 → 权重回写基线（不触网）。

验证：退化触发回滚后（gate._rollback 命中 on_rollback 回调）
  1. wl.weights == base_weights；
  2. version 自增；
  3. rolled_back_at 已记录；
  4. weight_state 落盘基线（weights/version 持久化）。
"""
import os
import sys
import json
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from decision.weight_learning import WeightLearner


def test_rollback_restores_base_weights():
    base = {"a": 0.5, "b": 0.5}
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "weight_state.json")
        wl = WeightLearner(base, path=p, min_samples=999)
        # 重定向验证门状态文件到临时目录（避免污染仓库）
        wl.gate.path = os.path.join(d, "weight_gate.json")
        # 模拟权重已进化偏离基线
        wl.weights = {"a": 0.9, "b": 0.1}
        wl.version = 3
        wl._save()

        # 触发验证门回滚（等价于喂退化 pnl 后 _observe 判定退化 → _rollback）
        wl.gate._rollback()

        assert wl.weights == base, f"回滚后应回到 base_weights，实际 {wl.weights}"
        assert wl.version == 4, f"version 应自增到 4，实际 {wl.version}"
        assert wl.rolled_back_at is not None, "rolled_back_at 应被记录"

        # weight_state 落盘基线
        with open(p) as f:
            data = json.load(f)
        assert data["weights"] == base, f"落盘 weights 应为基线，实际 {data['weights']}"
        assert data["version"] == 4, f"落盘 version 应为 4，实际 {data['version']}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_rollback_restores_base_weights()
    print("R2-1 单测 1 项通过 ✅")

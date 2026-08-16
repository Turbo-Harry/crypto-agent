"""R1-3 离线单测：状态文件拆分 + 原子写（不触网）。

验证：
  1. ThresholdLearner._save() 原子写后无 .tmp 残留、状态文件为合法 JSON；
  2. WeightLearner._save() 原子写后无 .tmp 残留、状态文件为合法 JSON；
  3. 方向侧与套利侧 learner 状态文件路径互异（threshold_state_dir.json vs threshold_state_arb.json）。
"""
import os
import sys
import json
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from decision.threshold_learning import ThresholdLearner
from decision.weight_learning import WeightLearner

ROOT = os.path.dirname(os.path.abspath(__file__))


def test_threshold_atomic_write_no_tmp():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "threshold_state.json")
        tl = ThresholdLearner(path=p, min_samples=999)  # 避免触发 calibrate
        tl.record(70, 0.01)  # < min_samples → 直接 _save()
        assert os.path.exists(p), "状态文件应已写入"
        assert not os.path.exists(p + ".tmp"), "原子写后不应残留 .tmp"
        with open(p) as f:
            data = json.load(f)
        assert data["threshold"] == 70 and len(data["decisions"]) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_weight_atomic_write_no_tmp():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "weight_state.json")
        wl = WeightLearner({"a": 0.5}, path=p, min_samples=999)
        wl.record({"a": 80}, 0.01)  # < min_samples → maybe_evolve 早退，不触验证门
        assert os.path.exists(p), "状态文件应已写入"
        assert not os.path.exists(p + ".tmp"), "原子写后不应残留 .tmp"
        with open(p) as f:
            data = json.load(f)
        assert "weights" in data and "records" in data
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_learner_paths_differ():
    # 源码级断言两个调用方传入的状态文件路径互异（避免实例化触网）
    dir_src = open(os.path.join(ROOT, "directional_trader.py")).read()
    main_src = open(os.path.join(ROOT, "trading_main.py")).read()
    assert 'ThresholdLearner(path="threshold_state_dir.json")' in dir_src, \
        "方向侧应改用 threshold_state_dir.json"
    assert 'ThresholdLearner(path="threshold_state_arb.json")' in main_src, \
        "套利侧应改用 threshold_state_arb.json"
    assert "threshold_state_dir.json" != "threshold_state_arb.json"


if __name__ == "__main__":
    test_threshold_atomic_write_no_tmp()
    test_weight_atomic_write_no_tmp()
    test_learner_paths_differ()
    print("R1-3 单测 3 项全部通过 ✅")

"""R1-2 离线单测：套利平仓喂阈值学习（假 exchange / 假 learner，不触网）。

验证：
  1. 台账有 composite_score 快照 → threshold_learner.record 恰好一次，取快照值；
  2. 旧台账无 composite_score 快照 → threshold_learner 不 record，weight_learner 照喂。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import engines.trading_main as tm

tm.notify = lambda msg: None  # 屏蔽飞书通知


class FakeExchange:
    """假交易所：只支撑 _close_hedge 的对账 + 平仓下单。"""

    def __init__(self):
        self.orders = []

    def fetch_balance(self):
        return {"BTC": {"total": 0.001}}  # 现货多 0.001（spot_side=long 可平）

    def create_market_buy_order(self, sym, amount, params=None):
        self.orders.append(("buy", sym, amount))
        return {"id": "fake"}

    def create_market_sell_order(self, sym, amount, params=None):
        self.orders.append(("sell", sym, amount))
        return {"id": "fake"}


class FakeLearner:
    """假阈值/权重 learner：只记录 record 调用。"""

    def __init__(self):
        self.calls = []

    def record(self, *args):
        self.calls.append(args)


def make_obj():
    obj = object.__new__(tm.TradingMain)  # 绕过 __init__（不触网）
    obj.exchange = FakeExchange()
    obj.arb_positions = []
    obj._save_arb_positions = lambda: None
    obj.threshold_learner = FakeLearner()
    obj.weight_learner = FakeLearner()
    return obj


def _base_rec(**overrides):
    rec = {
        "base": "BTC", "amount": 0.001,
        "dir": "short",            # 合约腿（rate>0 → 合约空）
        "spot_side": "long",       # 现货腿（rate>0 → 现货多）
        "entry_sign": 1,
        "entry_rate": 0.0005,
        "scores": {"funding": 80},
        "opened_at": time.time() - 86400,  # 持有 1 天
        "flip_since": None,
    }
    rec.update(overrides)
    return rec


def test_with_snapshot_records_threshold_once():
    rec = _base_rec(composite_score=75.0)
    obj = make_obj()
    obj.arb_positions = [rec]
    obj._close_hedge(rec, "测试")
    assert len(obj.threshold_learner.calls) == 1, \
        f"有快照应喂阈值层恰好 1 次，实际 {len(obj.threshold_learner.calls)}"
    score, pnl = obj.threshold_learner.calls[0]
    assert score == 75.0, f"阈值喂入分数应取快照值 75.0，实际 {score}"
    assert isinstance(pnl, float), f"盈亏应为 float，实际 {type(pnl)}"
    assert len(obj.weight_learner.calls) == 1, "权重层仍应照喂 1 次"


def test_without_snapshot_skips_threshold():
    rec = _base_rec()  # 无 composite_score 键
    obj = make_obj()
    obj.arb_positions = [rec]
    obj._close_hedge(rec, "测试")
    assert len(obj.threshold_learner.calls) == 0, \
        f"无快照不应喂阈值层，实际 {len(obj.threshold_learner.calls)} 次"
    assert len(obj.weight_learner.calls) == 1, "无快照时权重层仍应照喂 1 次"


if __name__ == "__main__":
    test_with_snapshot_records_threshold_once()
    test_without_snapshot_skips_threshold()
    print("R1-2 单测 2 项全部通过 ✅")

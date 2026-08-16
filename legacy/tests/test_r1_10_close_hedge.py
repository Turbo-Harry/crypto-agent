"""R1-10 离线单测：套利平仓现货腿方向（假 exchange，不触网）。

验证：
  1. rate<0 台账（spot_side="short"）平仓时现货腿走【买入】回补，合约多腿走【卖出】；
  2. 旧台账无 spot_side 字段时按 entry_sign 兜底推导现货方向；
  3. spot_side=None（单腿）跳过现货腿，只平合约腿。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import engines.trading_main as tm

# 屏蔽飞书通知（离线测试）
tm.notify = lambda msg: None


class FakeExchange:
    """假交易所：只记录订单方向/标的/数量，balance 由构造传入。"""

    def __init__(self, base_balance):
        self.base_balance = base_balance  # {base: {"total": held}}，正=多、负=空
        self.orders = []

    def fetch_balance(self):
        return self.base_balance

    def create_market_buy_order(self, sym, amount, params=None):
        self.orders.append(("buy", sym, amount))
        return {"id": "fake"}

    def create_market_sell_order(self, sym, amount, params=None):
        self.orders.append(("sell", sym, amount))
        return {"id": "fake"}


def make_obj(base_balance):
    obj = object.__new__(tm.TradingMain)  # 绕过 __init__（不触网）
    obj.exchange = FakeExchange(base_balance)
    obj.arb_positions = []
    obj._save_arb_positions = lambda: None
    return obj


def test_rate_negative_closes_spot_by_buy():
    # rate<0 → spot_side="short"（现货空）→ 平仓应【买入】现货回补
    rec = {
        "base": "BTC", "amount": 0.001,
        "dir": "long",            # 合约腿（rate<0 → 合约多）
        "spot_side": "short",     # 现货腿（rate<0 → 现货空）
        "entry_sign": -1,
        "entry_rate": -0.0005,
        "scores": {},
        "opened_at": time.time(), "flip_since": None,
    }
    obj = make_obj({"BTC": {"total": -0.001}})  # 现货空 0.001
    obj.arb_positions = [rec]
    obj._close_hedge(rec, "测试")
    spot = [o for o in obj.exchange.orders if o[1] == "BTC/USDT"]
    swap = [o for o in obj.exchange.orders if o[1] == "BTC/USDT:USDT"]
    assert len(spot) == 1 and spot[0][0] == "buy", f"现货腿应买入回补，实际 {spot}"
    assert len(swap) == 1 and swap[0][0] == "sell", f"合约多腿应卖出平仓，实际 {swap}"
    assert spot[0][2] == 0.001, f"数量应为 min(amount, held)=0.001，实际 {spot[0][2]}"
    assert rec not in obj.arb_positions, "平仓成功后应从台账移除"


def test_old_ledger_entry_sign_fallback():
    # 旧台账无 spot_side：entry_sign>0 → spot_side="long" → 平仓【卖出】现货
    rec = {"base": "BTC", "amount": 0.001, "dir": "short",
           "entry_sign": 1, "entry_rate": 0.0005, "scores": {},
           "opened_at": time.time(), "flip_since": None}
    obj = make_obj({"BTC": {"total": 0.001}})
    obj.arb_positions = [rec]
    obj._close_hedge(rec, "测试")
    spot = [o for o in obj.exchange.orders if o[1] == "BTC/USDT"]
    assert len(spot) == 1 and spot[0][0] == "sell", \
        f"旧台账 entry_sign=1 应卖出现货，实际 {spot}"


def test_spot_side_none_skips_spot_leg():
    # spot_side=None（单腿，无现货腿）→ 跳过现货腿，只平合约腿
    rec = {"base": "BTC", "amount": 0.001, "dir": "short",
           "spot_side": None, "entry_sign": 1, "entry_rate": 0.0005,
           "scores": {}, "opened_at": time.time(), "flip_since": None}
    obj = make_obj({"BTC": {"total": 0.001}})
    obj.arb_positions = [rec]
    obj._close_hedge(rec, "测试")
    spot = [o for o in obj.exchange.orders if o[1] == "BTC/USDT"]
    swap = [o for o in obj.exchange.orders if o[1] == "BTC/USDT:USDT"]
    assert len(spot) == 0, f"spot_side=None 不应平现货腿，实际 {spot}"
    assert len(swap) == 1, f"应只平合约腿，实际 {swap}"


if __name__ == "__main__":
    test_rate_negative_closes_spot_by_buy()
    test_old_ledger_entry_sign_fallback()
    test_spot_side_none_skips_spot_leg()
    print("R1-10 单测 3 项全部通过 ✅")

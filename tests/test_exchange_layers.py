"""
单元测试 — 验证分层架构：
  1. FakeAdapter（内存假交易所）可注入策略层，离线跑通 信号→开仓→止损→平仓 全链路
  2. 数量对齐（floor_to_lot / ctVal 张数换算 / minSz 拒绝）正确

运行：python3 test_exchange_layers.py（无需网络、无需资金）
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from exchange.fake_adapter import FakeAdapter
from exchange.models import Candle, floor_to_lot
from exchange.base import ExchangeError

from engines.directional_trader import DirectionalTrader

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


def make_candles(n=100, base=100.0, drift=0.1):
    """构造 1h K线：缓涨趋势（EMA20>EMA50），最后几根回踩后拒绝（长下影）。"""
    out = []
    ts = 1_700_000_000_000
    for i in range(n):
        close = base + i * drift
        open_ = close - drift * 0.8
        out.append(Candle(ts=ts + i * 3600_000, open=open_, high=close + 0.5,
                          low=open_ - 0.5, close=close, volume=1000))
    # 最后一根：回踩 EMA20 不破 + 拒绝K线（长下影）
    last = out[-1]
    body = 0.4
    out[-1] = Candle(ts=last.ts, open=last.close - body, high=last.close,
                     low=last.close - body - 1.2, close=last.close - 0.05, volume=1000)
    return out


def test_quantity_helpers():
    print("== 数量对齐 ==")
    check("floor 0.55@0.001 = 0.55", floor_to_lot(0.55, 0.001) == 0.55)
    check("floor 0.5555@0.001 = 0.555", floor_to_lot(0.5555, 0.001) == 0.555)
    check("floor 1.7@1 = 1.0", floor_to_lot(1.7, 1) == 1.0)


def test_full_trade_flow():
    print("== 信号→开仓→止损→平仓 全链路（FakeAdapter 离线） ==")
    fake = FakeAdapter(usdt_free=10_000.0)
    base = "BTC"
    # 灌 K线：1h 缓涨 + 最后一根拒绝K线（长下影）→ 应出做多信号
    fake.candles["BTC-USDT-SWAP"] = make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0

    # 隔离持久化：临时 journal/账本/经验库/阈值/决策日志（绝不污染实盘状态文件）
    import tempfile
    tmp = tempfile.mkdtemp(prefix="tst_exch_")
    dt = DirectionalTrader(exchange=fake, rt=None,   # CI 安全：不启 WebSocket
                           db_path=os.path.join(tmp, "scan.db"))
    from execution.trade_journal import TradeJournal
    from execution.position_ownership import PositionLedger
    from decision.threshold_learning import ThresholdLearner
    from decision.experience_scoring import ScoredExperience
    dt.journal = TradeJournal(path=os.path.join(tmp, "journal.json"))
    dt.ledger = PositionLedger(path=os.path.join(tmp, "ledger.json"),
                               lock_path=os.path.join(tmp, "ledger.lock"))
    dt.threshold_learner = ThresholdLearner(path="test", db_path=os.path.join(tmp, "threshold.db"))
    dt.exp_bank = ScoredExperience(path=os.path.join(tmp, "exp.json"))
    dt.evolver.bank = __import__("engines.directional_trader", fromlist=["_ExpAdapter"])._ExpAdapter(dt.exp_bank)

    sig = dt.scan_signal(base)
    check("BTC 出做多信号", sig is not None and sig["dir"] == "long")
    if sig is None:
        print("    （信号为空，跳过后续断言）")
        return
    check("止损在入场下方", sig["stop"] < sig["entry"])
    check("止盈 = 2:1", abs(sig["tp"] - sig["entry"]) > abs(sig["stop"] - sig["entry"]))

    # 开仓（模拟盘 FakeAdapter 记账）
    tid = dt.open_position(base, sig, score=80)
    check("开仓成功并记账", tid is not None)
    check("FakeAdapter 记录了市价单", len(fake.orders) == 1 and fake.orders[0]["venue"] == "swap")
    # 2026-08-20 断言修正: FLAG_ENABLE_EXCHANGE_TP(08-19)后开仓挂两张条件单
    # (止损+止盈),旧断言写死"恰好1张"过时——按类型分别断言。
    _stops = [a for a in fake.algos if not a["is_tp"]]
    _tps = [a for a in fake.algos if a["is_tp"]]
    check("FakeAdapter 记录了止损条件单", len(_stops) == 1)
    check("交易所侧止盈与开关一致",
          len(_tps) == (1 if config.FLAG_ENABLE_EXCHANGE_TP else 0))
    check("交易所持仓腿存在", any(p.inst_id == "BTC-USDT-SWAP" and p.side == "long"
                                  and p.base_qty > 0 for p in fake.positions))
    check("账本总敞口 = 名义额", abs(dt.ledger.total_notional() - fake.orders[0]["qty"] * 110.0) < 1.0)

    # 价格击穿止损 → monitor 平仓
    fake.last_prices["BTC-USDT-SWAP"] = sig["stop"] * 0.99
    dt.monitor()
    open_trades = [t for t in dt.journal.trades if t["status"] == "open"]
    check("止损触发后 journal 无未平仓", len(open_trades) == 0)
    check("平仓单为 reduceOnly 卖出", any(o["side"] == "sell" and o["reduce_only"]
                                          for o in fake.orders))
    check("止损条件单已撤销", len(fake.algos) == 0)
    check("FakeAdapter 持仓腿已清空", all(p.base_qty <= 0 for p in fake.positions))
    check("账本敞口已释放", abs(dt.ledger.total_notional()) < 1e-9)


def test_min_size_reject():
    print("== 最小下单量拒绝 ==")
    fake = FakeAdapter(usdt_free=10_000.0)
    fake.last_prices["ANTHROPIC-USDT-SWAP"] = 180.0
    # ANTHROPIC min_sz=1（1 张 = 1 币），0.5 币 < 1 张 → 应拒绝（ExchangeError）
    try:
        fake.place_market_order("ANTHROPIC-USDT-SWAP", "buy", 0.5, venue="swap",
                                pos_side="long")
        check("0.5 币 < min_sz 被拒绝", False)
    except ExchangeError as e:
        check("0.5 币 < min_sz 被拒绝", "最小" in str(e))
    res = fake.place_market_order("ANTHROPIC-USDT-SWAP", "buy", 1.0, venue="swap",
                                  pos_side="long")
    check("1.0 币（=1 张）可下单", res.ok)


if __name__ == "__main__":
    test_quantity_helpers()
    test_min_size_reject()
    test_full_trade_flow()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

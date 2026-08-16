"""
P0 审计修复行为测试(离线,FakeAdapter + 临时库,不碰生产状态):
  1. PositionLedger.release 归零后物理 DELETE —— 重启后幽灵 claim 不复活(审计 H2)
  2. PositionLedger.reconcile —— 不在事实源内的 claim 被物理释放(审计 C1)
  3. _recover_order —— 下单异常但反查已成交 → ok=True 继续记账/挂止损(审计 C1)
  4. open_position 端到端 —— 订单带 clOrdId、真实成交价记账、claim 落账
运行: PYTHONPATH=lib python3 tests/test_p0_fixes.py
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from exchange.base import ExchangeError
from exchange.fake_adapter import FakeAdapter
from engines.directional_trader import DirectionalTrader
from execution.position_ownership import PositionLedger

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def test_release_deletes_row():
    tmp = tempfile.mkdtemp(prefix="p0_ledger_")
    db = os.path.join(tmp, "ledger.db")
    pl = PositionLedger(path=db, lock_path=os.path.join(tmp, "l.lock"))
    ok, reason = pl.claim("BTC/USDT:USDT", "long", "dir", 1.0, 100.0)
    check("claim 成功", ok, reason)
    pl.release("BTC/USDT:USDT", "long", "dir", 1.0, 100.0)
    # 重启语义:重新实例化(重新 _load)后不得读回幽灵
    pl2 = PositionLedger(path=db, lock_path=os.path.join(tmp, "l.lock"))
    check("release 后重启不复活(总敞口=0)", pl2.total_notional() == 0.0,
          f"实际 {pl2.total_notional()}")
    check("release 后 DB 无残留行",
          len(pl2._data) == 0, f"实际 {pl2._data}")


def test_reconcile_releases_ghost():
    tmp = tempfile.mkdtemp(prefix="p0_rec_")
    db = os.path.join(tmp, "ledger.db")
    pl = PositionLedger(path=db, lock_path=os.path.join(tmp, "l.lock"))
    pl.claim("BTC/USDT:USDT", "long", "dir", 1.0, 100.0)
    pl.claim("ETH/USDT:USDT", "long", "dir", 1.0, 200.0)
    # 事实源:交易所只剩 ETH long
    released = pl.reconcile({"ETH/USDT:USDT:long"})
    check("幽灵 BTC claim 被释放", released == ["BTC/USDT:USDT:long"],
          f"实际 {released}")
    check("总敞口只剩 ETH", abs(pl.total_notional() - 200.0) < 1e-9,
          f"实际 {pl.total_notional()}")


def _make_trader():
    fake = FakeAdapter(usdt_free=10_000.0)
    trader = DirectionalTrader(exchange=fake, rt=None)
    tmp = tempfile.mkdtemp(prefix="p0_trd_")
    from execution.trade_journal import TradeJournal
    trader.journal = TradeJournal(path=os.path.join(tmp, "journal.db"))
    trader.ledger = PositionLedger(path=os.path.join(tmp, "ledger.db"),
                                   lock_path=os.path.join(tmp, "l.lock"))
    return trader, fake, tmp


def test_recover_order_filled():
    trader, fake, _ = _make_trader()
    fake.fetch_order_state = lambda iid, cid: {
        "state": "filled", "avg_px": 99.5, "ord_id": "f99"}
    res = trader._recover_order("X-USDT-SWAP", "ca-test", 1.0,
                                ExchangeError("网络错误: 超时"))
    check("反查已成交 → ok=True", res.ok)
    check("携带 ord_id", res.ord_id == "f99")
    trader2, fake2, _ = _make_trader()
    fake2.fetch_order_state = lambda iid, cid: None
    res2 = trader2._recover_order("X-USDT-SWAP", "ca-test2", 1.0,
                                  ExchangeError("网络错误: 超时"))
    check("反查无结果 → fail-closed ok=False", not res2.ok)


def test_open_position_e2e():
    trader, fake, _ = _make_trader()
    sig = {"dir": "long", "entry": 100.0, "stop": 95.0, "tp": 110.0, "atr": 5.0}
    tid = trader.open_position("ANTHROPIC", sig, score=80)
    check("开仓成功返回 tid", bool(tid), f"实际 {tid}")
    check("订单带 clOrdId", fake.orders and fake.orders[-1].get("cl_ord_id", "").startswith("ca-"),
          f"实际 {fake.orders[-1] if fake.orders else None}")
    open_trades = [t for t in trader.journal.trades if t["status"] == "open"]
    check("journal 有未平仓记录", len(open_trades) == 1, f"实际 {len(open_trades)}")
    if open_trades:
        # 真实成交价记账:fake 成交价 180.0(非信号价 100.0)
        check("entry_price=真实成交价 180.0",
              abs(open_trades[0]["entry_price"] - 180.0) < 1e-9,
              f"实际 {open_trades[0]['entry_price']}")
    check("账本有 claim", trader.ledger.total_notional() > 0,
          f"实际 {trader.ledger.snapshot()}")


if __name__ == "__main__":
    print("== P0 修复行为测试 ==")
    test_release_deletes_row()
    test_reconcile_releases_ghost()
    test_recover_order_filled()
    test_open_position_e2e()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

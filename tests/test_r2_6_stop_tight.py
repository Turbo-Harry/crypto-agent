"""R2-6 离线单测：止损复盘参数（atr_value/signal_price 接线），不触网。

验证：
  1. 止损距 < 1×ATR 的亏损单 → deep_review 产出"止损太紧"教训；
  2. 旧记录 atr_value/signal_price 为 None → deep_review 不崩；
  3. trade_journal.log_entry 存 atr_value/signal_price，未传时默认 None。
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.review_engine import deep_review
from execution.trade_journal import TradeJournal


def test_stop_too_tight_lesson():
    # entry 100 / stop 99 → stop_dist=1%；atr=5 → atr_pct=5% → stop_in_atr=0.2 < 1.0
    trade = {"entry_price": 100.0, "exit_price": 98.0, "stop_loss": 99.0,
             "take_profit": 110.0, "pnl": -0.02, "direction": "long"}
    report = deep_review(trade, atr_value=5.0, signal_price=100.0)
    tight = [l for l in report["lessons"]
             if l["category"] == "止损" and "太紧" in l["lesson"]]
    assert tight, f"止损<1×ATR 亏损单应产出'止损太紧'教训，实际教训: {report['lessons']}"


def test_old_record_none_no_crash():
    # 旧记录无 atr_value/signal_price（None）→ deep_review 正常返回，不抛异常
    trade = {"entry_price": 100.0, "exit_price": 102.0, "stop_loss": 99.0,
             "take_profit": 110.0, "pnl": 0.02, "direction": "long"}
    report = deep_review(trade)  # atr_value=None, signal_price=None
    assert "lessons" in report and "pnl" in report


def test_journal_stores_atr_signal_price():
    d = tempfile.mkdtemp()
    try:
        j = TradeJournal(path=os.path.join(d, "journal.json"))
        tid = j.log_entry("BTC", "回踩确认", "r", 100, 99, 110, 1,
                          atr_value=5.0, signal_price=100.0)
        t = next(x for x in j.trades if x["id"] == tid)
        assert t["atr_value"] == 5.0 and t["signal_price"] == 100.0

        tid2 = j.log_entry("ETH", "回踩确认", "r", 100, 99, 110, 1)
        t2 = next(x for x in j.trades if x["id"] == tid2)
        assert t2["atr_value"] is None and t2["signal_price"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_stop_too_tight_lesson()
    test_old_record_none_no_crash()
    test_journal_stores_atr_signal_price()
    print("R2-6 单测 3 项全部通过 ✅")

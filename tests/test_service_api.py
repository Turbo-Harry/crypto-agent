"""
服务层单测 — FastAPI TestClient + FakeAdapter（离线，不连交易所）。

验证：
  1. 全部观测接口返回 200 且 schema 正确（Pydantic 响应模型生效）
  2. /pause /resume 控制流
  3. 引擎 tick 可被手动驱动（心跳文件写入）
运行：PYTHONPATH=lib python3 test_service_api.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # tests 目录（import test_exchange_layers）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import config

from fastapi.testclient import TestClient
from service.app import app
from service.worker import ServiceTrader
from exchange.fake_adapter import FakeAdapter

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


class _FakeWorker:
    """CI 安全的假 worker：只提供 app 层读取的属性，不连 OKX/WS。"""

    def __init__(self, trader):
        self.trader = trader
        self.last_hb_dir = time.time()
        self.started_at = time.time()

    def heartbeat_age(self):
        return time.time() - self.last_hb_dir

    def uptime(self):
        return time.time() - self.started_at


def main():
    # 组装：FakeAdapter + ServiceTrader（离线）+ TestClient
    fake = FakeAdapter(usdt_free=10_000.0)
    # 隔离持久化（临时 journal/账本，不碰活体服务状态文件）
    import tempfile
    tmp = tempfile.mkdtemp(prefix="tst_svc_")
    # Phase0 T0.4：ServiceTrader 也必须传 db_path——本测试会 tick() 触发
    # scan_signals，不隔离会把测试决策行写进生产 scan_decisions（DEF-8）。
    trader = ServiceTrader(exchange=fake, rt=None,
                           db_path=os.path.join(tmp, "scan.db"))
    trader.last_hb = time.time()
    from execution.trade_journal import TradeJournal
    from execution.position_ownership import PositionLedger
    from decision.threshold_learning import ThresholdLearner
    from decision.experience_scoring import ScoredExperience
    trader.journal = TradeJournal(path=os.path.join(tmp, "journal.json"))
    trader.ledger = PositionLedger(path=os.path.join(tmp, "ledger.json"),
                                   lock_path=os.path.join(tmp, "ledger.lock"))
    trader.threshold_learner = ThresholdLearner(path="test", db_path=os.path.join(tmp, "threshold.db"),
                                                initial_threshold=config.THRESHOLD_INITIAL)
    trader.exp_bank = ScoredExperience(path=os.path.join(tmp, "exp.json"))
    worker = _FakeWorker(trader)
    app.state.worker = worker
    trader._last_scan = 0
    trader._last_risk_update = 0
    trader.signal_cool = {}

    # base_url=127.0.0.1:让 Host 头落在控制面白名单内(审计 B-H1 的 Host 校验)
    client = TestClient(app, base_url="http://127.0.0.1")
    print("== 聚合冒烟（全部只读端点一次遍历）==")
    smoke_ok, smoke_detail = True, []
    for path in ("/health", "/status", "/watchlist", "/journal", "/realtime/FAKE",
                 "/error", "/analysis/latest", "/reconcile", "/scan/evolve",
                 "/agent/status", "/agent/runs", "/agent/evaluation"):
        r = client.get(path)
        try:
            r.json()
            ok = r.status_code == 200
        except Exception:
            ok = False
        if not ok:
            smoke_ok = False
            smoke_detail.append(f"{path}→{r.status_code}")
    check("12 个只读端点全部 200+JSON 可解析", smoke_ok)
    if smoke_detail:
        print(f"    失败明细: {smoke_detail}")
    r = client.get("/scan/evolve")
    check("/scan/evolve 含 effective_wick 且 needs_approval=false",
          r.status_code == 200 and "effective_wick" in r.json()
          and r.json()["needs_approval"] is False)
    check("/agent/status 返回运行健康字段",
          client.get("/agent/status").status_code == 200
          and "failure_rate" in client.get("/agent/status").json())
    check("/agent/runs 只读查询返回 runs",
          client.get("/agent/runs").status_code == 200
          and "runs" in client.get("/agent/runs").json())
    check("/agent/evaluation 返回成熟度统计",
          client.get("/agent/evaluation").status_code == 200
          and "incremental_ev" in client.get("/agent/evaluation").json())
    r = client.post("/scan/evolve/approve")
    check("无验证通过提案时 /scan/evolve/approve → 409",
          r.status_code == 409)
    if smoke_detail:
        print(f"    失败明细: {smoke_detail}")

    print("== 观测接口（行为断言）==")
    r = client.get("/status")
    j = r.json()
    check("/status 余额来自 FakeAdapter（usdt_free=10000）",
          j["balance"]["usdt_free"] == 10000.0)
    check("/status 含投注统计字段（total/open/today notional）",
          all(k in j for k in ("total_notional_usdt", "open_notional_usdt", "today_notional_usdt")))
    r = client.get("/realtime/FAKE")
    check("/realtime rt=None 时 fresh=false 不崩", r.json()["fresh"] is False)
    r = client.post("/analysis/daily")
    j = r.json()
    check("/analysis/daily 跑通（含 report/issues）",
          r.status_code == 200 and "report" in j and "issues" in j)
    r = client.get("/analysis/latest")
    check("/analysis/latest 触发后有报告", r.status_code == 200 and r.json()["report"] is not None)
    # /signals/{base}：FakeAdapter 灌 K 线出信号（复用 test_exchange_layers 的构造）
    from test_exchange_layers import make_candles
    fake.candles["BTC-USDT-SWAP"] = make_candles()
    fake.last_prices["BTC-USDT-SWAP"] = 110.0
    fake.last_prices["BTC-USDT"] = 110.0
    r = client.get("/signals/BTC")
    j = r.json()
    check("/signals/BTC 出做多信号（引擎逻辑走通）",
          r.status_code == 200 and j["signal"] and j["signal"]["dir"] == "long")

    print("== 控制接口 ==")
    r = client.post("/pause")
    check("/pause → paused=true", r.status_code == 200 and r.json()["paused"] is True)
    check("trader.paused 同步", trader.paused is True)
    # 暂停态下手动 tick：scan_signals 被跳过（不会崩溃、不下单）
    trader.tick()
    check("暂停态 tick 无异常且未下单", len(fake.orders) == 0)
    r = client.post("/resume")
    check("/resume → paused=false", r.status_code == 200 and r.json()["paused"] is False)
    # 把 BTC 放入候选池再 tick（并压住每日刷新：refresh 时间戳+日期都置为当前）
    trader.watchlist = ["BTC"]
    trader.watch_scores = {"BTC": 0.9}
    trader._watch_date = time.strftime("%Y-%m-%d")
    trader._last_watch_refresh = time.time()
    trader._last_scan = 0   # 重置扫描计时（模拟 15min 已过）
    trader.signal_cool = {}
    trader.tick()
    check("恢复态 tick 后可开仓（FakeAdapter 记录市价单）",
          len(fake.orders) >= 1 and fake.orders[0]["venue"] == "swap")
    # 投注额已落盘：journal 新交易带 notional_usdt / risk_usdt，API 统计非零
    opened = [x for x in trader.journal.trades if x["status"] == "open"]
    check("journal 记录投注额 notional_usdt", opened and opened[-1].get("notional_usdt", 0) > 0)
    check("journal 记录风险额 risk_usdt", opened and opened[-1].get("risk_usdt", 0) > 0)
    r = client.get("/journal")
    jr = r.json()
    check("/journal 交易项含 notional_usdt",
          jr["trades"] and jr["trades"][0].get("notional_usdt") is not None)
    r = client.get("/status")
    check("/status 未平仓投注额 > 0", r.json()["open_notional_usdt"] > 0)
    # 对账：journal 记账 vs 交易所持仓（FakeAdapter 记账一致 → balanced=true）
    r = client.get("/reconcile")
    rc = r.json()
    check("/reconcile 200 且 balanced", r.status_code == 200 and rc["balanced"] is True)
    check("/reconcile per_symbol 含 BTC", any(p["symbol"] == "BTC" for p in rc["per_symbol"]))

    # 心跳文件（tick 会写 heartbeat_directional.txt）
    check("tick 写心跳文件", os.path.exists("heartbeat_directional.txt")
          and time.time() - float(open("heartbeat_directional.txt").read().strip()) < 30)

    # 旧记录 legacy 单位回填：写入一张 legacy journal 记录（0.5 张 ETH），
    # 回填后 notional = 0.5 × ctVal(0.1) × entry，且标 size_unit
    legacy_path = os.path.join(tmp, "legacy.json")
    import json as _json
    with open(legacy_path, "w") as f:
        _json.dump({"trades": [{"id": "txn_legacy", "symbol": "ETH", "size": 0.5,
                                "entry_price": 1885.0, "stop_loss": 1844.0,
                                "take_profit": 1968.0, "status": "open",
                                "direction": "long"}], "lessons": []}, f)
    j2 = TradeJournal(path=legacy_path)
    leg = j2.trades[0]
    check("legacy 回填 notional（张×ctVal×价）",
          abs(leg.get("notional_usdt") - 0.5 * 0.1 * 1885.0) < 0.01)
    check("legacy 标 size_unit", leg.get("size_unit") == "contracts(legacy)")

    # 复盘报告落盘：save_review 写入 journal + API /journal 返回 review 字段
    report = {"pnl": 0.02, "rr": 2.0,
              "lessons": [{"category": "入场时机", "lesson": "测试教训"}]}
    tid0 = trader.journal.trades[0]["id"]
    check("save_review 返回 True", trader.journal.save_review(tid0, report) is True)
    check("journal 落盘 review", trader.journal.trades[0].get("review") == report)
    r = client.get("/journal")
    jr = r.json()
    check("/journal 交易项含 review 复盘报告", jr["trades"][0].get("review") is not None)

    # 平仓后总盈亏写实际 USDT（比例 × 名义），不是百分比相加
    from execution.trade_journal import realized_pnl_usdt as _pnl_u
    row0 = trader.journal.trades[0]
    entry = float(row0["entry_price"] or 0)
    notional = float(row0.get("notional_usdt") or 0)
    trader.journal.log_exit(tid0, entry * 1.02, "测试止盈")
    r = client.get("/journal")
    jr = r.json()
    expected = _pnl_u(trader.journal.trades[0])
    item = jr["trades"][0]
    check("/journal 单笔含 pnl_usdt", item.get("pnl_usdt") == expected)
    check("/journal 总盈亏 total_pnl_usdt 为实际 USDT",
          jr.get("total_pnl_usdt") == expected)
    check("总盈亏约等于名义×2%（不是把 2% 写成 2）",
          expected is not None and abs(expected - 0.02 * notional) < 0.05)

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

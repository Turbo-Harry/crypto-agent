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

# 本脚本单独运行时也必须与 CI 一样锁定 paper；必须发生在 import config 前，
# 否则心跳实例名已按默认模式冻结，测试命令会产生与代码无关的假红。
os.environ["CRYPTO_AGENT_MODE"] = "paper"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # tests 目录（import test_exchange_layers）

import config

from fastapi.testclient import TestClient
from service.app import create_app
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
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1
        return True

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
    os.environ["CRYPTO_AGENT_RUNTIME_DIR"] = tmp
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
    # 把 storage 默认路径指向哨兵库：任何端点漏传 trader._db_path 都会在
    # 哨兵留下行，测试可直接捕获，且无论如何都不会碰生产库。
    import storage.db as sdb
    sentinel_db = os.path.join(tmp, "default_path_sentinel.db")
    sdb.init_db(sentinel_db)
    original_default_db = sdb.DB_PATH
    sdb.DB_PATH = sentinel_db
    worker = _FakeWorker(trader)
    app = create_app(worker_factory=lambda: worker)
    trader._last_scan = 0
    trader._last_risk_update = 0
    trader.signal_cool = {}

    # base_url=127.0.0.1:让 Host 头落在控制面白名单内(审计 B-H1 的 Host 校验)
    check("app factory 每次返回独立实例",
          app is not create_app(worker_factory=lambda: worker))
    check("lifespan 前未创建 worker", not hasattr(app.state, "worker"))
    client = TestClient(app, base_url="http://127.0.0.1")
    client.__enter__()
    check("lifespan 启动 worker 且注入 app.state",
          worker.start_calls == 1 and app.state.worker is worker)

    original_web_concurrency = os.environ.get("WEB_CONCURRENCY")
    os.environ["WEB_CONCURRENCY"] = "2"
    guarded_worker = _FakeWorker(trader)
    guard_factory_calls = []

    def guard_factory():
        guard_factory_calls.append(1)
        return guarded_worker

    try:
        try:
            with TestClient(create_app(worker_factory=guard_factory),
                            base_url="http://127.0.0.1"):
                pass
            rejected_multi_worker = False
        except RuntimeError:
            rejected_multi_worker = True
    finally:
        if original_web_concurrency is None:
            os.environ.pop("WEB_CONCURRENCY", None)
        else:
            os.environ["WEB_CONCURRENCY"] = original_web_concurrency
    check("单 worker 守卫在创建 worker 前拒绝 WEB_CONCURRENCY=2",
          rejected_multi_worker and not guard_factory_calls
          and guarded_worker.start_calls == 0)

    print("== 聚合冒烟（全部只读端点一次遍历）==")
    smoke_ok, smoke_detail = True, []
    for path in ("/health", "/status", "/watchlist", "/journal", "/realtime/FAKE",
                 "/error", "/analysis/latest", "/reconcile", "/scan/evolve",
                 "/agent/status", "/agent/runs", "/agent/proposals",
                 "/agent/evaluation",
                 "/models/entry", "/forecast/calibration", "/factors/trials",
                 "/research/readiness",
                 ):
        r = client.get(path)
        try:
            r.json()
            ok = r.status_code == 200
        except Exception:
            ok = False
        if not ok:
            smoke_ok = False
            smoke_detail.append(f"{path}→{r.status_code}")
    check("17 个只读端点全部 200+JSON 可解析", smoke_ok)
    if smoke_detail:
        print(f"    失败明细: {smoke_detail}")
    r = client.get("/scan/evolve")
    check("/scan/evolve 含 effective_wick 且 needs_approval=false",
          r.status_code == 200 and "effective_wick" in r.json()
          and r.json()["needs_approval"] is False)
    sdb.x(
        "INSERT INTO agent_runs "
        "(run_id,signal_id,idempotency_key,created_ts,runtime_status,"
        "final_action,prompt_version,tool_policy_version) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ["status-run", "status-signal", "status-key", time.time(),
         "completed", "agent_abstain", config.AGENT_HARNESS_PROMPT_VERSION,
         config.AGENT_HARNESS_TOOL_POLICY_VERSION], db_path=trader._db_path)
    sdb.x(
        "INSERT INTO agent_evaluations (run_id,lifecycle_status) VALUES (?,?)",
        ["status-run", "pending"], db_path=trader._db_path)
    sdb.x(
        "INSERT INTO agent_versions "
        "(version,strategy_id,role,status,created_ts) VALUES (?,?,?,?,?)",
        ["mature-version", config.ENTRY_SIGNAL_STRATEGY_ID, "champion",
         "observing", time.time()], db_path=trader._db_path)
    agent_status = client.get("/agent/status")
    agent_body = agent_status.json()
    check("/agent/status 分开返回配置、最新 run 与成熟生命周期身份",
          agent_status.status_code == 200
          and agent_body["failure_rate"] == 0.0
          and agent_body["configured_prompt_version"] ==
              config.AGENT_HARNESS_PROMPT_VERSION
          and agent_body["configured_tool_policy_version"] ==
              config.AGENT_HARNESS_TOOL_POLICY_VERSION
          and agent_body["latest_run_prompt_version"] ==
              config.AGENT_HARNESS_PROMPT_VERSION
          and agent_body["latest_run_tool_policy_version"] ==
              config.AGENT_HARNESS_TOOL_POLICY_VERSION
          and agent_body["latest_run_lifecycle_status"] == "pending"
          and agent_body["lifecycle_version"] == "mature-version"
          and agent_body["lifecycle_status"] == "observing"
          and agent_body["veto_enabled"] is True)
    check("/agent/runs 只读查询返回 runs",
          client.get("/agent/runs").status_code == 200
          and "runs" in client.get("/agent/runs").json())
    proposal_response = client.get("/agent/proposals")
    proposal_body = proposal_response.json()
    check("/agent/proposals 明确只读 shadow、协议覆盖率且无执行权限",
          proposal_response.status_code == 200
          and proposal_body["shadow_only"] is True
          and proposal_body["execution_authority"] is False
          and proposal_body["current_protocol_version"] ==
              config.AGENT_PROPOSAL_IMPLEMENTATION_VERSION
          and proposal_body["auditable_run_count"] == 0
          and proposal_body["current_protocol_proposal_coverage"] == 0.0)
    watch = client.get("/watchlist").json()
    check("/watchlist 分开返回加密/美股候选池",
          "crypto_items" in watch and "stock_items" in watch
          and all(item["pool"] == "crypto" for item in watch["crypto_items"])
          and all(item["pool"] == "stock" for item in watch["stock_items"]))
    check("/agent/evaluation 返回成熟度统计",
          client.get("/agent/evaluation").status_code == 200
          and "incremental_ev" in client.get("/agent/evaluation").json()
          and "incremental_ev_lower_bound" in
          client.get("/agent/evaluation").json()["harness"])
    check("/models/entry 默认无模型且禁止扩大预算",
          client.get("/models/entry").json() ==
          {"models": [], "budget_expansion_allowed": False})
    check("/forecast/calibration 无样本明确 uncalibrated",
          client.get("/forecast/calibration").json()["status"] == "uncalibrated")
    check("/factors/trials 返回结构化列表",
          "trials" in client.get("/factors/trials").json())
    research_readiness = client.get("/research/readiness").json()
    check("/research/readiness 不把空测试库冒充统计完成",
          research_readiness["statistically_complete"] is False
          and research_readiness["counts"]["paper_closed"] == 0
          and research_readiness["counts"]["raw_candidate_snapshots"] == 0
          and research_readiness["counts"]["duplicate_version_snapshots"] == 0
          and research_readiness["budget"]["expansion_allowed"] is False)
    breakout_readiness = client.get(
        "/research/readiness?strategy_id=B_breakout")
    check("/research/readiness 可按 B 策略独立审计",
          breakout_readiness.status_code == 200
          and breakout_readiness.json()["scope"]["strategy_id"] == "B_breakout"
          and breakout_readiness.json()["counts"]["paper_closed"] == 0)
    check("/research/readiness 拒绝未知策略",
          client.get("/research/readiness?strategy_id=unknown").status_code == 422)
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
    check("/realtime 无盘口流时显式返回 missing 而非伪值",
          r.json()["orderflow_status"] == "missing" and
          r.json()["ofi_event_multilevel"] is None and
          r.json()["ofi_event_count"] == 0)
    r = client.post("/analysis/daily")
    j = r.json()
    check("/analysis/daily 跑通（含 report/issues）",
          r.status_code == 200 and "report" in j and "issues" in j)
    r = client.get("/analysis/latest")
    check("/analysis/latest 触发后有报告", r.status_code == 200 and r.json()["report"] is not None)
    check("分析报告写入 trader 隔离库",
          sdb.q1("SELECT COUNT(*) n FROM analyses", db_path=trader._db_path)["n"] == 1)
    check("服务端点没有回落到默认数据库",
          sdb.q1("SELECT COUNT(*) n FROM analyses", db_path=sentinel_db)["n"] == 0)
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
    check("journal 记录策略身份 strategy_id",
          opened and opened[-1].get("strategy_id") ==
          config.ENTRY_SIGNAL_STRATEGY_ID)
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

    # paper 模式必须读本测试运行目录里的 heartbeat_paper，不能误读活体
    # heartbeat_directional 形成假绿。
    heartbeat = os.path.join(tmp, "heartbeat_paper.txt")
    check("tick 写入隔离的 paper 心跳文件", os.path.exists(heartbeat)
          and time.time() - float(open(heartbeat).read().strip()) < 30)

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

    print("== 日内研究生产调度 ==")
    from unittest.mock import patch
    from service.worker import (run_intraday_research_cycle,
                                _intraday_research_retry_marker,
                                _record_background_failure)
    calls = []

    def fake_mining(db_path=None, strategy_id=None):
        calls.append(("factor", strategy_id, db_path))
        return [{"status": "insufficient_data"}]

    def fake_entry(direction, db_path=None, strategy_id=None):
        calls.append(("entry", strategy_id, direction))
        return {"status": "insufficient_data"}

    def fake_extrema(direction, db_path=None, strategy_id=None):
        calls.append(("extrema", strategy_id, direction))
        return {"status": "insufficient_data"}

    with patch("factors.intraday_factor_mining.run_mining", fake_mining), \
            patch("factors.entry_model_training.train_entry_model", fake_entry), \
            patch("factors.extrema_model_training.train_extrema_model", fake_extrema), \
            patch("decision.model_lifecycle.advance", return_value=[]):
        research = run_intraday_research_cycle(os.path.join(tmp, "research.db"))
    expected_strategies = {config.ENTRY_SIGNAL_STRATEGY_ID,
                           config.BREAKOUT_SIGNAL_STRATEGY_ID,
                           config.AGENT_PROPOSAL_STRATEGY_ID}
    check("paper worker 自动研究 A/B/AI提案三个策略且证据隔离",
          {row["strategy_id"] for row in research["strategies"]} ==
          expected_strategies and not research["errors"] and
          {(kind, sid) for kind, sid, _ in calls if kind == "factor"} ==
          {("factor", sid) for sid in expected_strategies})
    check("每个策略分别训练 long/short 的概率与极值模型",
          all(sum(1 for kind, strategy_id, direction in calls
                  if kind == model_type and strategy_id == sid and
                  direction in ("long", "short")) == 2
              for model_type in ("entry", "extrema")
              for sid in expected_strategies))
    error_db = os.path.join(tmp, "research-error.db")
    error_trader = type("ErrorTrader", (), {
        "_db_path": error_db, "last_error": ""})()
    try:
        raise RuntimeError("research failed")
    except RuntimeError as exc:
        _record_background_failure(error_trader, "intraday_research", exc)
    research_error = sdb.q1(
        "SELECT engine,error FROM engine_errors ORDER BY id DESC LIMIT 1",
        db_path=error_db)
    check("日内研究失败进入 /error 与隔离事件库而非静默 24h",
          "research failed" in error_trader.last_error and
          research_error and research_error["engine"] == "intraday_research")
    retry_marker = _intraday_research_retry_marker(now=1_000_000)
    next_due = retry_marker + config.FACTOR_MINING_INTERVAL_HOURS * 3600
    check("日内研究失败按配置退避 15 分钟而非等待下一天",
          next_due - 1_000_000 == config.FACTOR_MINING_RETRY_SECONDS)

    client.__exit__(None, None, None)
    check("lifespan 退出仅停止 worker 一次", worker.stop_calls == 1)
    sdb.DB_PATH = original_default_db
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

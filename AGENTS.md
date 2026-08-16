# AGENTS.md — 本仓库的 AI 协作模型

> 供 AI agent / 新协作者阅读。读本文即可理解"这个仓库是什么、怎么跑、能动什么、不能动什么"。

## 1. 这是什么

自动化加密货币交易系统（OKX 模拟盘，虚拟资金）。
两条策略线：**方向性日内短线**（活跃）与**资金费率套利**（用户已停用，代码保留）。

核心哲学（用户反复强调，不可违背）：
- 宁可做对，也不做错；空仓是默认，持仓是例外
- 抓最佳时机进出，不频繁交易
- 防止过拟合；不夸大收益；诚实地报亏损
- 小仓位慢跑（单笔风险 1%，名义 ≤150 USDT）

## 2. 分层架构（物理分层已落地）

```
service/            服务端外壳（FastAPI + uvicorn，完整功能唯一入口）
  ├─ main.py        进程入口：启动双引擎 + HTTP
  ├─ app.py         HTTP 接口层（观测/控制/运维，禁止下单接口）
  ├─ models.py      Pydantic 响应模型（AI 可读 schema，自动进 /docs）
  └─ worker.py      引擎托管：两后台线程 + 共享 WS + watchdog 心跳

engines/           交易引擎层
  ├─ directional_trader.py  方向性引擎（回踩确认 + 2:1 盈亏比 + tick 止损）
  ├─ trading_main.py        套利引擎（事件驱动 + 费率告警 + 持仓管理）
  ├─ trading_agent.py       旧套利 agent（保留）
  ├─ funding_arb.py         套利 CLI（保留）
  └─ daily_scan.py          每日全市场候选扫描 → watchlist.json

decision/          决策与进化层
  ├─ self_evolving_trader.py  决策进化（综合经验库做开仓决策）
  ├─ experience_scoring.py    经验评分库
  ├─ threshold_learning.py    阈值自适应
  ├─ weight_learning.py       权重自进化
  ├─ scoring.py               套利评分体系
  ├─ review_engine.py         复盘引擎
  └─ evolution_gate.py        进化验证门

execution/         执行与台账层
  ├─ quantity.py              名义→数量换算（lotSz 对齐）
  ├─ trade_journal.py         交易台账（原子写）
  └─ position_ownership.py    持仓所有权账本（flock + 总敞口≤600）

exchange/          交易所访问四层（见 docs/architecture/exchange_layers.md）
  transport.py     OKX 原生 REST：HMAC 签名/模拟盘/限速/错误归一
  okx_adapter.py   单位换算(ctVal/lotSz)/场所探测/响应翻译
  base.py          抽象接口 ExchangeAdapter + ExchangeError
  models.py        领域模型 + floor_to_lot
  fake_adapter.py  内存假交易所（单测注入）

factors/           因子挖掘研究层（factor_discovery/evolution/mining）
tools/             工具脚本（scan.py / paper_trade.py / okx_pg_ingest.py / watchdog.py）
data/              数据源（fetch_okx / fetch_* / realtime_okx / economic_calendar）
strategy/  risk/  backtest/   指标 / 风控 / 回测
tests/             全部测试（test_exchange_layers.py / test_service_api.py / test_r*）
docs/              文档中心（architecture/plans/reports/ops/prompts，索引见 docs/README.md）
legacy/            废弃文件（trading_daemon.py.legacy）
config.py          全局配置（根目录，被所有层 import）
```

依赖单向向下：service → engines/decision/execution → exchange 接口 → OKX 传输层。
**禁止反向 import**（如 exchange 层 import engines）。

## 3. 怎么跑（服务端，唯一推荐入口）

```bash
cd crypto-agent
PYTHONPATH=lib python3 -m service.main            # 前台
PYTHONPATH=lib python3 -m service.main --port 8090
```

一个进程承载全部功能：
- 方向性引擎：2s 止损监控 + 15min 信号扫描（tick 由 run() 抽取，逻辑不重复）
- 套利引擎：60s 事件检测/告警/持仓管理（开仓受 `ENABLE_FUNDING_ARB` 开关）
- 共享 WebSocket 实时行情；心跳文件沿用 watchdog 命名

HTTP 接口（127.0.0.1，Swagger 文档在 `GET /docs`）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 双引擎心跳健康 |
| GET | /status | 余额/持仓/风控全景 |
| GET | /watchlist | 今日候选池（评分→笔数） |
| GET | /signals/{base} | 按需信号检查（只读） |
| GET | /journal | 交易台账+胜率 |
| GET | /realtime/{base} | WS 实时行情快照 |
| GET | /arb/status | 套利引擎状态 |
| POST | /pause /resume | 暂停/恢复方向性开仓 |
| POST | /scan/daily | 手动触发全市场扫描 |
| GET | /error | 引擎最近异常堆栈 |

独立调试模式仍可用（不改交易逻辑）：`python3 engines/directional_trader.py --once`。

## 4. 测试

```bash
PYTHONPATH=lib python3 tests/test_exchange_layers.py   # 分层架构单测（FakeAdapter 离线全链路）
PYTHONPATH=lib python3 tests/test_service_api.py       # 服务端接口单测（TestClient 离线）
python3 -m py_compile <改动的文件>                      # 改动后必跑
```
改交易逻辑后：先单测，再沙盘实测一条下单链路（开仓→挂止损→pending→撤单→平仓），最后才重启活体进程。

## 5. 关键安全不变量（不可破坏）

1. `sandbox=True` 永远保持（模拟盘）。真实资金交易 = 绝对禁止。
2. 交易所侧止损（slTriggerPx 条件单）必须随每笔合约开仓挂出——本地监控只是兜底。
3. 单笔风险 1%、名义上限 150 USDT、组合总敞口 ≤600（PositionLedger）。
4. 最小下单量不足时**拒绝开仓**，绝不放大仓位凑最小张数。
5. `ENABLE_FUNDING_ARB=False` 是用户决定，不得擅自改回 True。
6. 条件单字段：止损 `slTriggerPx`、止盈 `tpTriggerPx`（triggerPx 会 50015）；`orders-algo-pending` 必须带 `ordType`。
7. HTTP 层只读观测 + 暂停/恢复；**不允许暴露下单接口**。

## 6. 代码最佳实践（本仓库约定）

- 单写者：同一文件同一时刻只有一个协作者写；并行任务只做只读验证。
- 失败语义两级：网络/签名→抛 `ExchangeError`（fail-closed）；业务拒绝→返回 `OrderResult(ok=False, message)`。
- 数量只向下取整（floor_to_lot），绝不超发。
- 所有对外接口写类型标注 + Pydantic 模型；关键决策注释写"为什么"。
- 状态文件原子写（.tmp + os.replace）+ flock（见 position_ownership.py）。
- 改活体进程前：先 `py_compile` → 单测 → 沙盘实测 → 重启 → 验证心跳/持仓衔接。
- 引擎代码不 import 任何 web 框架；HTTP 只是外壳。

## 7. 不允许的行为（红线）

1. ❌ 连接真实资金账户或修改 `sandbox` 开关。
2. ❌ 在 HTTP 层添加下单/撤单接口。
3. ❌ 删除/绕过风控闸门（1% 风险、150 上限、600 总敞口、熔断）。
4. ❌ 移除或弱化交易所侧止损；或改为仅本地止损。
5. ❌ 伪造回测/复盘数据，或向用户夸大收益、承诺胜率。
6. ❌ 擅自改 `ENABLE_FUNDING_ARB`、策略阈值等用户已拍板的配置。
7. ❌ 用"自动进化的模型输出"直接下单而不经既有闸门。
8. ❌ 引入新的重依赖（框架/SDK）而不先说明理由；交易所访问只走 exchange 层。
9. ❌ 在活体进程运行中直接改它读写的状态文件（trade_journal.json / watchlist.json / 心跳）。
10. ❌ 重启进程后不验证心跳与持仓衔接就宣称"已恢复"。

## 8. 已知边界（诚实声明）

- 方向性策略历史回测未证明正期望；系统只承诺"亏损有界"（止损+小仓位），不承诺收益。
- 美股代币仅现货者（XNVDA 等）只做多、无杠杆、本地止损；ANTHROPIC-USDT-SWAP 有合约走合约路径。
- 沙盘市场清单可能与生产有差异（如 XIAOMI-USDT-SWAP 沙盘暂缺）。

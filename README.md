# 加密货币自动化交易系统（宁可做对，不可做错）

以「宁可错过、不可做错」为核心哲学的自动化交易 agent。
OKX 模拟盘（虚拟资金），方向性日内短线（2026-08-16 用户决定：资金费率套利引擎移除，代码归档 legacy/）。
完整功能收在 FastAPI 服务端进程里，HTTP 只读观测 + 最小控制面。

> ⚠️ 重要声明：没有任何系统能"保证"收益率。本系统保证的是**风险上限**
> （单笔 1%、名义 ≤150 USDT、组合总敞口 ≤600、交易所侧止损），收益是概率性的。

## 核心理念

- **宁可做对，也不做错**：默认空仓，只在多重条件共振的"最正确时机"出手
- **空仓是默认，持仓是例外**
- **保证风险，追求收益**：回撤红线硬约束，收益让市场回答
- **防过拟合、不夸大收益、诚实报亏损**

## 快速开始（服务端唯一入口）

```bash
cd crypto-agent
PYTHONPATH=lib python3 -m service.main            # 前台
PYTHONPATH=lib python3 -m service.main --port 8090
```

一个进程承载全部功能：

| 组件 | 说明 |
|---|---|
| 方向性引擎 | 2s 止损监控 + 15min 回踩信号扫描 + 每日候选刷新 |
| 实时行情 | 原生 WebSocket |
| HTTP API | `GET /docs`（Swagger）、/health、/status、/watchlist、/journal、/signals/{base}、/realtime/{base}、POST /pause /resume /scan/daily |

## 目录结构（分层）

```
crypto-agent/
├── AGENTS.md               # AI 协作模型：分层/入口/安全不变量/最佳实践/红线
├── docs/architecture/exchange_layers.md      # 交易所访问四层架构
├── service/                # 服务端外壳（FastAPI + uvicorn，完整功能入口）
│   ├── main.py             #   进程入口：方向性引擎 + HTTP
│   ├── app.py              #   HTTP 接口层（只读观测 + 暂停/恢复，禁止下单）
│   ├── models.py           #   Pydantic 响应模型（AI 可读 schema）
│   └── worker.py           #   引擎托管：后台线程 + 共享 WS + 心跳
├── engines/                # 交易引擎层（2026-08-20 按功能拆分，行为零变化）
│   ├── directional_trader.py   # 方向性引擎核心壳（入口/组装/对账/tick 主循环）
│   ├── signal_scan.py          #   信号扫描/候选池/额度/冷却（SignalScanMixin）
│   ├── position_mgmt.py        #   开仓全链路/条件单/失败落库（PositionMixin）
│   ├── risk_monitor.py         #   止损监控/熔断强平（RiskMonitorMixin）
│   ├── review_pipeline.py      #   平仓复盘链/阈值进化门（ReviewMixin）
│   └── daily_scan.py           # 每日全市场候选扫描 → watchlist（走 ExchangeAdapter）
├── decision/               # 决策与进化层（self_evolving/experience/threshold/review/evolution_gate）
├── execution/              # 执行与台账层（quantity/trade_journal/position_ownership）
├── exchange/               # 交易所访问四层（transport/okx_adapter/base/models/fake）
├── factors/                # 因子挖掘研究层
├── tools/                  # 工具（scan/paper_trade/okx_pg_ingest/watchdog）
├── data/                   # 数据源（fetch_okx/fetch_*/realtime_okx/economic_calendar）
├── strategy/  risk/  backtest/
├── tests/                  # 全部测试
├── docs/                   # 文档中心（architecture/plans/reports/ops/prompts，索引 docs/README.md）
├── legacy/                 # 废弃/归档文件（含已移除的套利引擎与对应测试）
└── config.py               # 全局配置
```

依赖单向向下：service → engines/decision/execution → exchange 接口 → OKX 传输层。禁止反向 import。

## 测试

```bash
PYTHONPATH=lib python3 tests/test_exchange_layers.py   # 分层架构单测（FakeAdapter 离线）
PYTHONPATH=lib python3 tests/test_service_api.py       # 服务端接口单测（TestClient 离线）
python3 -m py_compile <改动的文件>
```

改交易逻辑后：先单测 → 沙盘实测一条下单链路（开仓→挂止损→pending→撤单→平仓）→ 再重启活体进程 → 验证心跳与持仓衔接。

## 数据源与交易所

- 交易所：OKX 模拟盘（sandbox，虚拟资金），原生 REST 直连（无 ccxt）
- 交易路径 REST：一律走 `exchange/`（适配层 `fetch_tickers`/`fetch_candles`/下单）；`engines/` 禁止裸打 OKX URL
- 行情：OKX 原生 WebSocket（tickers/funding-rate/trades）+ 适配层 REST 预热
- 历史数据（研究/回测）：`data/fetch_okx.py` 的 `/market/history-candles`（约 6 年）
- 本地库：`crypto_agent.db`（SQLite）。流水日志保留 90 天（`config.DB_RETENTION_DAYS`），每天扫完候选池后自动清理；交易台账、经验库、研究表永久保留。未处理的告警不会被清掉。
- 飞书通知：开仓/平仓/告警走交互卡片（`lark_md`），不是 GitHub Markdown。普通 `--text` 在飞书里不会加粗。

## 已知边界（诚实声明）

- 方向性策略历史回测未证明正期望；系统只承诺"亏损有界"，不承诺收益
- 美股代币仅现货者（XNVDA 等）只做多、无杠杆；ANTHROPIC-USDT-SWAP 有合约走合约路径
- 沙盘市场清单可能与生产有差异（如 XIAOMI-USDT-SWAP 沙盘暂缺）

详见 [AGENTS.md](AGENTS.md)。

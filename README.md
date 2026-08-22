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

## 反哺体系（本项目的核心）

交易产生的每一类数据，都有一条受控回路流回系统——**没有任何一条回路能直接改参数**：

| 回路 | 机制 |
|---|---|
| 复盘 → 教训 | 每笔平仓强制六维复盘（入场/止损/出场/信号/仓位/决策链），教训经一致性初筛入经验库 |
| 教训 → 验证 | 被采纳的教训用后续真实交易 ±10 分 × 3 次 → trusted / discarded（数据匹配才是成立） |
| 教训 → 生效 | 只聚合同场景 trusted 教训（场景条件向量：方向/波动带/趋势/信号类型），按验证强度分档 |
| 场景归纳 | 同场景 trusted ≥3 条 → 沉淀归纳结论（只读汇总，防"归纳验证归纳"回声） |
| 组合试验 | 实际采纳的教训组合（≥2 条）按真实结果记账——"单条不盈利，combo 可能盈利" |
| 历史先验 | 教训诞生即查历史同场景先例（胜率/期望 R），只观测不进验证循环 |
| 异常 → 值守 | 所有异常统一进异常中心，单一告警链推送飞书交互卡片 + 会话注入 |
| 修复经验 | 每个已修缺陷登记为一条机器护栏（G1-G19），体检每 5 分钟验证防回退 |

## 实盘就绪三盏灯

上实盘 = 三灯全绿（`python3 tools/readiness.py`，阈值在 config.py `READY_*`）：

- 🔴/🟢 **灯1 样本**：平仓 ≥60 笔 且 Van Tharp SQN ≥1.6
- 🔴/🟢 **灯2 稳定**：近 7 天零 critical 异常且零未决异常
- 🔴/🟢 **灯3 反哺**：trusted 经验 ≥3 条 且 场景归纳 ≥1 条


## 快速开始（服务端唯一入口）

```bash
cd crypto-agent
PYTHONPATH=lib python3 -m service.main            # 前台
PYTHONPATH=lib python3 -m service.main --port 8090
```

一个进程承载全部功能：

| 组件 | 说明 |
|---|---|
| 方向性引擎 | 2s 止损监控 + 5min 回踩信号扫描 + 每日候选刷新 |
| 实时行情 | ccxt.pro watch_ticker（config.REALTIME_BACKEND 可切回原生 WS） |
| HTTP API | `GET /docs`（Swagger）、/health、/status、/watchlist、/journal、/signals/{base}、/realtime/{base}、/scan/evolve、POST /pause /resume /scan/daily /scan/evolve/approve /scan/evolve/rollback |

### 双实例并行（2026-08-23 模拟盘 + 实盘同时跑）

环境变量决定实例身份，两实例状态完全隔离：

| 环境变量 | live（实盘，8090） | paper（模拟盘，8091） |
|---|---|---|
| `CRYPTO_AGENT_MODE` | live | paper |
| `CRYPTO_AGENT_DB` | crypto_agent_live.db（独立，实盘盈亏基线起算） | crypto_agent.db（延续历史模拟数据） |

- 库、engine.lock / engine_paper.lock、进化门状态文件、心跳/PID/tick 文件（directional.* / paper.*）全部按模式隔离
- launchd：`com.crypto.agent`（实盘）+ `com.crypto.paper`（模拟盘），watchdog 双实例独立监控，healthcheck 各查各库
- 通知统一打 `【实盘】`/`【模拟盘】` 前缀（飞书 + 会话注入同一报警链）
- 实盘盈亏从基线净值起算（kv `live_pnl_start`），平仓只累计 venue=live 的交易；模拟盘延续历史统计
- 数据看板（8899）右上角可切换 模拟盘/实盘 视图

### 经验共享（2026-08-23 用户指示"经验共享"）

两实例的教训库（lessons + lesson_rollups）互相镜像：

- 教训以内容哈希 `share_key` 为跨库唯一身份（两库自增 id 会撞车，不依赖 id）
- 每条教训由**产生它的实例**独占验证（origin='local'）；对端只读镜像（origin='peer'），`validate()` 跳过镜像行，防 good/bad 双重计数
- 同步时机：引擎启动时 + 每小时（`EXPERIENCE_SHARE_INTERVAL_HOURS`），双向 = 各拉对方
- 镜像带 `last_update` 前进条件，对端旧镜像不会回滚本地新状态
- 决策聚合（evidence_strength）对镜像教训按 `EXPERIENCE_PEER_WEIGHT` 加权（默认 1.0 等权）
- 效果：模拟盘激进采集 → 验证为 trusted 的教训自动流入实盘决策；实盘真金验证的教训回流模拟盘

### 策略保持一致（2026-08-23 用户指示"策略也保持一致，反哺策略"）

两实例的**策略演化状态**（反哺产物）也互相合并，决策用同一套策略：

- `thresholds[key='dir']`：决策阈值 + 校准样本（score→pnl）。样本按 (score, pnl) 去重取并集——两实例的全部交易证据合成一份；阈值取 `updated_at` 新者（进化门在任一侧晋升/回滚，另一侧跟随）
- `kv scan_evolve.*`：扫描尺子进化状态（REJECT_WICK_RATIO 影子/批准），按时间戳新者镜像
- 同步时机与经验共享一致（启动 + 每小时），双向幂等；合并后重载阈值学习器
- 效果：模拟盘的激进样本量 + 实盘的真金样本合并校准同一个阈值，两个实例的决策门槛永远一致

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
- 飞书通知：开仓/平仓/告警走交互卡片（`lark_md`），不是 GitHub Markdown。普通 `--text` 在飞书里不会加粗。每日看账和交易台账的「总盈亏」是已平仓合计的**实际 USDT**（名义投注额 × 盈亏比例），不是把各笔百分比加起来。

## 已知边界（诚实声明）

- **6 个月历史回测当前为负期望**：裸信号 -0.306R（2482 笔）、完整管线（每日筛选+冷却）-0.372R（715 笔），含 10bps 成本、无未来函数（`tools/replay_pipeline.py`，先跑 `backfill_history.py` 回填数据）。按自定"证伪权"条款（回测只能证伪、不能证实），策略处于**退役评估**阶段
- 系统只承诺"亏损有界"，不承诺收益；本项目为研究/教育用途，**不构成投资建议**
- 美股代币仅现货者（XNVDA 等）只做多、无杠杆；ANTHROPIC-USDT-SWAP 有合约走合约路径
- 沙盘市场清单可能与生产有差异（如 XIAOMI-USDT-SWAP 沙盘暂缺）

详见 [AGENTS.md](AGENTS.md)。

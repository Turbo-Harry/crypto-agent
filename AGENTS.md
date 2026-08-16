# AGENTS.md — 本仓库的 AI 协作模型

> 供 AI agent / 新协作者阅读。读本文即可理解"这个仓库是什么、怎么跑、能动什么、不能动什么"。

## 1. 这是什么

自动化加密货币交易系统（OKX 模拟盘，虚拟资金）。
策略线：**方向性日内短线**（活跃）。（2026-08-16 用户决定：资金费率套利引擎整线移除，
代码归档 `legacy/`——trading_main/trading_agent/funding_arb/weight_learning/scoring 与对应测试。）

核心哲学（用户反复强调，不可违背）：
- 宁可做对，也不做错；空仓是默认，持仓是例外
- 抓最佳时机进出，不频繁交易
- 防止过拟合；不夸大收益；诚实地报亏损
- 小仓位慢跑（单笔风险 1%，名义 ≤150 USDT）

## 2. 分层架构（物理分层已落地）

```
service/            服务端外壳（FastAPI + uvicorn，完整功能唯一入口）
  ├─ main.py        进程入口：方向性引擎 + HTTP
  ├─ app.py         HTTP 接口层（观测/控制/运维，禁止下单接口）
  ├─ models.py      Pydantic 响应模型（AI 可读 schema，自动进 /docs）
  └─ worker.py      引擎托管：后台线程 + 共享 WS + watchdog 心跳

engines/           交易引擎层
  ├─ directional_trader.py  方向性引擎（回踩确认 + 2:1 盈亏比 + tick 止损）
  └─ daily_scan.py          每日全市场候选扫描 → watchlist.json

decision/          决策与进化层
  ├─ self_evolving_trader.py  决策进化（综合经验库做开仓决策）
  ├─ experience_scoring.py    经验评分库
  ├─ threshold_learning.py    阈值自适应
  ├─ review_engine.py         复盘引擎
  └─ evolution_gate.py        进化验证门

execution/         执行与台账层
  ├─ quantity.py              名义→数量换算（lotSz 对齐）
  ├─ trade_journal.py         交易台账 + 复盘报告
  └─ position_ownership.py    持仓所有权账本（总敞口≤600）

storage/           数据持久化层（SQLite，全仓数据唯一落点）
  └─ db.py         crypto_agent.db：trades/lessons/thresholds/watchlist/
                   position_snapshots/arb_positions/ownership/kv 八张表，
                   WAL + busy_timeout，每操作独立短连接（线程安全）；
                   首启自动迁移旧 JSON（幂等）

exchange/          交易所访问四层（见 docs/architecture/exchange_layers.md）
  transport.py     OKX 原生 REST：HMAC 签名/模拟盘/限速/错误归一
  okx_adapter.py   单位换算(ctVal/lotSz)/场所探测/响应翻译
  base.py          抽象接口 ExchangeAdapter + ExchangeError
  models.py        领域模型 + floor_to_lot
  fake_adapter.py  内存假交易所（单测注入）

factors/           因子挖掘研究层（factor_discovery/evolution/mining）
tools/             工具脚本（scan.py / paper_trade.py / okx_pg_ingest.py / watchdog.py / code_graph.py）
data/              数据源（fetch_okx / fetch_* / realtime_okx / economic_calendar）
strategy/  risk/  backtest/   指标 / 风控 / 回测
tests/             全部测试（test_exchange_layers.py / test_service_api.py / test_r*）
docs/              文档中心（architecture/plans/reports/ops/prompts，索引见 docs/README.md）
legacy/            废弃/归档文件（trading_daemon.py.legacy + 2026-08-16 归档的套利引擎与测试）
config.py          全局配置（根目录，被所有层 import）
llms.txt           AI 入口索引（llmstxt 标准，指向 AGENTS/README/docs 关键文档）
```

依赖单向向下：service → engines/decision/execution → exchange 接口 → OKX 传输层。
**禁止反向 import**（如 exchange 层 import engines）。
代码关系图（mermaid + 依赖矩阵 + 分层检查）见 `docs/architecture/code_graph.md`；
改动 import 后跑 `python3 tools/code_graph.py --check` 验证无反向依赖。

## 3. 怎么跑（服务端，唯一推荐入口）

```bash
cd crypto-agent
PYTHONPATH=lib python3 -m service.main            # 前台
PYTHONPATH=lib python3 -m service.main --port 8090
```

一个进程承载全部功能：
- 方向性引擎：2s 止损监控 + 15min 信号扫描（tick 由 run() 抽取，逻辑不重复）
- WebSocket 实时行情；心跳文件沿用 watchdog 命名

HTTP 接口（127.0.0.1，Swagger 文档在 `GET /docs`）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 方向性引擎心跳健康 |
| GET | /status | 余额/持仓/风控全景 |
| GET | /watchlist | 今日候选池（评分→笔数） |
| GET | /signals/{base} | 按需信号检查（只读） |
| GET | /journal | 交易台账+胜率 |
| GET | /realtime/{base} | WS 实时行情快照 |
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
5. 条件单字段：止损 `slTriggerPx`、止盈 `tpTriggerPx`（triggerPx 会 50015）；`orders-algo-pending` 必须带 `ordType`。
6. HTTP 层只读观测 + 暂停/恢复；**不允许暴露下单接口**。

## 6. 文档路径约束（写文档必守）

所有新文档一律进 `docs/`，按功能选目录，文件名带日期前缀：

| 文档类型 | 目录 | 示例 |
|---|---|---|
| 架构/设计 | `docs/architecture/` | exchange_layers.md |
| 计划/简报 | `docs/plans/` | optimization_plan_agentB_R2_FINAL.md |
| 报告/日志/踩坑 | `docs/reports/` | pitfalls.md |
| 运维/验证手册 | `docs/ops/` | tp_sandbox_verify.md |
| AI 提示词 | `docs/prompts/` | evolution_loop_prompt.md |

- 文件名：`YYYY-MM-DD_功能名.md`；同一方案保留草稿+`_FINAL` 终审稿，终审稿标注"权威实施稿"。
- 例外（活文档，追加式更新，不加日期前缀）：`docs/reports/pitfalls.md`、`docs/reports/optimization_notes.md`。
- 新增/移动文档后：同步更新 `docs/README.md`（功能表+时间线表）与全部交叉引用。
- 根目录只留 `README.md`、`AGENTS.md`、`llms.txt` 三个入口，禁止在根目录新增散装 md。
- 禁止把文档塞进代码目录（backtest/ data/ 等已清空归位）。
- 新增关键入口文档/架构文档后，同步更新 `llms.txt` 链接清单（AI 靠它发现入口）。

## 7. 写代码前：先借鉴已有经验（必做）

动手前按顺序读：
1. `docs/reports/pitfalls.md` —— 踩坑档案（先看有没有同类坑）
2. `docs/reports/optimization_notes.md` —— 历史实施记录（R1/R2/OP/CR/RES，别重复造轮子）
3. 相关架构文档（`docs/architecture/`）与当前模块代码

同类问题已踩过的坑不得重踩；若旧方案被推翻，先在优化记录里写明原因再动手。

## 8. 写代码时：同步记踩坑（必做）

每修一个 bug / 每踩一个新坑，**当场**按模板追加到 `docs/reports/pitfalls.md`：
现象 → 根因 → 修复 → 预防。不留到事后补；与代码提交同步走。

## 9. 代码最佳实践（本仓库约定）

- 单写者：同一文件同一时刻只有一个协作者写；并行任务只做只读验证。
- 失败语义两级：网络/签名→抛 `ExchangeError`（fail-closed）；业务拒绝→返回 `OrderResult(ok=False, message)`。
- 数量只向下取整（floor_to_lot），绝不超发。
- 所有对外接口写类型标注 + Pydantic 模型；关键决策注释写"为什么"。
- 状态文件原子写（.tmp + os.replace）+ flock（见 position_ownership.py）。
- 改活体进程前：先 `py_compile` → 单测 → 沙盘实测 → 重启 → 验证心跳/持仓衔接。
- 引擎代码不 import 任何 web 框架；HTTP 只是外壳。

## 10. 不允许的行为（红线）

1. ❌ 连接真实资金账户或修改 `sandbox` 开关。
2. ❌ 在 HTTP 层添加下单/撤单接口。
3. ❌ 删除/绕过风控闸门（1% 风险、150 上限、600 总敞口、熔断）。
4. ❌ 移除或弱化交易所侧止损；或改为仅本地止损。
5. ❌ 伪造回测/复盘数据，或向用户夸大收益、承诺胜率。
6. ❌ 擅自改策略阈值等用户已拍板的配置。
7. ❌ 用"自动进化的模型输出"直接下单而不经既有闸门。
8. ❌ 引入新的重依赖（框架/SDK）而不先说明理由；交易所访问只走 exchange 层。
9. ❌ 在活体进程运行中直接改它读写的状态文件（trade_journal.json / watchlist.json / 心跳）。
10. ❌ 重启进程后不验证心跳与持仓衔接就宣称"已恢复"。

## 11. 已知边界（诚实声明）

- 方向性策略历史回测未证明正期望；系统只承诺"亏损有界"（止损+小仓位），不承诺收益。
- 美股代币仅现货者（XNVDA 等）只做多、无杠杆、本地止损；ANTHROPIC-USDT-SWAP 有合约走合约路径。
- 沙盘市场清单可能与生产有差异（如 XIAOMI-USDT-SWAP 沙盘暂缺）。

## 12. 完成声明 = 证据包（M7，收敛保证机制）

任何"已完成"的声明必须附带逐项证据（任务清单 ↔ 提交 diff 双向核对）：
1. 声明完成前，把原任务清单逐项写出**实测证据**（测试输出/接口响应/表行数），
   禁止"应该没问题"式推断（2026-08-16 T0.4 曾漏清 thresholds 临时 key 的教训）；
2. 全量回归必须当场重跑并附数字（绿 N 项、红 0 项）；
3. 活体改动必须附重启后验证（心跳年龄、持仓衔接、体检结果）；
4. 与清单不符的项要么补做、要么在完成声明中明确标记"未做+原因"。

## 13. 参数集中化规则（用户规则，机器执行）

1. **新增/修改策略参数只能在 `config.py` 的「参数统一维护区」进行**；策略层模块
   （engines/decision/execution/risk/service）只允许 `X = config.X` 形式引用，
   禁止私藏数字/字符串字面量参数。
2. 机器执行：`tools/params_lint.py` + `tests/test_params_centralization.py`（进全量套件，
   违规则测试红）。合法例外（结构/凭证/服务绑定类）见 params_lint 注释。
3. 门槛三件套联动约束：`THRESHOLD_INITIAL < DECIDE_MIN_SCORE <= SIGNAL_SCORE`
   （不满足会导致全部信号被拒或门槛失效），改动必须在 config 注释中同步说明。

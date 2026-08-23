# AI 友好仓库协作契约

> 活文档。2026-08-23 第三轮同步：补入 15m/4h 数据研究链与交易 Agent Harness，
> 从“入口可发现”继续升级为“运行链可追踪、统计结论有成熟度边界”。本页面面向第一次
> 接手仓库的 AI 与新协作者。

## 一、60 秒接手路径

1. 先读根目录 `AGENTS.md`：它定义安全边界、架构规则与禁止事项。
2. 再读 `llms.txt`：按任务选择最少的代码与文档，不要先吞完整仓库。
3. 运行 `python3 tools/agent_notes.py status` 与 `git status --short`：保护其他协作者和用户改动。
4. 写代码前按 AGENTS.md 要求读 `docs/reports/pitfalls.md`、
   `docs/reports/optimization_notes.md` 与相关架构文档。
5. 用 `python3 tools/code_graph.py --query module:<模块>` 或
   `--query calls:<符号>` 查影响面，再决定修改范围。
6. 写入前 claim 文件，完成后 release；协议见 `docs/AGENT_NOTES.md`。

首次阅读到这里时，不应启动服务、连接交易所、重启进程或修改任何状态文件。

## 二、事实优先级

信息冲突时按以下顺序处理：

1. 当前用户指令与 `AGENTS.md` 的安全不变量；
2. `config.py`、Pydantic schema、接口/实现代码和可重复测试；
3. `README.md` 与 `docs/architecture/` 的当前设计说明；
4. `docs/plans/` 的终审稿；
5. `docs/reports/` 中带日期的历史结论与旧实施记录。

历史文档只说明“当时发生过什么”，不能覆盖当前代码。若高优先级来源彼此冲突，尤其是
paper/live、下单权限、风控上限、止损或策略阈值，必须 fail-closed：停止变更并向用户确认。

当前已知的高风险例子：代码中存在 live/paper 双实例能力，而 AGENTS.md 对 AI 只授权 paper。
因此 AI 的启动命令必须显式写 `CRYPTO_AGENT_MODE=paper`；不得把代码能力解释成操作授权。

## 三、任务路由

| 任务 | 先读 | 主要入口 | 必做验证 |
|---|---|---|---|
| 服务/API | AGENTS §2～5、exchange_layers | `service/app.py`、`service/models.py`、`service/worker.py` | service API 测试 + py_compile |
| 信号/开仓 | pitfalls、optimization_notes、参数规则 | `engines/signal_scan.py`、`engines/position_mgmt.py` | params lint + 决策/分层测试 |
| 止损/风控 | AGENTS 安全不变量、相关 pitfalls | `engines/risk_monitor.py`、`risk/`、`execution/position_ownership.py` | 风控测试；涉及活体必须走沙盘证据链 |
| 复盘/进化 | self_evolution_design、trade_features_schema | `engines/review_pipeline.py`、`decision/` | 验证门测试 + 防过拟合检查 |
| 15m 候选/标签 | entry_accuracy 终审稿、路径标签 pitfalls | `engines/signal_sampling.py`、`decision/signal_outcomes.py` | 同 K 幂等 + 4h/1m 完整路径测试 |
| 因子/概率/极值 | entry_accuracy 终审稿、15m SWAP 重放报告、factor prompt | `tools/replay_15m_research.py`、`tools/entry_accuracy_audit.py`、`factors/feature_registry.py`、`intraday_factor_*`、`entry_model_training.py`、`extrema_model_training.py` | SWAP/as-of/路径连续性 + purged walk-forward + DSR/PBO + 校准/分位测试 |
| Agent Harness | agent_harness 终审稿、entry_accuracy T8 | `decision/agent_harness.py`、`agent_policy.py`、`agent_evaluation.py`、`storage/agent_*` | contracts/context/policy/trace/memory/evaluation 全链测试 |
| 交易所接入 | exchange_layers、API 类 pitfalls | `exchange/base.py`、适配器、transport | FakeAdapter 离线链路 + 分层检查 |
| 数据库/schema | SQLite 类 pitfalls | `storage/db.py` | 临时库迁移/事务测试，禁止碰活体库 |
| 文档/AI 入口 | 本页、docs/README | `AGENTS.md`、`llms.txt`、`docs/README.md` | `tools/ai_repo_check.py` |
| 运维/状态查询 | AGENTS §10、docs/ops | `tools/health_check.py`、只读 HTTP | 只读证据；未经授权不重启/不写状态 |

## 四、AI 可执行证据清单

按改动范围选择，不能用“应该没问题”代替实测：

```bash
# AI 入口、文档链接、索引完整性
python3 tools/ai_repo_check.py
python3 tests/test_ai_repo_check.py

# 架构、参数与守卫
python3 tools/code_graph.py --check
python3 tools/params_lint.py
python3 tools/test_isolation_lint.py
python3 tools/fix_guard.py

# 主要离线入口
PYTHONPATH=lib python3 tests/test_exchange_layers.py
PYTHONPATH=lib python3 tests/test_service_api.py

# 完整套件以 CI 的自动发现脚本为准；每个脚本必须使用独立 DB/JSONL/runtime
# 当前仓库快照为 47 个 tests/test_*.py，禁止手写固定测试白名单。

# 仅模拟盘启动；不要省略模式
CRYPTO_AGENT_MODE=paper PYTHONPATH=lib python3 -m service.main --port 8091
```

涉及交易行为、活体进程或状态库时，还要遵守 AGENTS.md 的沙盘实测、重启后心跳与持仓
衔接证据要求。纯文档/守卫改动不应触碰交易所、数据库、心跳、日志或通知通道。

## 五、机器守卫

`tools/ai_repo_check.py` 使用 Python 标准库，检查：

- 根入口 `README.md`、`AGENTS.md`、`llms.txt`、`docs/README.md` 存在；
- 根目录没有散装 Markdown；
- 入口与 docs 中的本地 Markdown 链接存在且不越出仓库；
- `llms.txt` 覆盖安全规则、协作协议、架构入口与历史经验；
- `docs/README.md` 索引每一篇 docs 文档；
- `AGENTS.md` 保留 paper 显式启动、协作占用、AI 自检、分层检查和踩坑阅读护栏。

`.github/workflows/ci.yml` 另外自动发现全部 `tests/test_*.py`，逐脚本隔离
`CRYPTO_AGENT_DB`、`CRYPTO_AGENT_EVENTS_FILE` 与 `CRYPTO_AGENT_RUNTIME_DIR`，并运行
compileall、参数集中化、代码图、测试隔离和修复护栏；测试依赖包含 `ccxt>=4.5,<5`。

CI 会运行同一检查及其变异测试。它能抓“链接/索引/关键操作护栏漂移”，不能判断自然语言
中的所有语义冲突；事实冲突仍按第二节人工 fail-closed。

## 六、维护规则

- 新增关键入口：同时更新 `llms.txt`；新增/移动 docs：同时更新 `docs/README.md`。
- 文档引用当前行为时，优先引用 `config.py` 名称，不复制易漂移的数字。
- 报告、计划和事故档案要保留日期语境；不要把历史结果伪装成当前状态。
- 改 import 后运行 code graph；改策略参数只动 config 参数统一维护区。
- AI 自检发现漂移时，先修事实源和引用，再更新索引；不要用跳过列表隐藏失效链接。

## 七、当前运行链地图

- 候选链：`SignalScan → signal_samples → signal_outcomes`。15m 已收线 K 是样本身份，
  4h/1m 完整路径才允许结算；规则、额度和 Agent 拒绝同样保留反事实。
- 研究链：`signal_samples/outcomes → feature_registry → intraday_factor_mining →
  model_artifacts`。`tools/evaluate_15m_research.py` 只接受带 provenance 的 research-only
  重放库，一键产出成本 EV、因子、概率/极值模型和停止裁决；模型只做结构信号的
  meta-label，不产生新方向。
- 预测链：止损 -1R、止盈 +2R 固定不搜索；开仓概率、TP/SL/timeout 首触概率和最高/最低
  条件分位先写 shadow。真实 OKX paper 另有严格 `preopen_2to1` 门：只有通过独立验证的
  active 模型且按本候选 `entry/stop` 换算、扣除双边 taker/滑点/保守不利资金费后的 EV 95%
  下界为正才开仓；潜在资金费收入不降低严格门。shadow/observing 至少需要 30 个实际预测
  放行样本且其净 EV>0。缺失/损坏/scope/成本版本不符均失败关闭，拒绝
  候选仍结算反事实标签。FakeAdapter 和未重启 live 不受该 paper 门影响。
- Agent 链：`agent_context → memory/read-only tools → model contract → policy kernel →
  agent_runs/steps/evaluations`。真实 OKX paper 会注入严格 provider；Harness 在去重结构候选
  形成后立即 shadow 留痕，因此额度/分数/2:1 门拒绝也能取得 4h 反事实结果。legacy AI 仍只在
  实际下单链持有现役否决权，模型故障不能扩大权限。FakeAdapter/live 不接入新 Harness。
  成熟评价会按完整 Harness 版本自动生成费用后增量 EV 下界、Brier 和稳定性，并最多推进到
  validated；`GET /agent/evaluation.harness` 是当前证据入口，validated 不等于 active-veto。
- 生命周期：`candidate → validated → shadow → accepted → active → observing → kept/rolled_back`；
  达到状态门不等于有收益证据，仍需训练截止点之后的新样本。
- 完成度链：`tools/entry_accuracy_audit.py` 与 `GET /research/readiness` 只读汇总自然
  paper 平仓、六维完整平仓、候选类别、因子、模型、校准、Agent 与长期 EV 预算锁；
  独立历史研究样本只可通过候选/类别门，永远不能抵扣 paper 或 Agent 门。

## 八、2026-08-23 体检结论

已补齐：AI 接手路径、事实优先级、任务路由、文件占用入口、纯标准库文档守卫、变异测试与
CI 接线；同步 15m/4h 候选、因子、概率、极值和 Agent Harness 的代码入口；同时移除
`llms.txt` 对已归档套利模块的失效引用，并把 paper 启动模式写成显式命令。

当前统计边界：代码和离线测试通过不等于开仓准确率已经提高。paper 的现役 A 策略已有 26 条
版本快照、23 个独立 15m/4h 市场机会、9 个自动结算路径（TP 4/SL 4/timeout 1）和 1 笔自然
平仓（六维完整）；概率校准与有效 legacy Agent 结果各 9 条，Harness 自然成熟评价 1 条，但
validated 因子与 accepted 模型仍为 0。新 B 策略的候选、因子试验、模型制品、自然平仓和
Agent 版本都不计入或遮挡 A 的门槛；
schema v31 已把交易台账、模型选择、生命周期、回滚、预算锁与 readiness 一并按策略隔离，
并用 canonical 视图将跨配置审计快照收敛为独立市场机会；只读接口可显式查询 A/B 且返回
各自配置身份。独立历史研究库扩展到 A 的 1,712 个
真实 SWAP 候选/1,690 个完整路径，并以相同市场和标签口径新增 B 的 1,973/1,931；固定
2:1 的毛 EV=-0.0789R、净 EV=-0.9770R，固定随机种子下概率 Brier skill=-2.63%；当前 61 个
预注册因子中 validated 仍为 0。六维子分已正确物化进因子矩阵；资金费、5m 波动率、同收线
横截面以及布林/ADX/效率比/VWAP 共 61 项现可统一评价，但全部未过硬门。新增的 top-5 连续事件 OFI、同价队列消退、事件数和年龄使用 `signal-features-v4`，不足 10 个事件或
断流超过 5 秒即缺失；`GET /realtime/{base}` 可直接审计状态、值、事件数与年龄。历史库没有
L2 事件，4 项均只能判 `insufficient_data`，详见 15m SWAP 重放报告。行情标签目前只用于
分层证据；`decision/market_regime.py` 与 `strategy_router.py` 已把可解释的行情权重和策略建议
冻结进候选，明确 `calibrated=false/has_execution_authority=false`。A/B 共用 15m/4h 标签链
但训练证据隔离；历史中 B 毛 EV=+0.1013R、成本后=-0.7565R，路由命中仍=-0.4615R；A/B
路由命中合并=-0.5140R。因此新 B 自然结果未成熟前，不能宣称自动策略选择有效，也不能用
硬标签直接放单。
因子 Combo 只允许两种形式：有理论依据的预注册二阶交互逐项过完整因子门，以及多个
validated 因子作为同一特征向量进入概率/极值模型的 purged walk-forward 联合评价；禁止
无假设穷举后在同一数据集挑最高收益。当前 validated=0，因此没有可晋升的联合模型。
paper worker 每 24 小时对 A/B 分别运行完整的因子、long/short 概率和极值研究；失败进入
`/error` 与 `engine_errors`，15 分钟幂等重试。首次生产周期已得到 A/B 各 61 项、errors=0；
B 当前 2 个候选、成熟路径 0，仍无执行权限。
每日候选的 CCXT ticker 已显式绑定 SWAP 场所并修复批量结果缺 `base` 的映射；最终模拟盘
扫描为 64→60→35→30，写入 12 个非回退候选。`STOCK_SWAP_TOKENS` 的 9 个沙盘美股永续
与加密币同门筛选，不能绕过流动性、趋势、ATR、固定 2:1 和既有风险门。
模拟盘严格预测门因此保持空仓和反事实采集，Agent 自然样本未达门前禁止扩大预算。

仍需人工决策：代码已实现 live/paper 双模式，但 AGENTS.md 对 AI 的授权仍只允许 paper。
本轮不修改交易模式、策略参数或活体配置；后续若用户改变授权，必须在同一改动中同步
AGENTS.md、README、服务描述、运维手册与机器护栏。

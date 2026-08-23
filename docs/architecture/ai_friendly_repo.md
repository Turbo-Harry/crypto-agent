# AI 友好开发与协作契约

> 活文档。面向第一次接手仓库的 AI 与新协作者。它回答四件事：什么可以做、先读哪里、
> 怎样安全地改、用什么证据证明完成。安全授权以根目录 `AGENTS.md` 为准；本页不扩大权限。

## 一、60 秒只读接手

1. 读根目录 `AGENTS.md`，确认 paper-only 授权、安全不变量和红线。
2. 读 `llms.txt`，按任务选择最少的入口；进入顶层模块后再读该目录的 `AGENTS.md`。
3. 运行 `python3 tools/agent_notes.py status` 和 `git status --short`，识别协作者占用与用户改动。
4. 写代码前读 `docs/reports/pitfalls.md`、`docs/reports/optimization_notes.md` 和相关架构文档。
5. 用 `rg` 定位真实调用点，再用代码图确认影响面，例如：

   ```bash
   rg -n "open_position|preopen_2to1" engines decision tests
   python3 tools/code_graph.py --query calls:open_position
   python3 tools/code_graph.py --query module:service.app
   ```

6. 只有准备写入时才 claim 文件；完成验证后 release。协议见 `docs/AGENT_NOTES.md`。

首次接手到这里仍是只读阶段：不启动服务、不构造交易所客户端、不调用控制接口、不修改
数据库、台账、心跳、PID、watchlist 或其他运行态文件。

## 二、事实与权限怎么裁决

用户目标决定“要做什么”，`AGENTS.md` 决定“允许做到哪里”。两者不能用代码现有能力替代：
仓库具备 paper/live 双实例能力，不等于 AI 获得 live 操作权限。

事实冲突时按以下顺序处理：

1. 当前用户目标与 `AGENTS.md` 的安全授权；红线冲突时以更严格边界为准。
2. `config.py`、`interfaces/`、Pydantic schema、实现代码和可重复测试。
3. `README.md` 与 `docs/architecture/` 的当前设计。
4. `docs/plans/*_FINAL.md` 权威实施稿。
5. 带日期的计划、报告、实施日志和历史回测。

实用判断规则：

- 当前值看 `config.py`，不要从旧报告抄参数。
- HTTP 契约看 `service/models.py` 和运行时 `/docs`，不要从历史接口表猜字段。
- 数据库事实看 `storage/db.py` 与 repository/query API，不在调用方散写 SQL。
- “曾经通过”“设计为”“代码支持”都不等于当前已运行、已授权或已有收益证据。
- paper/live、下单权限、风控、止损、仓位单位或策略阈值有冲突时，停止变更并 fail-closed。

特别注意：`config.py` 的模式默认值是 `live`。任何可能构造应用、引擎或交易所对象的命令，
都不得依赖默认值；AI 只可在任务明确需要时显式使用 `CRYPTO_AGENT_MODE=paper`。

规则继承：根 `AGENTS.md` 对全仓生效，离目标文件最近的模块 `AGENTS.md` 增加局部职责、依赖
边界和验证要求。局部文件不得放宽 paper-only、风控、止损、执行权限、文档或证据红线；
出现冲突时执行更严格规则并回到根文件确认。

## 三、变更等级与授权边界

| 等级 | 典型任务 | 默认动作 | 额外条件 |
|---|---|---|---|
| D0 只读 | 解释代码、审计、状态报告 | 可读源码、文档、Git 状态和只读接口 | 不写文件、不调用控制接口 |
| D1 离线改动 | 文档、测试、无副作用纯函数 | claim 后修改，使用临时状态验证 | 不启动活体、不连接交易所 |
| D2 交易敏感改动 | 接口、信号、风控、数量、执行、schema、策略参数 | 先查影响面，Fake/Stub 与隔离库验证 | 参数只改 `config.py`；不得弱化安全门 |
| D3 paper 运行 | 沙盘下单链、重启、状态迁移 | 仅在用户当前任务明确要求时执行 | 显式 paper；验证止损、心跳、持仓衔接并留证 |
| D4 live | 真实账户、真实资金、live 切换 | 禁止执行 | 代码存在不构成授权 |

`POST /pause`、`/resume`、扫描、回滚、批准等都属于有状态控制动作，不是普通只读检查；
未经当前任务授权不要调用。HTTP 层不得新增下单或撤单接口。

## 四、标准开发闭环

### 1. 定界

- 把用户目标改写成可核验清单，区分“诊断”“实现”“运行操作”。
- 用 `git status --short`、`git diff -- <file>` 判断文件中是否已有用户改动。
- 用 `rg` 和 `tools/code_graph.py --query ...` 找调用方、契约、状态文件和相关测试。
- 确认变更等级；D2/D3 必须先列出会触及的安全不变量。

### 2. 占用

```bash
python3 tools/agent_notes.py status
git log --oneline -3 -- <file>
python3 tools/agent_notes.py claim <会话标签> <file1> [file2...]
```

同一文件同一时刻只有一个写者。遇到活跃 claim 时换文件或等待；遇到未 claim 的用户改动时
保留其内容，不能用 reset、checkout 或整文件覆盖来“清理”工作区。

### 3. 实现

- 先改稳定契约，再改实现和调用方；跨功能包只依赖公开 Protocol/ABC、领域模型或 `*_api.py`。
- 参数只在 `config.py` 的统一维护区定义，业务模块只引用，不复制参数字面量。
- 数据库写入经 repository，服务查询经 `storage/query_api.py`，服务运行控制经
  `TradingRuntimePort` 或 `decision/api.py`。
- 新 bug 当场按“现象 → 根因 → 修复 → 预防”追加 `docs/reports/pitfalls.md`。
- 不顺手修改无关文件，不覆盖用户改动，不把调试产物或运行态状态加入 Git。

### 4. 验证

- 先运行最窄的相关测试，再运行结构守卫；风险越高，证据范围越大。
- 测试数据库、事件流和 runtime 目录必须隔离，不能指向活体文件。
- D3 依次执行：静态检查 → 离线测试 → paper 沙盘链 → 受控重启 → 心跳/持仓/条件单核对。
- 失败时报告真实结果；不要用重跑到绿、删测试、改断言或清状态掩盖问题。

### 5. 交付

- 用 `git diff --check` 和 `git diff --stat` 复核改动范围。
- 按第九节模板逐项给出实测证据、未做项和剩余风险。
- 不提交代码时也要 release：`python3 tools/agent_notes.py release <会话标签>`。

## 五、任务路由

| 任务 | 先读 | 主要入口 | 最小验证 |
|---|---|---|---|
| 文档/AI 入口 | 本页、`docs/README.md` | `AGENTS.md`、`llms.txt`、docs 索引 | AI 仓库自检及其变异测试 |
| 服务/API | 接口优先分层、exchange layers | `service/app.py`、`models.py`、`worker.py` | service API + interface boundaries |
| 运行时组装 | 接口优先分层 | `service/main.py`、`engines/runtime_api.py`、`directional_trader.py` | service API + production guard + code graph |
| 信号/候选 | pitfalls、参数规则、准确率终审稿 | `engines/signal_scan.py`、`signal_sampling.py`、`strategy_b.py` | signal sampling + decision loop + strategy B |
| 开仓/数量/止损 | 安全不变量、exchange layers | `engines/position_mgmt.py`、`risk_monitor.py`、`execution/quantity.py` | exchange layers + production guard + 风控相关测试 |
| 复盘/经验/进化 | self-evolution 终审设计、feature schema | `engines/review_pipeline.py`、`decision/review_engine.py`、`evolution_gate.py` | phase/review/gate/lesson 测试 |
| 交易所接入 | exchange layers、API 类 pitfalls | `exchange/base.py`、适配器、transport | FakeAdapter 全链 + venue/单位测试 + code graph |
| 数据库/schema | SQLite pitfalls、接口优先分层 | `storage/db.py`、`*_repository.py`、`query_api.py` | 临时库迁移 + isolation lint + interface boundaries |
| 因子/概率/极值 | 准确率终审稿、重放报告 | `factors/`、`decision/entry_probability.py`、`extrema_forecast.py` | replay + factor gate + probability/calibration 测试 |
| Agent Harness | Harness 方案、建设路线图与主动提案终审稿 | `decision/agent_*`、`interfaces/agent.py`、`storage/agent_*` | contracts/context/policy/storage/evaluation/proposals |
| 运维/状态 | `docs/ops/`、AGENTS 红线 | `tools/health_check.py`、只读 HTTP | 只读证据；未经授权不重启、不写状态 |

计划与报告的具体路径从 `llms.txt` 进入；不要在本表复制容易漂移的完整文件清单。

## 六、文件与状态边界

| 类别 | 示例 | 处理规则 |
|---|---|---|
| 版本化源码 | `service/`、`engines/`、`decision/`、`storage/` | claim 后可按任务修改 |
| 版本化文档 | `AGENTS.md`、`llms.txt`、`docs/` | 同步维护入口和索引，链接必须可验证 |
| 测试临时状态 | 临时 DB、JSONL、runtime 目录 | 使用独立临时目录；测试后不得混入仓库 |
| 活体状态 | `*.db`、`*.db-wal`、`trade_journal.json`、`watchlist.json` | 默认只读；不得在进程运行时直接编辑 |
| 进程协调状态 | `heartbeat_*.txt`、`*.pid`、`engine*.lock`、`tick_*.txt` | 只由进程/运维工具维护，不手改 |
| 凭证与外部配置 | `okx_config.json`、仓库外 live 凭证、API token | 不读取、不输出、不提交；不要为测试复用 |

`.gitignore` 是运行产物清单的辅助事实源，但“被忽略”不代表“可以安全删除或覆盖”。任何状态
迁移都要先确认实例、路径、写者和恢复方案。

## 七、稳定架构与运行链

依赖方向：

```text
service → engines → decision/execution/risk/strategy
        → storage/exchange/data → interfaces/config
```

`tools`、`tests`、`backtest` 是外围入口；`factors` 是离线研究层，不拥有交易执行权限。
实际边界以 `docs/architecture/2026-08-23_interface_first_layering.md` 和
`python3 tools/code_graph.py --check` 为准。

关键链路：

- 服务链：`service.main → TradingWorker → DirectionalTrader → 功能 Mixin/公开 API`。
- 控制链：`service.app → TradingRuntimePort / decision.api / storage.query_api`；HTTP 不读取
  trader 私有字段，不掌握 SQL，不暴露订单接口。
- 候选链：已收线 15m K → 去重候选 `signal_samples` → 4h/1m 完整路径
  `signal_outcomes`；被规则、额度或 Agent 拒绝的候选仍可保留反事实标签。
- 研究链：候选/结果 → feature registry → purged walk-forward → factor trials / model
  artifacts；模型只做 meta-label，不能绕过结构信号和交易闸门。
- Agent 链：版本化 context → 证据范围 memory/只读 tools → model contract → 确定性 policy
  → runs/steps/evaluations；主动提案没有执行权限，故障必须回到量化基线或失败关闭。
- 执行链：名义金额 → 合约单位与 lot 向下取整 → 风险/总敞口校验 → 开仓 → 交易所侧止损
  → journal/ownership 对账；任一关键环节失败都不能留下无保护仓位。

状态迁移、模型生命周期和策略演进必须可追溯、可回滚。达到某个状态名不等于已证明正收益，
训练样本、shadow 样本和自然 paper 结果不得混作同一种证据。

## 八、验证矩阵

按改动范围选取并记录真实输出，不写固定“当前测试文件数”；数量用自动发现结果为准。

```bash
# 文档入口、链接、索引和护栏
python3 tools/ai_repo_check.py
python3 tests/test_ai_repo_check.py

# 改动 Python 文件（显式把字节码缓存留在可写临时目录，兼容 macOS 沙箱）
PYTHONPYCACHEPREFIX=/tmp/crypto-agent-pyc python3 -m py_compile <改动文件...>

# import、跨层契约或共享状态
python3 tools/code_graph.py --check
CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_interface_boundaries.py

# 参数、运行目录和历史修复护栏
python3 tools/params_lint.py
python3 tools/test_isolation_lint.py
python3 tools/fix_guard.py

# 服务与交易所离线主链
CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_service_api.py
CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_exchange_layers.py
```

全量回归以 `.github/workflows/ci.yml` 为唯一命令事实源：它自动发现 `tests/test_*.py`，并为
每个脚本隔离 `CRYPTO_AGENT_DB`、`CRYPTO_AGENT_EVENTS_FILE` 和
`CRYPTO_AGENT_RUNTIME_DIR`。本地需要复现时也应使用同样隔离，不维护固定白名单。

D3 不能用离线测试代替沙盘证据。只有用户明确要求运行操作时，才可用显式 paper 模式：

```bash
CRYPTO_AGENT_MODE=paper PYTHONPATH=lib python3 -m service.main --port 8091
```

启动成功不等于完成；还需按任务核验 `/health`、`/status`、`/reconcile`、`/error`、持仓所有权
以及交易所侧条件单。

## 九、完成证据包模板

每次交付至少包含以下字段；不适用项明确写“不适用”，不要省略：

```text
目标：用户原任务的可核验重述
变更：文件 + 行为变化；未改动的安全边界
清单证据：每个目标对应测试输出、接口响应或数据行数
回归：实际运行命令；通过数/失败数
运行态：未触碰 / paper 验证详情 / 重启后心跳与持仓衔接
未做：项目 + 原因
风险：当前仍未知、样本不足或需要人工决策的部分
工作区：相关 diff；保留的用户原有改动
```

“代码已写”“单测应该覆盖”“接口能启动”都不是单独的完成证据。交易逻辑变更还要把原任务
清单与最终 diff 双向核对，防止漏项和越界实现。

## 十、机器守卫能做什么

`tools/ai_repo_check.py` 使用标准库检查：

- 根入口存在，根目录无散装 Markdown；
- 入口和 docs 的本地链接存在且不越出仓库；
- `llms.txt` 覆盖安全、协作、架构和历史经验入口；
- `docs/README.md` 索引全部 docs 文档；
- `AGENTS.md` 保留 paper、claim、自检、分层和踩坑护栏。

`tools/code_graph.py --check` 检查分层反向依赖、接口绕过、跨层共享状态和 import 环；
`params_lint.py`、`test_isolation_lint.py`、`fix_guard.py` 分别守参数集中化、测试隔离和已修问题。

这些工具不能理解全部自然语言语义，也不能证明收益、实时状态、交易所条件单或外部数据质量。
静态代码图对动态调用和变量间接路径只是尽力解析；通过机器守卫仍需按变更等级补足人工证据。

## 十一、文档维护与新鲜度

- 本页只写稳定开发规则，不保存会快速过期的持仓、PnL、样本数、测试文件数或心跳快照。
- 当前运行状态从只读接口取：`/health`、`/status`、`/reconcile`、`/error`。
- 当前研究成熟度从 `/research/readiness`、`/factors/trials`、`/models/entry`、
  `/forecast/calibration` 和 `/agent/evaluation` 取；历史裁决看带日期的 reports。
- 新增/移动 docs 时同步 `docs/README.md`；新增关键入口时同步 `llms.txt`。
- 新增顶层功能模块时必须同时增加局部 `AGENTS.md`、更新 `llms.txt` 和机器守卫模块清单。
- 当前行为写现在时；历史决策写日期和证据来源；计划不得写成已实现，离线结果不得写成活体结果。
- 接口、运行模式或安全边界变化时，同步核对 `AGENTS.md`、`README.md`、Pydantic schema、
  架构文档、运维手册和机器护栏，不能只改其中一处。
- 文档发现代码与规则冲突时，先修权威事实源或升级确认，不用模糊措辞把冲突藏起来。

## 十二、已知边界

- 历史回测尚未证明方向性策略长期正期望；系统安全目标是损失有界，不是承诺收益。
- AI 只获准操作 OKX 模拟盘；live 代码、配置和外部凭证不属于默认开发授权。
- 只做 SWAP；无合约场所或最小下单量不满足时拒绝，不放大数量凑单。
- Agent、因子、概率和极值模型都受既有结构信号、风险门、生命周期和证据成熟度约束，不能直接下单。
- 当前某项研究是否成熟、活体是否健康，必须在本次任务中重新读取证据，不能引用本页旧快照。

# 交易 Agent Harness 建设路线图

> 状态：权威实施稿（FINAL）
> 日期：2026-08-23
> 范围：OKX 模拟盘方向性交易 Agent Harness 的持续建设、统计验证与权限晋升
> 前置方案：`2026-08-23_agent_harness_improvement_FINAL.md`、`2026-08-23_agent_active_proposal_shadow_FINAL.md`
> 核心原则：先把 Harness 建成可信的实验与证据系统；只有样本外证明净增量后，才讨论增加模拟盘权限。

## 1. 建设结论

Harness 的目标不是让 LLM 直接成为交易员，而是建设两个受约束的能力：

1. 风险评审员：审查量化基线准备放行的候选，只能提出风险否决建议。
2. 影子研究员：从既定候选池中提出方向性研究候选，积累独立反事实证据。

两条能力线都必须服从确定性策略核。Agent 不决定入场价格、仓位、杠杆、止损、止盈和预算，
不接触交易执行工具，不修改配置、Prompt、代码或风控参数。

当前 H0～H7 的运行骨架已经基本落地，下一阶段的主要矛盾不是缺少更多 Agent 功能，
而是缺少足够、可信、可复现的自然模拟盘成熟结果。因此建设重心从“功能开发”切换为：

`稳定采样 → 数据核账 → 增量评价 → Challenger 实验 → 人工晋升 → 观察回滚`

## 2. 授权与安全边界

本路线图只授权 OKX 模拟盘，不启动、切换或操作 live 实例。

以下边界在所有阶段保持不变：

- 规则与硬风控先于 Agent；基线拒绝的候选，Agent 无权恢复。
- `AGENT_HARNESS_VETO_ENABLED=False` 是默认状态；未经过独立授权不得开启。
- Agent 无权生成订单、撤销订单、加仓、调整杠杆或修改止损止盈。
- 单笔风险 1%、名义上限 150 USDT、组合敞口上限 600 USDT 不得放宽。
- 每笔真实模拟盘开仓仍必须附带交易所侧止损。
- HTTP 层只保留观测能力，不增加下单、撤单、激活 veto 或修改配置接口。
- 模型失败不得伪装成批准；no-key、timeout、HTTP、解析和 schema 错误分别落账。
- 未成熟结果、历史研究重放和其他策略样本不得冒充自然 paper Harness 证据。

## 3. 当前基线

已经落地的能力包括：

- 版本化 `AgentInput`、严格 `AgentDecision` 和运行状态契约。
- 固定顺序、带 as-of 与缺失语义的 Context Builder。
- episodic、semantic、procedural 三层记忆及成熟度、scope、衰减约束。
- 白名单只读工具、步数预算、工具预算和总超时预算。
- 确定性 Policy Kernel；Harness 默认只产生 `shadow_reject`。
- `agent_runs`、`agent_steps`、`agent_evaluations` 完整 Trace。
- 15m 候选到 4h/1m 首触、MFE/MAE、费用后结果的反事实评价链。
- champion/challenger 生命周期与版本级回滚能力。
- `/agent/status`、`/agent/runs`、`/agent/evaluation` 和 `/research/readiness` 只读观测。
- `C_agent_proposal` 主动候选影子线；固定 `execution_authority=false`。

当前仍未完成的不是代码接线，而是统计证明：Harness 必须取得足够自然模拟盘成熟结果，
并证明相对纯量化基线的费用后增量，而不能用少量盈利、主观解释或历史重放替代。

## 3.1 技术选型修订：P0 提前引入 LangGraph

用户于 2026-08-23 决定提前采用 LangChain 体系，避免未来随着节点、持久化和人工审批增加
后再做高成本迁移；随后明确要求 paper/live 共用同一套并移除旧流程。本路线图收敛为：
**LangGraph 是唯一 Harness 编排运行时，LangChain 负责模型 Runnable 与结构化输出；
paper/live 共用该实现，不保留手写编排分支。**

选择 LangGraph 而不是整套 LangChain Agent 的理由：

- LangGraph 官方定位为低层、有状态、长流程 Agent 编排运行时，可单独使用，不要求依赖
  LangChain 的高层 Agent 抽象。
- `StateGraph` 的 State、Node、Edge 与本仓库现有 Context、Memory、Tool、Model、Policy、
  Recorder 边界可以逐一映射，不需要重写业务契约。
- checkpoint、故障恢复、状态历史和 human-in-the-loop 可作为后续版本晋升与人工批准的基础。
- 只迁移 orchestration，不迁移事实源、风控、策略核和交易执行，能够把早期采用的风险
  限制在影子路径。

官方依据：

- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)：LangGraph
  是低层编排运行时，可独立于 LangChain 使用，核心能力包括 durable execution、
  human-in-the-loop 和 persistence。
- [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)：工作流由显式 State、
  Node 和 Edge 构成，适合保留确定性控制流。
- [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：checkpointer 按
  thread 保存各步骤状态，支持状态历史、重放和故障恢复。

首版图节点固定为：

`START → context → retrieve → tools → model → validate → policy → record → END`

其中：

- 节点只是现有公开函数的薄适配器，不在节点内重新实现业务逻辑。
- `AgentInput`、`AgentDecision`、`HarnessRun` 和 `PolicyResult` 继续作为稳定契约。
- `PolicyKernel` 继续是最终权限边界，不能被条件边或模型输出替代。
- `storage.agent_harness` 继续是业务审计事实源；LangGraph checkpoint 只保存编排状态，
  不替代成熟结果和增量评价表。
- `ReadOnlyToolRouter` 继续维护工具白名单；首版不使用可以自动发现工具的高层 Agent。
- 真实 OKX 的 paper/live 都装配同一个风险评审 Harness provider；两边仍固定 shadow。
- FakeAdapter 和离线测试只使用显式注入 callback，永不自行访问外部模型。
- 主动候选 `C_agent_proposal` 仍是独立 paper-only 研究线，不随风险 Harness 进入 live。

一次性替换规则：

1. `decision/agent_harness.py` 只保留稳定 API 门面，内部唯一调用 LangGraph，不保存旧循环。
2. 每个候选只调用模型一次，禁止双跑、重复计费或产生两份有效 run。
3. 正常、reject、abstain、no-key、timeout、畸形 JSON、schema error 和 tool error
   全部用离线固定输入回归既有安全语义。
4. 任何行为漂移、重复副作用、checkpoint 污染、延迟超限或故障降级语义变化都 fail-safe
   回量化基线；不以恢复旧编排掩盖问题。

运行基线同步升级为 Python 3.12、LangChain 1.x 和 LangGraph 1.x。新环境统一使用 `.venv`；
旧 Python 3.9 `lib/` 只作末级兼容路径，不能覆盖虚拟环境依赖。

首版暂不使用 `interrupt()` 处理交易许可。官方说明 interrupt 恢复时会从节点开头重新执行，
因此 interrupt 前的副作用必须幂等；在本仓库完成重复副作用专项验证前，人工批准仍采用现有
离线生命周期流程，不能通过图恢复触发任何交易动作。

### 3.2 第一批实施状态（2026-08-23）

已落地：

- Python 运行基线升级到 3.12，新增 `.python-version`，本地统一使用隔离 `.venv`，CI 同步
  从 3.9 升到 3.12。
- `requirements.txt` 加入 LangChain 1.x 与 LangGraph 1.x；本地验证版本为 LangChain
  1.3.16、LangGraph 1.2.11。
- `decision/agent_graph.py` 实现唯一 `StateGraph`：context、retrieve、tools、model、
  validate、policy、record 七个显式节点。
- 模型调用通过 LangChain `RunnableLambda`，结构化结果通过 `PydanticOutputParser` 后，
  再进入仓库原有严格领域校验，框架解析不能放宽 JSON 和 evidence 约束。
- `decision/agent_harness.py` 的旧手写编排已删除，只保留 `run_harness` 兼容门面并委托
  LangGraph；不存在 paper/live 分支或旧 runtime 开关。
- 真实 OKX paper/live 均装配同一个风险 Harness provider，两边固定 shadow；主动候选 C
  仍保持 paper-only。
- 同一 `run_id` 命中 durable run 时直接返回，不再次调用模型；从“数据库一行”升级为
  “账本与模型计费同时幂等”。
- 旧 Python 3.9 `lib/` 从强制优先改为末级兼容路径，防止覆盖 Python 3.12 `.venv` 的
  NumPy/Pydantic 等二进制依赖。

离线证据：

- LangGraph 专项 9 项通过：显式节点、唯一运行时、严格结构化输出、只读工具、单次模型
  调用、durable 幂等、量化基线优先、故障状态分离、显式 veto 权限。
- 现有 Harness 端到端 10 项通过，Agent 增量评价 9 项通过。
- Python 3.12 临时隔离副本自动发现全部 53 个 `tests/test_*.py`，全绿、失败 0。
- `code_graph --check`、`params_lint`、`test_isolation_lint`、`fix_guard`、`ai_repo_check`
  与 AI 入口变异测试全部通过。
- 未启动、切换或重启 paper/live 实例；未改变 `AGENT_HARNESS_VETO_ENABLED=False`，未进行
  真实模型调用或交易操作。

## 4. 目标证据链

每个结构候选必须形成下列闭环：

`Signal Sample`
`→ Agent Input Snapshot`
`→ Context / Memory / Tool Trace`
`→ Model Decision`
`→ Deterministic Final Action`
`→ 4h/1m Mature Outcome`
`→ Agent Evaluation`
`→ Scoped Mature Memory`

### 4.1 风险概率先验（2026-08-24）

- 每个 forecast 冻结 `p_loss_prior=P(SL first)+0.5×P(timeout)`；超时结果未知时固定取中性 50%，
  不从当前样本中搜索权重。
- Agent abstain 表示没有足够市场证据调整先验，输出风险概率必须在先验 ±0.02 内；越界进入一次有界
  semantic repair，仍不合法则 schema error 并回量化基线。
- approve/reject 只有引用当前时点冻结证据时才可明显调整；模型不能把先验本身当证据，也不能用
  模型未激活、预测未校准或策略路由状态作为风险理由。
- 版本身份升级为 `harness-risk-v5-forecast-loss-prior`；旧版本结果保留但不混计晋升样本。

每一环都必须能通过稳定 ID 关联，并满足：

- 同一 `signal_id + harness_version` 幂等。
- 输入、Prompt、模型、Context、Schema、Retrieval 和工具策略均可定位版本。
- 相同冻结输入可以离线重放。
- 运行失败、模型判断和系统最终动作三类状态相互独立。
- 被拒候选仍持续结算，避免只评价成交样本造成选择偏差。
- 成熟评价不会因重试退回 pending。
- 记忆只消费成熟、同 scope、无未来泄漏的证据。

## 5. 建设批次

### P0：Shadow 采样生产化

目标：让自然模拟盘结构候选稳定产生可成熟、可重放的 Harness 记录。

建设内容：

- 新增唯一 LangGraph `StateGraph` 编排，节点只包装既有 Context、Memory、Tool、
  Model、Validator、Policy 和 Recorder。
- 增加冻结输入安全语义回归，覆盖正常、reject、abstain、no-key、
  timeout、畸形 JSON、schema error 和 tool error。
- `run_harness` 稳定入口直接委托唯一 LangGraph 实现，不存在模式分支或旧运行时开关。
- 真实 OKX paper/live 共用风险 Harness provider；两边均保持 shadow。
- 保持 Harness 在所有额度、分数和严格 2:1 拒绝门之前采样。
- 每根已收线 15m K 使用稳定候选身份，禁止五分钟重试产生重复有效运行。
- 保证 worker 持续推进 signal outcome、mature evaluation 和 scoped memory 回流。
- 将 disabled、no-key、timeout、HTTP、解析、schema 和 tool 错误全部写入 Trace。
- 对连续无样本、pending 积压、成熟链断裂、故障率异常和版本漂移增加运维告警。
- 监控模型延迟、降级率、重复一致性、token 和估算成本。

验收证据：

- 固定输入和固定模型输出下的 decision、runtime status、final action、Trace 顺序和稳定
  hash 均满足既有契约。
- LangGraph 节点无法取得 exchange、订单、配置写入或风控修改对象。
- 每个候选只产生一次模型调用和一个有效 run。
- 候选运行留痕覆盖率 100%。
- 同一候选和 Harness 版本最多一个有效运行。
- 任意一条 run 能定位输入 hash、版本、步骤、输出、最终动作和成熟结果。
- Harness 故障不阻断风控监控、候选结算和现役量化链。
- 测试、研究重放和 FakeAdapter 不写入自然 paper 统计。

### P1：数据质量与持续核账

目标：证明评价数据是真实、因果、去重且按策略隔离的。

建设内容：

- 每日核对 `signal_samples_canonical`、`agent_runs`、`agent_evaluations` 的数量关系。
- 按 `strategy_id`、方向、timeframe、horizon、Harness 完整版本过滤评价样本。
- 检查 1m 路径是否连续，TP/SL 同分钟触发时保持 ambiguous，不主观选胜方。
- 检查候选时点以后生成的数据没有进入 AgentInput、记忆或历史重放。
- 校验 pending 记忆不可检索，stale 记忆退出决策检索但保留审计原文。
- 核对 `runtime_status`、`model_verdict`、`final_action` 没有语义混写。

验收证据：

- 候选、run、成熟评价和记忆回流可逐条对账。
- 未来泄漏、重复样本、跨策略借样本和测试污染均为 0。
- 缺失路径如实保持 missing，不使用插值结果冒充完整路径。
- `/research/readiness` 与底层可复算计数一致。

### P2：固定增量评价

目标：用相对量化基线的净增量裁决 Harness，而不是评价理由是否好听。

最小样本门：

- 至少 100 条已有成熟结果的有效判断。
- 模型 reject 至少 30 条。
- 样本必须来自自然模拟盘 Harness 运行；历史研究重放不抵扣该门槛。

固定指标：

- reject 数、reject 覆盖率。
- 拦亏精确率和 `saved_loss`。
- 错拦盈利率和 `missed_profit`。
- 模型调用成本和故障降级成本。
- `incremental_ev = saved_loss - missed_profit - model_cost`。
- 增量 EV 的单侧 95% 保守下界。
- 风险概率的 Brier 分数与分桶校准。
- abstain 的条件价值。
- long/short、币种、regime、月份和消息类型稳定性。
- 最大单一分段贡献占比，防止单币或单场景支撑全部结论。

晋升必要条件：

- `saved_loss > missed_profit + model_cost`。
- 样本外增量 EV 的保守下界大于 0。
- 风险概率不劣于简单经验频率基线。
- 增量不依赖单币、单方向或单一 regime。
- 运行失败率、降级率和重复不一致率处于可接受范围。

任一条件不满足，版本保持 shadow 或进入 rolled-back；不得解释性放宽标准。

### P3：Champion/Challenger 实验制度

目标：让每一次 Harness 改动都可归因、可比较、可回滚。

版本单位：

- model version
- prompt version
- context version
- schema version
- retrieval version
- tool policy version

实验规则：

- 每轮优先只改变一个主要变量。
- champion 与 challenger 消费相同的冻结输入和相同结果标签。
- challenger 先跑离线 fixture 与故障/攻击回归，再进入 paper shadow。
- 新版本不得覆盖旧版本 Trace、评价和生命周期记录。
- 新版本未过 P2 门前不能影响开仓。
- Prompt、模型、检索和工具策略不得在线自修改。

首批适合验证的 challenger：

1. 风险理由压缩：减少无证据自然语言，只保留标准 reason code 和 evidence ID。
2. 概率校准：验证风险概率是否优于只输出 approve/reject。
3. 检索消融：比较无记忆、同 scope 记忆和加入 verified lesson 的真实增量。
4. 数据质量优先：验证 stale/missing critic 是否能降低错误自信，而不是扩大 reject。

### P4：人工批准 Paper Veto

目标：只让已经证明净增量的版本在模拟盘获得有限否决权。

晋升链：

`candidate → shadow → validated → active-veto → observing → kept/rolled-back`

权限规则：

- worker 可以按证据自动登记 candidate、推进 shadow，并在满足门槛后到 validated。
- validated 不等于获得交易权限。
- `active-veto` 必须由用户对该版本和证据包单独明确授权。
- 激活通过受控离线运维流程完成，不增加 HTTP 写接口。
- Agent reject 只有具备合法 reason code、evidence ID 和完整 schema 时才可能形成 veto。
- approve、abstain、运行故障均保持现役量化动作，不产生额外放行能力。

激活前证据包：

- Harness 完整版本身份。
- 100/30 样本门实测计数。
- saved loss、missed profit、模型成本和增量 EV。
- 增量 EV 保守下界、Brier 和分段稳定性。
- prompt injection、畸形输出、超时、429/5xx、断网和工具失败回归结果。
- 全量测试和仓库机器守卫结果。
- 一键回滚演练结果。

### P5：观察、保留与回滚

目标：防止离线有效、上线退化的版本长期影响模拟盘。

观察门：

- active-veto 后至少观察 60 个新去重候选，或 30 笔新平仓。
- 观察样本必须晚于激活时间，不能复用晋升数据。
- 继续同时计算基线反事实，保留机会成本比较。

回滚条件：

- 增量 EV 或其保守下界转为非正。
- `missed_profit` 增长超过 `saved_loss`。
- Brier 或分桶校准显著恶化。
- 效果集中到单一币、方向或 regime。
- 输出不稳定、故障降级率或延迟显著上升。
- Agent 接触到未授权工具、配置写入或执行路径。

回滚不删除失败版本和实验数据；保留完整证据用于后续研究。

### P6：主动候选研究线

目标：验证 AI 是否具备独立发现方向候选的增量，但不授予执行权限。

建设规则：

- 使用独立 `strategy_id=C_agent_proposal`，不与 A_pullback、B_breakout 混样本。
- 每根已收线 15m K 最多一个幂等批次，每批提出 0～2 个候选。
- 标的必须来自每日候选池；evidence ID 必须真实存在于输入快照。
- 入场参考、1R 止损、2R 止盈、成本和概率门全部由确定性代码计算。
- 没有同策略、同方向的 validated active 概率模型时，只记录
  `no_validated_active_model`，不能把理论 2:1 当作正期望。
- 所有结果固定 `execution_authority=false`，继续结算完整 4h/1m 反事实路径。

未来如要赋予 C 策略任何影响交易的权限，必须另立方案，独立满足因子、概率、校准、
费用后 EV、DSR/PBO、稳定性、人工批准和回滚要求。本路线图不授予该权限。

## 6. 运维节奏

### 每个 15m 周期

- 生成去重候选和 Harness run。
- 检查版本、input hash、运行状态和最终 shadow 动作。
- 失败时记录降级语义，不阻断现役规则链。

### 每小时

- 推进到期候选的 4h/1m outcome。
- 将成熟 outcome 写入 agent evaluation。
- 将合格成熟评价转为 scoped memory。
- 检查 pending 积压和成熟链断裂。

### 每日

- 查看 `/agent/status` 和 `/agent/runs` 的调用健康。
- 核对候选、run、evaluation、memory 数量关系。
- 查看 `/agent/evaluation` 的样本量、reject、Brier 和增量 EV。
- 查看 `/research/readiness`，确认历史研究样本没有抵扣自然 paper 门槛。

### 每达到一个新增成熟样本批次

- 固定版本运行增量评价。
- 生成 champion/challenger 同输入比较。
- 检查 long/short、币种、regime 和月份稳定性。
- 更新生命周期，但自动流程最多推进到 validated。

## 7. 建设优先级

### P0：立即建设

1. 完成唯一 LangGraph/LangChain 编排和冻结输入安全语义测试。
2. 升级 Python 3.12 隔离运行基线，移除旧编排实现。
3. 让真实 OKX paper/live 共用风险评审 Harness，保持 shadow 和零执行权限。
4. 保证 shadow 采样和成熟结果链持续运行。
5. 增加连续无样本、pending 积压、成熟链断裂和高故障率告警。
6. 建立每日候选/run/evaluation/memory 自动核账。
7. 积累 100 条有效成熟结果和 30 条 reject。

### P1：样本达到门槛前并行建设

1. 固化增量评价报告和数据复算入口。
2. 建立 challenger 版本登记与单变量实验模板。
3. 完善概率校准、分段稳定性和故障攻击回归集。
4. 演练版本回滚，但不激活 veto。

### P2：证据达标后建设

1. 提交完整的 active-veto 人工批准证据包。
2. 只在 OKX 模拟盘启用指定版本。
3. 进入 60 个候选或 30 笔平仓的独立观察期。
4. 保留 champion 并持续计算反事实，满足条件后 kept，否则回滚。

### P3：长期研究

- 新闻风险、市场结构和数据质量专用 critic。
- 按任务复杂度进行模型路由。
- 上下文压缩、语义检索和跨模型一致性。
- 决策解释质量评分。

P3 只在 P0～P2 稳定后评估；不引入自由多 Agent 辩论，不开放写工具或执行工具。

## 8. 停止条件

出现以下任一情况，停止版本晋升并保留 shadow 数据：

- 运行记录或输入快照不能完整复现。
- 存在未来数据泄漏、跨策略借样本或测试污染生产数据。
- 有效样本少于 100，或 reject 少于 30。
- 成本后增量 EV 非正，或其保守下界不大于 0。
- 错拦盈利机会成本不低于拦亏收益。
- 概率校准劣于经验频率基线。
- 增量主要来自单币、单方向或单一 regime。
- 输出不稳定、故障率过高或模型调用阻塞现役链路。
- prompt injection 或未注册工具调用能够改变最终动作。
- Agent 可以接触执行、配置写入或风控修改路径。
- 长期样本外结果仍为负。

停止晋升不等于停止采集，也不删除失败实验。保持空仓或现役基线，用独立新样本继续证伪。

## 9. 完成定义

只有以下条件同时满足，才能声明 Harness 建设完成一个有效晋升周期：

1. 每个候选都有版本化输入、步骤、输出、最终动作和成熟结果审计链。
2. Trace 覆盖率 100%，同候选同版本幂等，失败语义与模型批准严格分离。
3. 上下文、记忆和历史重放没有未来泄漏或跨策略污染。
4. 至少取得 100 条自然模拟盘有效成熟判断，其中 reject 不少于 30 条。
5. saved loss 大于 missed profit 与模型成本之和，增量 EV 保守下界大于 0。
6. 概率校准和跨币种、方向、regime、月份稳定性达到门槛。
7. champion/challenger 同输入比较、人工批准、观察期和回滚演练闭环通过。
8. Agent 始终没有新增交易、放宽风控、修改参数或访问执行工具的权限。
9. 全量测试与机器守卫红 0，生产数据库和事件文件未被测试污染。
10. 若进入 active-veto，完成 60 个新候选或 30 笔新平仓观察且未退化。

在上述统计证据形成前，只能声明“Harness 基础设施已接线、正在 shadow 采集”，
不能声明“Agent 已提高开仓准确率”或“Agent 已产生正收益”。

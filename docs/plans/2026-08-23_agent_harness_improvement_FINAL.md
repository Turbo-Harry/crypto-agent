# 交易 Agent Harness 能力提升计划

> 状态：权威实施稿（FINAL）
> 日期：2026-08-23
> 分支：`codex/agent-harness-plan`
> 范围：OKX 模拟盘方向性交易 Agent 的运行框架；不改变策略参数与交易安全边界
> 核心原则：模型只提供受约束的风险评审，确定性策略核拥有最终权限；所有能力先可观测、再影子验证、最后才允许生效。

## 1. 目标与非目标

## 0. 可借鉴的公开实现（仅借鉴 Harness，不引入依赖）

本方案不直接照搬任何交易机器人，而是借鉴已经验证过的 Agent 运行时模式：

- OpenAI Agents SDK：把输入/输出/工具 guardrail、结构化输出、trace/span 和确定性测试作为一等能力；对应本方案的 Schema Validator、Tool Router、Decision Recorder 与回归集。
- LangGraph：用 checkpoint、thread/run 标识、可恢复执行和幂等副作用支撑长流程；对应本方案的 `run_id`、`agent_steps`、重试/恢复和审计重放。
- OpenAI Evals/脚本化模型测试：把 orchestration 与真实模型行为拆开，先用固定输出测试工具、策略和错误路径；对应本方案的离线 fixture、shadow 回放和 champion/challenger。
- 量化交易系统的 paper/shadow 模式：策略核保留最终权限，模型只能做风险评审；所有模型输出必须经过硬风控、版本化和成熟结果评价。

这些参考实现的共同点是“可恢复、可观测、可拒绝、可回放”，而不是多 Agent 自由讨论。当前仓库优先采用轻量本地实现，避免为了 Harness 引入新的运行时依赖。

### 1.1 目标

1. 把当前“一次同步 LLM 二判”升级为可复现、可审计、可评测、可回滚的 Agent Harness。
2. 建立版本化上下文、结构化输出、受控只读工具、分层记忆和确定性策略核。
3. 明确区分模型结论、运行状态与系统最终动作，基础设施降级不得伪装成模型批准。
4. 用完整反事实结果评价 Agent 是否产生净增量，而不是用主观理由或少量盈利判断能力。
5. 支持 champion/challenger 影子比较，使 prompt、模型、检索和工具策略都能独立验证与回滚。

### 1.2 非目标

- 不允许 LLM 直接下单、撤单、加仓、调整杠杆或修改止损止盈。
- 不允许 Agent 绕过规则信号、1% 单笔风险、150 USDT 名义上限、600 USDT 总敞口或交易所侧止损。
- 不在本计划中启动、切换或操作 live 实例。
- 不用多 Agent 自由辩论替代数据和评测；v1 先完成单 Agent 的确定性 Harness。
- 不让 Agent 根据少量交易自动重写 prompt、配置、策略参数或自身代码。

## 2. 当前基线与缺口

当前核心实现位于 `decision/agent_judge.py`，实际能力是一次同步模型调用：拼接字符串上下文，调用 DeepSeek，解析 `approve/reject/abstain`，再把少量历史案例回喂模型。

已具备的基础：

- AI 只否决、不主动放行新信号。
- 模型异常时回到现役规则，交易链不依赖模型可用性。
- 判断结果可与成交交易绑定，平仓后能回填结果。
- 有最小离线注入测试，覆盖 reject、approve、abstain、异常和解析失败。
- 本地止损监控已有独立线程，模型调用不会阻断交易所侧止损或专用监控线程。

主要缺口：

1. 无 Harness 状态机、工具协议、调用步数与总时间预算。
2. `approve`、无密钥、超时、HTTP 错误、解析失败可能收敛成同一放行语义，无法区分模型判断与系统降级。
3. 异常路径不完整落库，无法统计可用率、失败率和降级影响。
4. 无 prompt、模型、上下文、输出 schema 和检索策略版本，历史判断不可完整复现。
5. 无输入快照哈希与幂等键，同一候选可能重复调用、重复计费。
6. 记忆检索主要取近期同方向案例，缺少策略版本、币种、regime、timeframe、证据成熟度和多样性约束。
7. 历史案例只保存结论与简短理由，缺少当时上下文、检索证据和工具输出。
8. 模型输出只有 verdict/reason，没有风险概率、置信度、标准 reason code、证据 ID 与缺失信息。
9. 被否决候选的旧评价使用到期单点价格，不能表达 TP/SL 首触、路径风险和机会成本。
10. 无 token、延迟、成本、重试、限流、熔断和模型供应商状态观测。
11. 无 prompt injection、过期数据、矛盾数据、429/5xx、畸形 JSON 等 Harness 级回归集。
12. 无 champion/challenger 和上线后退化回滚闭环。

## 3. 目标架构与权责边界

目标链路：

`规则/风控硬门 → Context Builder → Memory Retriever → Read-only Tool Router → Risk Critic → Schema Validator → Policy Kernel → Decision Recorder → Outcome Evaluator`

权责原则：

- 规则与风控硬门先执行；未通过时 Agent 无权恢复交易。
- Agent 输出是建议，不是执行命令。
- Policy Kernel 是普通确定性代码，负责把模型结论映射为最终动作。
- Tool Router 只暴露白名单只读工具，不暴露 exchange 下单、撤单或配置写入。
- Recorder 对所有路径留痕，包括 disabled、no-key、timeout 和 parse-error。
- Evaluator 只使用成熟、无未来泄漏的结果标签评价 Agent。

## 4. 核心数据契约

### 4.1 AgentInput

建议字段：

- 身份：`run_id/signal_id/event_ts/kline_ts/strategy_version`。
- 版本：`prompt_version/model_version/context_version/schema_version/retrieval_version`。
- 信号：币种、方向、timeframe、入场、止损、止盈、ATR、连续分与六维子分。
- 市场：1H/4H regime、波动、订单流、OI、basis、资金费率。
- 消息：情感与新闻摘要、来源时间、新鲜度和缺失状态。
- 账户：当前持仓、方向敞口、组合敞口、风控与熔断状态。
- 健康：行情延迟、交易所状态、特征缺失和异常中心摘要。
- 记忆：检索案例 ID、经验 ID、相似度、证据强度与结果成熟度。
- 审计：字段来源、as-of 时间与整体 `input_hash`。

### 4.2 AgentDecision

模型必须输出严格结构：

- `verdict`: `approve | reject | abstain`
- `risk_probability`: `[0, 1]`
- `confidence`: `[0, 1]`
- `reason_codes`: 标准枚举列表
- `evidence_ids`: 使用过的上下文、案例或工具证据
- `missing_information`: 影响判断的缺失字段
- `abstain_reason`: abstain 时必填
- `reason`: 简短自然语言解释

首批 reason code：

- `news_direction_conflict`
- `extreme_market_event`
- `liquidity_failure`
- `stale_or_missing_data`
- `signal_inconsistency`
- `position_risk_conflict`
- `insufficient_evidence`

### 4.3 三类状态必须分离

- `model_verdict`：模型真正输出的 approve/reject/abstain。
- `runtime_status`：completed/disabled/no_key/timeout/http_error/parse_error/schema_error/tool_error。
- `final_action`：baseline_reject/baseline_pass/shadow_reject/agent_reject。

例如模型超时应记录为：`model_verdict=null`、`runtime_status=timeout`、`final_action=baseline_pass`，绝不能记成模型 approve。

## 5. 存储与可观测性

### 5.1 agent_runs

每次候选判断一行：

- `run_id` 主键，`signal_id` 幂等关联。
- 创建/完成时间、实例模式、运行状态、最终动作。
- prompt/model/context/schema/retrieval 版本。
- input hash、模型响应 hash、解析结果。
- 总延迟、模型延迟、token、估算成本、错误类型。
- champion/challenger 身份和父运行 ID。

### 5.2 agent_steps

记录 context、retrieve、tool、model、validate、policy 等步骤：

- step 序号、类型、状态、开始/结束时间。
- 工具名、输入摘要、输出摘要、证据 ID。
- 重试次数、错误类型和降级动作。

### 5.3 agent_evaluations

成熟结果一行：

- TP/SL/timeout/ambiguous。
- pnl_r、mfe_r、mae_r、净成本后 EV。
- saved_loss、missed_profit、incremental_ev。
- 评价版本、结算时间和标签来源。

### 5.4 只读观测接口

- `GET /agent/status`：现役版本、调用健康、shadow 状态。
- `GET /agent/runs`：最近运行、状态与错误。
- `GET /agent/evaluation`：样本量、拦亏、错拦和增量 EV。

接口只读，不增加 Agent 下单或运行时修改能力。

## 6. 分阶段实施计划

### H0：冻结基线与契约

范围：

- 固化当前 prompt 为 `judge-v1`。
- 定义输入、输出、运行状态和最终动作的数据模型。
- 将现有 `decision/agent_judge.py` 保留为兼容入口。
- 建立安全不变量测试夹具。

验收：

- 现有行为测试零回归。
- 非法输出不能形成有效 reject。
- 模型不能输出或间接改变方向、仓位、杠杆、止损和止盈。
- 运行失败与真实 approve 在数据层可区分。

依赖：无。

### H1：运行账本与完整 Trace

范围：

- 新增 `agent_runs/agent_steps/agent_evaluations` 表与迁移。
- 每个结构候选生成稳定 `run_id`，以 `signal_id + harness_version` 幂等。
- 所有成功、跳过、失败和降级路径落库。
- 添加只读状态与运行查询接口。

验收：

- 候选运行留痕覆盖率 100%。
- 同一候选同一 Harness 版本最多产生一个有效运行。
- 任一判断可从版本、输入 hash、证据和原始响应重建。
- 日志不包含 API key 或其他凭证。

依赖：H0；与候选 `signal_id` 数据链完成后接线。

### H2：版本化 Context Builder

范围：

- 把字符串拼接改为结构化、固定顺序的上下文。
- 每个动态字段带来源、as-of 时间、新鲜度与缺失状态。
- 设置整体 token 预算和各分区预算。
- 对新闻、经验和外部文本做数据边界隔离。

验收：

- 相同冻结快照得到相同 input hash。
- 未来数据不能进入历史重放。
- 过期与缺失信息显式可见。
- 上下文长度不会随数据库增长无限膨胀。

依赖：H0。

### H3：分层记忆与检索

记忆分层：

1. Episodic：历史候选、当时判断及完整路径结果。
2. Semantic：经过独立样本验证的 trusted/discarded 教训。
3. Procedural：不可被模型修改的安全政策与决策规则。

检索约束：

- 先过滤策略版本、方向、timeframe、结果成熟度。
- 再按币种/资产类别、regime、特征相似度排序。
- 加入证据强度、时间衰减与结果多样性。
- 禁止 pending 结果和同一事件派生样本作为成熟记忆。

验收：

- 不同方向、币种和 regime 的检索隔离测试通过。
- 未成熟结果不进入记忆。
- 检索结果包含稳定 evidence ID。
- 单一旧案例不能无限主导判断。

依赖：H1、H2、成熟候选结果标签。

### H4：受控只读工具层

首批工具白名单：

- `get_signal_snapshot`
- `get_market_regime`
- `get_risk_state`
- `get_positions`
- `get_sentiment_snapshot`
- `get_similar_cases`
- `get_verified_lessons`
- `get_market_health`

运行约束：

- 每次最多 2～3 次工具调用。
- 总时间预算集中配置；到期确定性退出。
- 只允许预注册工具和严格 schema。
- 工具只读，禁止任意 SQL、任意 URL、文件写入、配置修改和交易操作。
- 工具异常写 trace，不能静默吞掉。

验收：

- 达到步数/时间预算时能够稳定降级。
- 工具失败不产生重复调用或卡死扫描线程。
- 静态检查证明 Harness 无执行层工具路径。
- prompt injection 不能引导 Harness 调用未注册工具。

依赖：H1、H2。

### H5：确定性 Policy Kernel

映射规则：

- 规则/风控未通过：`baseline_reject`，Agent 无权恢复。
- 模型 approve/abstain：保持现役规则动作。
- 运行失败：`baseline_pass`，并单独记录降级状态。
- 模型 reject：shadow 阶段只记录；通过验证后才可映射为 `agent_reject`。
- 缺少有效 reason code、证据或 schema 校验失败的 reject 无效。

验收：

- Agent 无法放宽风控或生成新交易。
- 相同输入与模型结构化输出得到相同最终动作。
- 无效输出、注入文本和工具异常均不能绕过策略核。

依赖：H0、H4。

### H6：离线 Harness Eval

范围：

- 按时间顺序重放历史候选，严格使用当时可见上下文。
- champion 与 challenger 消费同一冻结输入。
- 用完整 1m 路径结果评价 TP/SL 首触、timeout、MFE/MAE 和净 EV。
- 评测数据库、事件文件和模型缓存全部与生产隔离。

核心指标：

- reject 数与覆盖率。
- 拦亏精确率与 saved_loss。
- 错拦盈利率与 missed_profit。
- 机会成本调整后的 `incremental_ev = saved_loss - missed_profit - model_cost`。
- risk probability 的 Brier/校准误差。
- abstain 的条件价值。
- 相同输入重复运行的一致性。
- 延迟、失败率、降级率、token 与成本。
- long/short、币种、regime、月份稳定性。

故障与攻击回归集：

- 缺字段、过期新闻、矛盾数据。
- no-key、timeout、429、5xx、断网。
- 畸形 JSON、未知 verdict、超长输出。
- 新闻和经验文本中的 prompt injection。
- 工具返回过期、空值或 schema 不符。

晋升样本门：

- 至少 100 条已有完整结果的有效判断。
- reject 类至少 30 条。
- saved_loss 大于 missed_profit 与模型成本之和。
- 样本外增量 EV 的保守置信下界大于 0。
- 不能依赖单币、单方向或单一 regime 获得大部分增量。

依赖：H1～H5、成熟候选结果标签。

### H7：Champion/Challenger、晋升与回滚

状态机：

`candidate → shadow → validated → active-veto → observing → kept/rolled-back`

规则：

- prompt、模型、检索、上下文和工具策略的任何变化都生成新 Harness 版本。
- challenger 只影子运行，不能影响开仓。
- 完整通过 H6 后才允许在 paper 实例启用 veto。
- 激活后至少观察 60 个新去重候选或 30 笔新平仓。
- 增量 EV、错拦成本、校准、稳定性任一显著退化即回滚。
- 版本晋升、回滚和证据写入实验注册表。

依赖：H6。

### H8：后续增强（非 v1 必需）

只有 H0～H7 稳定后再评估：

- 按任务复杂度做模型路由。
- 新闻风险、市场结构、数据质量专用 critic 并行。
- 上下文压缩与语义检索。
- 跨模型一致性检查。
- 决策解释质量评分。

以下能力仍不授权：自由多 Agent 辩论、自动改 prompt、自动改策略参数、写配置和交易工具访问。

## 7. 推荐代码落点

- `decision/agent_harness.py`：状态机与总编排。
- `decision/agent_contracts.py`：输入、输出、状态 schema。
- `decision/agent_context.py`：版本化上下文与 input hash。
- `decision/agent_memory.py`：三层记忆和检索。
- `decision/agent_tools.py`：只读工具注册表和预算。
- `decision/agent_policy.py`：确定性最终动作映射。
- `decision/agent_judge.py`：兼容入口，逐步收窄为 Harness adapter。
- `storage/db.py`：运行、步骤、评价表与迁移。
- `service/app.py` / `service/models.py`：只读观测接口。
- `tools/eval_agent_harness.py`：离线重放与 champion/challenger 报告。
- `tests/test_agent_harness_*.py`：契约、故障、注入、检索、重放与策略核测试。

依赖方向保持：`service → decision → storage`；Harness 不 import `engines` 或直接访问 exchange 执行方法，运行时数据由调用方以结构化输入注入。

## 8. 实施批次

### 批次 A：可审计基础

- H0、H1。
- 交付：严格契约、完整状态语义、运行账本、幂等与只读观测。
- 行为：不改变现役开仓结果。

### 批次 B：上下文和记忆可信

- H2、H3。
- 交付：as-of 上下文、数据新鲜度、三层记忆、证据化检索。
- 行为：仍只 shadow。

### 批次 C：受控 Agent Runtime

- H4、H5。
- 交付：只读工具、预算状态机、确定性策略核。
- 行为：reject 仍只记录，不拦单。

### 批次 D：评测、晋升和回滚

- H6、H7。
- 交付：离线重放、champion/challenger、增量报告、paper veto 与自动回滚。
- 行为：只有通过样本门后才允许 paper 实例 veto。

## 9. 测试与证据包

每批完成声明必须附：

1. 原任务清单与实际 diff 双向核对。
2. 改动文件 `py_compile` 结果。
3. 定向测试的绿/红数量。
4. 全量 `tests/test_*.py` 当场回归，红 0。
5. `tools/params_lint.py`、`tools/code_graph.py --check`、`tools/test_isolation_lint.py`、`tools/fix_guard.py` 全绿。
6. 测试前后生产数据库和事件日志哈希不变。
7. Trace 覆盖率、幂等、未来泄漏、故障降级和注入测试证据。
8. 若激活 paper veto：附 shadow 样本量、增量 EV、错拦成本、观察期和回滚演练。

## 10. 风险与缓解

- 随机性：低温度、严格 schema、重复一致性测试、确定性 Policy Kernel。
- 延迟：总预算、超时、缓存、幂等和断路器；专用止损线程继续独立。
- 提示词注入：外部文本作为不可信数据、工具白名单、禁止任意指令执行。
- 记忆污染：只用成熟结果、版本过滤、证据 ID、时间衰减和异常隔离。
- 未来泄漏：as-of Context Builder、purged 时间重放、时间边界测试。
- 过拟合：champion/challenger、完整试验日志、样本门和样本外增量。
- 错拦机会：saved_loss 与 missed_profit 同时报，不用“拦下亏损数”单边美化。
- 模型不可用：回到现役规则并标记 `baseline_pass`，不伪装 approve。

## 11. 停止条件

遇到以下任一情况停止晋升：

- 运行记录或输入快照不能完整复现。
- 存在未来数据泄漏或测试污染生产数据。
- Agent 能接触交易执行、配置写入或风控修改路径。
- 样本量或 reject 类样本不足。
- 错拦盈利机会成本不低于拦亏收益。
- 增量 EV 成本后不为正或只在单一场景有效。
- 模型概率未校准、输出不稳定或故障降级率过高。
- prompt injection 或未注册工具调用可以改变最终动作。

停止晋升不删除数据与实验；保留版本和证据，等待独立新样本继续验证。

## 12. 最终完成定义

只有以下条件同时满足，才能声明 Agent Harness 能力得到提升：

1. 每次运行都有版本、输入、步骤、输出、最终动作和结果的完整审计链。
2. 相同冻结输入可复现，所有失败语义与模型 approve 明确分离。
3. 上下文和记忆无未来泄漏，检索证据可追溯。
4. 工具严格只读、步数和时间有界，Agent 无交易执行权限。
5. 确定性 Policy Kernel 完整保留现有规则与风控边界。
6. 离线样本外评测证明 saved_loss 大于 missed_profit 与模型成本。
7. champion/challenger、paper shadow、观察期和一键回滚闭环通过。
8. 全量回归与仓库机器守卫红 0，生产数据在测试前后不变。

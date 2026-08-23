# Agent 主动候选提案 Shadow 实施稿

> 状态：权威实施稿（FINAL）
> 日期：2026-08-23
> 范围：OKX 模拟盘 15 分钟日内候选发现；不授予交易执行权限
> 目标：让 AI 主动提出方向候选，同时用确定性 2:1、成本、概率校准和既有风控约束其权限。

## 1. 最终链路

```text
每日候选池 Top N
  → 已收线 15m/1h/4h 因果快照
  → AI 批量提出 0～2 个方向候选
  → Schema、标的、置信度、证据 ID 校验
  → 系统按 ATR 计算固定 1R 止损和 2R 止盈
  → 候选级费用与资金费成本
  → 已验证 active 概率模型的 EV 95% 下界检查
  → C_agent_proposal 影子留样
  → 完整 4h/1m TP/SL 首触、MFE/MAE 反事实结算
  → 因子、概率、极值和校准研究
```

当前链路在任何结果下都不会调用 `open_position`。即使未来概率门通过，v1 仍写
`shadow_prediction_passed`，`execution_authority=false`。

## 2. AI 能做什么

- 从本轮给定候选池选择标的和 long/short 方向。
- 输出 0～2 个提案；没有清晰机会时返回空列表。
- 给出 0～1 置信度、可证伪理由，以及输入里真实存在的 evidence ID。

AI 不能决定入场价格、止损、止盈、仓位、杠杆和预算，不能新增候选池外标的、
伪造证据、修改策略参数或调用交易所执行接口。

## 3. 确定性验证

### 3.1 输入与频率

- 仅在真实 OKX 模拟盘装配 provider；FakeAdapter 和 live 模式不装配。
- 每根已收线 15m K 最多一个幂等批次，重启或重试不会重复调用模型。
- 按当日扫描评分取前 `AGENT_PROPOSAL_MAX_SYMBOLS` 个标的。
- v1 输入只包含已收线 15m/1h/4h OHLCV 推导的 ATR、EMA、动量和量比；v3 自部署后额外冻结
  自然时点的盘口、价差、订单流、OI、basis 与资金费，历史 OHLCV 不伪造这些字段。

### 3.2 契约门

- v1 顶层只能有 `proposals`；v3 顶层固定为 `proposals + abstain_reason`，空列表必须给标准空仓原因，
  非空列表的 `abstain_reason` 必须为 null。
- 每项只能有 base、direction、confidence、thesis、evidence_ids。
- 标的必须在输入快照中；证据 ID 必须逐字属于对应标的快照。
- v3 每条非空提案还必须引用对应标的的 microstructure evidence ID；只复述 K 线而不锚定微观证据
  会在几何与留样前确定性拒绝。
- 数量、字段、置信度和理由长度均由 `config.py` 集中限制。
- 解析、Schema、provider 或超时失败只落审计，不生成影子候选。

### 3.3 2:1 与概率门

- `entry` 使用最后一根已收线 15m 的收盘参考价。
- `risk = STOP_ATR_MULT × ATR`，`reward = TP_ATR_MULT × ATR`。
- `reward/risk` 必须严格等于 `ENTRY_REQUIRED_REWARD_RISK=2.0`。
- 再按双边 taker、滑点和方向不利资金费计算 `cost_r` 与扣费保本胜率。
- 没有同策略、同方向的已验证 active 概率模型时，明确记录
  `no_validated_active_model`，不能把理论 2:1 当作正期望证明。
- 只有成本后 `EV_R` 单侧 95% 下界大于 0，才可标记
  `shadow_prediction_passed`；仍无执行权限。

## 4. 数据与隔离

SQLite schema v32 新增：

- `agent_proposal_runs`：周期幂等键、模型/Prompt/Schema 版本、输入输出 hash、运行状态、延迟和数量。
- `agent_proposals`：方向、置信度、证据、确定性价格几何、成本、保本胜率、验证状态和 signal_id。

v3 不修改 live peer schema；每个 paper 提案批次在现有 `kv` 中以
`agent_proposal_audit:<run_id>` 原子冻结完整 input snapshot、Prompt/Schema/实现版本、输入 hash、
快照数、微观字段覆盖率，以及 completed/schema_error/标准空仓原因。`input_hash` 必须能由冻结 JSON
逐字复算；旧无审计 run 保留但不计入当前协议成熟度。

几何有效的提案以 `strategy_id=C_agent_proposal` 写入 `signal_samples`，并固定：

- `rule_decision=shadow`
- `ai_verdict=proposal`
- `final_decision=rejected`
- `execution_authority=0`

它复用现有 `signal_outcomes` 完整路径结算，但与 A_pullback、B_breakout 的候选、
因子、模型和 readiness 统计完全隔离。每日研究调度会分别研究 A/B/C，C 的证据
不能点亮 A 的统计门。

## 5. 观测入口

- `GET /agent/proposals`：最近批次、提案、2:1 几何、概率门、成熟路径结果。
- 同一接口同时给出当前 implementation version 的 run/completed/abstain/proposal/mature 数、非空提案
  覆盖率、微观结构覆盖率和逐 run 冻结 input audit；旧版本只保留审计，不混入当前统计。
- `decision/agent_proposals.py`：提案契约、快照、批次与审计。
- `engines/signal_sampling.py`：把已验证提案接入共同标签链。
- `tests/test_agent_proposals.py`：2:1、伪证据、低置信度、幂等、live 禁用和零订单证据。

## 6. 后续晋升门

本批次只开启数据积累。任何“AI 主动信号可以影响交易”的后续提案，至少需要：

1. C 策略独立自然机会达到概率模型训练门，不能与 A/B 混样本。
2. long/short 各自满足 TP/SL 类别下限和 purged walk-forward。
3. Brier skill、4/5 折一致、成本后净 EV、DSR/PBO 和稳定性门全部通过。
4. shadow 放行样本达到模型生命周期要求，费用后实际 EV 仍为正。
5. 与 A 基线做同输入、同时间的增量 EV 和机会成本比较。
6. 单独设计新的人工批准与回滚批次；本实施稿不授予该权限。

长期回测或 shadow 成本后 EV 为负时，无论短期多少笔盈利，都不得扩大预算或把
提案链切到执行模式。

## 7. 本批次验收口径

- 主动提案专项测试全部通过。
- 服务只读接口回归通过。
- 参数集中化、代码图、测试隔离与 AI 文档链接检查通过。
- 全量测试脚本失败为 0。
- 未重启、未修改、未操作 live 实例。

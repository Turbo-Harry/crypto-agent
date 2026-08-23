# 文档中心（docs/）

> 文档管理约定：**功能分类**（目录）＋**时间线**（文件名前缀 `YYYY-MM-DD_`）。
> 新增文档先选分类目录，文件名带日期前缀；改动后更新本页"时间线"表。

## 一、按功能分类

| 目录 | 内容 | 文档 |
|---|---|---|
| [architecture/](architecture/) | 架构设计 | [exchange_layers.md](architecture/exchange_layers.md)（交易所访问四层）、[mtf_resonance_design.md](architecture/mtf_resonance_design.md)（多周期共振设计）、[code_graph.md](architecture/code_graph.md)（代码知识图谱：三层建模+影响面查询）、[ai_friendly_repo.md](architecture/ai_friendly_repo.md)（AI 接手路径/事实优先级/15m 研究链/Agent Harness/机器守卫） |
| [plans/](plans/) | 优化/实施计划与简报 | Agent B R1/R2 方案及终审稿、D 批次实施简报、自进化系统设计方案、[交易 Agent Harness 权威实施稿](plans/2026-08-23_agent_harness_improvement_FINAL.md)、[Agent 主动提案 Shadow 权威实施稿](plans/2026-08-23_agent_active_proposal_shadow_FINAL.md)、[开仓准确率/因子/极值预测权威实施稿](plans/2026-08-23_entry_accuracy_factor_forecast_FINAL.md) |
| [reports/](reports/) | 研究报告与收敛报告 | [15m/4h OKX SWAP 历史重放裁决](reports/2026-08-23_15m_research_replay_report.md)、research_report_round2/3、optimization_report、convergence_report、final_report、evolution_loop_report、optimization_notes（实施日志）、backtest_report、**pitfalls（踩坑档案，写代码前必读）** |
| [ops/](ops/) | 运维与验证手册 | watchdog_launchd（进程守护）、tp_sandbox_verify（止盈沙盘验证清单）、subaccount_test_plan（子账户测试计划）、data_collection_schedule（数据采集调度） |
| [prompts/](prompts/) | AI 提示词 | evolution_loop_prompt（进化循环提示词）、factor_mining_goal_prompt（因子挖掘完善，验证门标准） |
| [AGENT_NOTES.md](AGENT_NOTES.md) | Agent 协作 | 多 Agent 文件占用协议与当前 claim（机器维护） |

仓库四个入口：根目录 `README.md`（人类总览）、`AGENTS.md`（AI 安全与协作规则）、
`llms.txt`（机器可读地图），以及 `docs/README.md`（文档索引）。

## 二、按时间线（全部 35 篇，不含本索引）

| 时间 | 文档 | 一句话 |
|---|---|---|
| 08-16 03:23 | [ops/data_collection_schedule.md](ops/data_collection_schedule.md) | 数据采集调度 |
| 08-16 04:11 | [reports/backtest_report.md](reports/backtest_report.md) | 回测报告 |
| 08-16 04:57 | [architecture/mtf_resonance_design.md](architecture/mtf_resonance_design.md) | 1h+4h 多周期共振设计 |
| 08-16 07:11 | [reports/optimization_report.md](reports/optimization_report.md) | 首轮优化报告 |
| 08-16 07:45 | [reports/final_report.md](reports/final_report.md) | 系统交付终报 |
| 08-16 07:50 | [prompts/evolution_loop_prompt.md](prompts/evolution_loop_prompt.md) | 进化循环提示词 |
| 08-16 07:56 | [plans/brief_template.md](plans/brief_template.md) | 实施简报模板 |
| 08-16 07:56 | [reports/research_report_round2.md](reports/research_report_round2.md) | 第 2 轮研究（NEW-1~8） |
| 08-16 08:02 | [plans/optimization_plan_agentB_R1.md](plans/optimization_plan_agentB_R1.md) | Agent B R1 方案 |
| 08-16 08:04 | [reports/research_report_round3.md](reports/research_report_round3.md) | 第 3 轮研究（R3-1~5） |
| 08-16 08:10 | [plans/launch_brief_draft.md](plans/launch_brief_draft.md) | 上线简报草稿 |
| 08-16 08:11 | [plans/optimization_plan_agentB_R1_FINAL.md](plans/optimization_plan_agentB_R1_FINAL.md) | R1 终审稿（权威实施稿） |
| 08-16 08:14 | [plans/batch2_brief.md](plans/batch2_brief.md) | R1 批次实施简报 |
| 08-16 08:21 | [plans/optimization_plan_agentB_R2.md](plans/optimization_plan_agentB_R2.md) | Agent B R2 方案 |
| 08-16 08:26 | [plans/r2_brief.md](plans/r2_brief.md) | R2 实施简报 |
| 08-16 08:26 | [plans/optimization_plan_agentB_R2_FINAL.md](plans/optimization_plan_agentB_R2_FINAL.md) | R2 终审稿（权威实施稿） |
| 08-16 08:32 | [ops/subaccount_test_plan.md](ops/subaccount_test_plan.md) | 子账户沙盘测试计划 |
| 08-16 08:39 | [ops/tp_sandbox_verify.md](ops/tp_sandbox_verify.md) | TP 沙盘验证清单 |
| 08-16 08:40 | [ops/watchdog_launchd.md](ops/watchdog_launchd.md) | 进程守护与 launchd 配置 |
| 08-16 08:42 | [reports/evolution_loop_report.md](reports/evolution_loop_report.md) | 进化循环报告（18 实施 + 3 驳回） |
| 08-16 14:28 | [reports/convergence_report.md](reports/convergence_report.md) | 收敛报告 |
| 08-16 14:39 | [reports/optimization_notes.md](reports/optimization_notes.md) | 实施日志（R1/R2/OP/CR/RES 全记录） |
| 08-16 16:03 | [architecture/exchange_layers.md](architecture/exchange_layers.md) | 交易所访问分层架构 |
| 08-16 16:48 | [reports/pitfalls.md](reports/pitfalls.md) | 踩坑档案（API/数量/工程类，写代码前必读） |
| 08-16 17:00 | [architecture/code_graph.md](architecture/code_graph.md) | 代码知识图谱（模块/符号/数据流三层 + --check/--query） |
| 08-16 17:05 | [architecture/ai_friendly_repo.md](architecture/ai_friendly_repo.md) | AI 协作契约：接手路径、事实优先级、15m 研究链、Agent Harness 与机器守卫 |
| 08-16 19:10 | [plans/2026-08-16_self_evolution_design.md](plans/2026-08-16_self_evolution_design.md) | 自进化系统设计方案 v0.2（现状/DEF 缺陷/业界标准验收/Phase 路线图/质疑轮） |
| 08-16 19:40 | [reports/2026-08-16_strategy_research.md](reports/2026-08-16_strategy_research.md) | 业界策略调研：订单流/聪明钱/分场景选策略（Agent A 格式，附来源） |
| 08-16 21:20 | [prompts/2026-08-16_factor_mining_goal_prompt.md](prompts/2026-08-16_factor_mining_goal_prompt.md) | 因子挖掘完善目标 prompt（验证门/试验日志/影子政策） |
| 08-16 22:10 | [architecture/trade_features_schema.md](architecture/trade_features_schema.md) | Phase 1 特征采集 schema（MFE/MAE/R 倍数/regime/订单流/影子分） |
| 08-21 | [AGENT_NOTES.md](AGENT_NOTES.md) | 多 Agent 单写者占用协议（活文档） |
| 08-23 | [plans/2026-08-23_agent_harness_improvement_FINAL.md](plans/2026-08-23_agent_harness_improvement_FINAL.md) | 交易 Agent Harness：上下文、记忆、只读工具、策略核、Trace、Eval 与回滚（权威实施稿） |
| 08-23 | [plans/2026-08-23_agent_active_proposal_shadow_FINAL.md](plans/2026-08-23_agent_active_proposal_shadow_FINAL.md) | AI 主动方向候选、确定性 2:1、独立反事实标签与零执行权限（权威实施稿） |
| 08-23 | [plans/2026-08-23_entry_accuracy_factor_forecast_FINAL.md](plans/2026-08-23_entry_accuracy_factor_forecast_FINAL.md) | 15m 主周期/4h horizon 的开仓准确率、因子挖掘、极值区间与 Agent 增量验证（权威实施稿） |
| 08-23 | [reports/2026-08-23_15m_research_replay_report.md](reports/2026-08-23_15m_research_replay_report.md) | 10 个 OKX SWAP 的 15m/4h 历史重放、因子/概率/极值样本外裁决与停止结论 |

## 三、命名约定

- 新增文档：`YYYY-MM-DD_功能名.md`，放进对应功能目录。
- **例外（活文档，追加式更新，不加日期前缀）**：`reports/pitfalls.md`（踩坑档案）、`reports/optimization_notes.md`（实施日志）、`AGENT_NOTES.md`（机器维护的协作占用）。
- 同一方案多轮迭代：保留草稿（如 `optimization_plan_agentB_R1.md`）＋终审稿（`..._FINAL.md`），终审稿在文件名与内容里标注"权威实施稿"。
- 被代码引用的文档路径改动后，必须同步更新引用（`tools/watchdog.py`、`engines/directional_trader.py` 等）。

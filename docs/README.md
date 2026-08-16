# 文档中心（docs/）

> 文档管理约定：**功能分类**（目录）＋**时间线**（文件名前缀 `YYYY-MM-DD_`）。
> 新增文档先选分类目录，文件名带日期前缀；改动后更新本页"时间线"表。

## 一、按功能分类

| 目录 | 内容 | 文档 |
|---|---|---|
| [architecture/](architecture/) | 架构设计 | [exchange_layers.md](architecture/exchange_layers.md)（交易所访问四层）、[mtf_resonance_design.md](architecture/mtf_resonance_design.md)（多周期共振设计）、[code_graph.md](architecture/code_graph.md)（代码知识图谱：三层建模+影响面查询）、[ai_friendly_repo.md](architecture/ai_friendly_repo.md)（AI 友好仓库调研与落地） |
| [plans/](plans/) | 优化/实施计划与简报 | Agent B R1/R2 方案及终审稿、D 批次实施简报（brief_template / launch_brief_draft / batch2_brief / r2_brief） |
| [reports/](reports/) | 研究报告与收敛报告 | research_report_round2/3、optimization_report、convergence_report、final_report、evolution_loop_report、optimization_notes（实施日志）、backtest_report、**pitfalls（踩坑档案，写代码前必读）** |
| [ops/](ops/) | 运维与验证手册 | watchdog_launchd（进程守护）、tp_sandbox_verify（止盈沙盘验证清单）、subaccount_test_plan（子账户测试计划）、data_collection_schedule（数据采集调度） |
| [prompts/](prompts/) | AI 提示词 | evolution_loop_prompt（进化循环提示词） |

根目录保留三个入口文档（不在 docs/ 内）：`README.md`（项目总览）、`AGENTS.md`（AI 协作模型）、`docs/README.md`（本索引）。

## 二、按时间线（全部 25 篇）

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
| 08-16 08:42 | [reports/evolution_loop_report.md](reports/evolution_loop_report.md) | 进化循环报告（18 实施 + 3 驳回） |
| 08-16 14:28 | [reports/convergence_report.md](reports/convergence_report.md) | 收敛报告 |
| 08-16 14:39 | [reports/optimization_notes.md](reports/optimization_notes.md) | 实施日志（R1/R2/OP/CR/RES 全记录） |
| 08-16 16:03 | [architecture/exchange_layers.md](architecture/exchange_layers.md) | 交易所访问分层架构 |
| 08-16 16:48 | [reports/pitfalls.md](reports/pitfalls.md) | 踩坑档案（API/数量/工程类，写代码前必读） |
| 08-16 17:00 | [architecture/code_graph.md](architecture/code_graph.md) | 代码知识图谱（模块/符号/数据流三层 + --check/--query） |
| 08-16 17:05 | [architecture/ai_friendly_repo.md](architecture/ai_friendly_repo.md) | AI 友好仓库调研与落地对照 |

## 三、命名约定

- 新增文档：`YYYY-MM-DD_功能名.md`，放进对应功能目录。
- **例外（活文档，追加式更新，不加日期前缀）**：`reports/pitfalls.md`（踩坑档案）、`reports/optimization_notes.md`（实施日志）。
- 同一方案多轮迭代：保留草稿（如 `optimization_plan_agentB_R1.md`）＋终审稿（`..._FINAL.md`），终审稿在文件名与内容里标注"权威实施稿"。
- 被代码引用的文档路径改动后，必须同步更新引用（`tools/watchdog.py`、`engines/directional_trader.py` 等）。

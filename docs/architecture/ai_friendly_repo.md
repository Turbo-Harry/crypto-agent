# AI 友好仓库调研与落地对照

> 2026-08-16 调研。来源：llms.txt 标准、AGENTS.md/CLAUDE.md 社区实践、
> [AI 仓库结构示例（IgniteUI/ai-repo-structure）](https://github.com/IgniteUI/ai-repo-structure)、
> [supabase/mcp AGENTS 指南 PR](https://github.com/supabase/mcp/pull/199)、
> [ai-project-scaffold 模板](https://github.com/hellOoSaksit/ai-project-scaffold)。

## 一、AI 友好仓库的核心实践（调研结论）

| # | 实践 | 说明 |
|---|---|---|
| 1 | **AGENTS.md** | 仓库级 AI 协作说明：架构、入口、约束、红线。AI 读它即知"怎么干/不干什么"。 |
| 2 | **llms.txt** | 机器可读入口索引（Markdown 链接清单），AI 抓取仓库时先看它。与 AGENTS.md 互补：llms.txt 是"地图"，AGENTS.md 是"规则"。 |
| 3 | **SSOT 单一事实源** | 关键信息只放一处（配置、阈值、路径约定），其余文档引用而不复制——防止文档漂移。 |
| 4 | **README 说"是什么/怎么跑"** | 面向人，简短；细节下沉到 docs/。 |
| 5 | **依赖图/架构图可机检** | 分层规则写进代码检查脚本，而非只写进文档（本仓：tools/dependency_graph.py --check）。 |
| 6 | **踩坑档案** | pitfalls 记录现象/根因/修复/预防，AI 写代码前先查，避免重踩。 |
| 7 | **提交规范** | Conventional Commits（feat:/fix:/docs:…），git log 即时间线。 |
| 8 | **文档 lint/索引同步** | 新增文档必须更新索引与交叉引用；本仓以 docs/README.md 为索引。 |

## 二、本仓落地情况

| 实践 | 状态 | 位置 |
|---|---|---|
| AGENTS.md | ✅ | 根目录（11 节：架构/入口/安全不变量/文档路径/读经验/记踩坑/红线） |
| llms.txt | ✅ | 根目录（入口文档/架构/经验/代码/运维/工具 六组链接） |
| SSOT | ✅ | config.py 参数集中；阈值/预算/开关都在 AGENTS.md 注明"不可擅改" |
| README | ✅ | 简短总览 + 快速开始，细节下沉 docs/ |
| 依赖图机检 | ✅ | `tools/dependency_graph.py --check`（AST 分析，✅ 无违规）+ `docs/architecture/dependency_graph.md`（mermaid+矩阵） |
| 踩坑档案 | ✅ | `docs/reports/pitfalls.md`（15 条，模板=现象/根因/修复/预防） |
| 提交规范 | ✅ | 本仓 git log 全部 Conventional Commits |
| 文档索引 | ✅ | `docs/README.md`（功能分类表 + 时间线表） |

## 三、待办（诚实声明）

- [ ] CI：GitHub Actions 跑 py_compile + 两套单测 + dependency_graph --check（当前手跑）
- [ ] docs-lint：自动校验文档交叉引用不失效（当前手查）
- [ ] llms-full.txt：全量文档拼接版（当前只有索引版 llms.txt，仓库小时没必要）

# AGENTS.md — docs

> 作用域：`docs/`。继承[根协作规则](../AGENTS.md)；这里保存架构、计划、报告、运维和提示词。

## 职责

- 让当前行为可发现、历史结论有日期语境、计划与实装可区分、运维步骤可验证。

## 局部规则

- 新文档按 architecture/plans/reports/ops/prompts 分类并使用日期前缀；活文档例外见根规则。
- 新增/移动文档同步更新[文档索引](README.md)；关键 AI 入口同步根 `llms.txt`。
- 当前事实优先引用代码、config 名称、schema 和可重复测试，不复制易漂移统计。
- 计划不得写成已实现，离线结果不得写成活体结果，历史报告不得覆盖当前授权。
- `pitfalls.md` 按“现象 → 根因 → 修复 → 预防”追加；不要改写旧事故语境。
- Markdown 链接必须仓库内可达；禁止用跳过列表隐藏孤儿文档或断链。

## 最小验证

- `python3 tools/ai_repo_check.py`
- `python3 tests/test_ai_repo_check.py`
- `git diff --check`

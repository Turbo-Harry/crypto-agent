# Agent 协作备忘录（2026-08-21 建立）

> 本仓库有**多条 agent 线并行提交**（同一 git 身份），靠此文件协调，避免同时改同一文件。

## 协作协议（三条铁律）

1. **动手前**：`python3 tools/agent_notes.py status` 看活跃占用 + `git log --oneline -3 -- <file>` 看文件最近是否被别人刚改过。
2. **动手时**：`python3 tools/agent_notes.py claim <会话标签> <文件1> [文件2...]` —— 声明你要改的文件。
3. **改完**：`python3 tools/agent_notes.py release <会话标签>` —— 释放占用并附 commit hash。

占用 60 分钟自动过期（防忘记释放导致死锁）；`git commit` 前钩子会检查冲突并**警告**（不阻止，但请认真看）。

## 活跃占用（机器维护，勿手改）

<!-- AGENT_CLAIMS_BEGIN -->
- strategy_b_replay | 1787475706 | config.py,decision/signal_outcomes.py,docs/architecture/ai_friendly_repo.md,docs/plans/2026-08-23_entry_accuracy_factor_forecast_FINAL.md,docs/reports/2026-08-23_15m_research_replay_report.md,docs/reports/optimization_notes.md,docs/reports/pitfalls.md,engines/signal_sampling.py,engines/strategy_b.py,tests/test_replay_15m_research.py,tests/test_strategy_b.py,tools/evaluate_15m_research.py,tools/replay_15m_research.py
<!-- AGENT_CLAIMS_END -->

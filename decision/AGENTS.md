# AGENTS.md — decision

> 作用域：`decision/`。继承[根协作规则](../AGENTS.md)；模型、Agent 和进化逻辑没有独立执行权限。

## 职责

- 确定性决策、经验/复盘、阈值与权重进化、候选标签、概率/极值消费和 Agent Harness。
- 通过 `decision/api.py` 向服务层提供稳定公开门面。

## 局部规则

- 禁止直接 import `engines`/`service` 或持有交易所下单工具；需要上层能力时定义结果或 callback。
- 模型和 Agent 只能否决、shadow 或提供受约束建议，不得恢复基线已拒候选或绕过风险门。
- 缺失、损坏、scope/版本/成本不符必须显式失败关闭，不得伪装成批准。
- 训练、shadow、自然 paper、策略 A/B/C 的证据必须按身份隔离，禁止混计成熟度。
- 所有演进要有 proposal、验证门、审计、回滚和人工授权边界；状态名不代表正收益。
- 参数只引用 `config.py`；跨层持久化经公开 repository/query API。

## 最小验证

- 运行对应 `tests/test_*` 决策专项，至少覆盖输入契约、失败语义、scope、幂等和回滚。
- Agent 改动运行 `tests/test_agent_*.py` 与 `tests/test_agent_proposals.py`。
- `python3 tools/params_lint.py && python3 tools/code_graph.py --check`

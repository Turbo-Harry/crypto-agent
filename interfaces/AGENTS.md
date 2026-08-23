# AGENTS.md — interfaces

> 作用域：`interfaces/`。继承[根协作规则](../AGENTS.md)；这里是无副作用、最稳定的中立契约层。

## 职责

- 定义服务、引擎、决策和存储共同依赖的 Protocol、数据契约与轻量领域类型。

## 局部规则

- 禁止 import `service`、`engines`、`decision`、`execution` 或 `storage` 的实现模块。
- 导入本模块不得访问文件、数据库、网络、环境凭证或启动线程。
- 契约变化先写/改边界测试，再同步所有实现与调用方；优先向后兼容。
- 不在 Protocol 中泄漏交易所原始响应、SQLite 行结构或 Web 框架对象。
- 类型必须明确；可变容器和可选字段要写清所有权与缺失语义。

## 最小验证

- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_interface_boundaries.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_agent_contracts.py`
- `python3 tools/code_graph.py --check`

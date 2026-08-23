# AGENTS.md — storage

> 作用域：`storage/`。继承[根协作规则](../AGENTS.md)；这里是 SQLite schema 与持久化实现的唯一归属层。

## 职责

- schema/迁移/事务原语、只读 query API、领域 repository、Agent Trace/记忆与运行异常持久化。

## 局部规则

- 禁止反向 import `service`、`engines`、`decision` 或 `execution`；共享契约只能来自 `interfaces`。
- 调用方不得掌握 SQL；新增写路径放入所属 repository，新增读路径放入 `query_api.py` 或公开查询接口。
- 迁移必须幂等、兼容旧库、短事务；保留 WAL、busy timeout 和线程安全短连接语义。
- 策略、模型、版本、自然实验单位和 provenance 必须是一等过滤条件，禁止跨 scope 混计。
- 测试只使用临时数据库；禁止对活体 DB、WAL/SHM 或运行中状态文件执行迁移实验。

## 最小验证

- 运行涉及表的专项测试与 `tests/test_interface_boundaries.py`。
- `python3 tools/test_isolation_lint.py`
- `python3 tools/code_graph.py --check`

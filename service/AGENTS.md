# AGENTS.md — service

> 作用域：`service/`。继承[根协作规则](../AGENTS.md)；冲突时根规则优先，本文件只收紧服务层边界。

## 职责

- FastAPI/uvicorn 外壳、Pydantic 对外 schema、worker 生命周期与运行时组合。
- HTTP 通过 `TradingRuntimePort`、`decision.api` 和 `storage.query_api` 访问下层能力。

## 局部规则

- 禁止新增下单、撤单、加仓、改杠杆或绕过风控的 HTTP 接口。
- 控制端点必须继续使用回环 Host 与可选 API token 防护；止损监控不受 pause 影响。
- 不得访问 trader 的私有字段、内部协作者或直接 import `storage.db`；不得让核心运行链依赖 `tools`。
- 应用必须保持单 worker；导入模块、生成 OpenAPI 和测试建 app 时不得连接交易所。
- API 字段先改 `service/models.py` 契约，再同步实现和测试；当前完整接口以 `/docs` 为准。

## 最小验证

- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_service_api.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_interface_boundaries.py`
- `python3 tools/code_graph.py --check`

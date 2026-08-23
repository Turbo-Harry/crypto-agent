# AGENTS.md — engines

> 作用域：`engines/`。继承[根协作规则](../AGENTS.md)；本目录是交易行为编排层，按 D2 高风险改动处理。

## 职责

- 方向性引擎组合根、信号扫描/采样、开平仓、风控监控、复盘和每日候选扫描。
- 把决策、交易所、执行、存储与实时行情能力组装成可审计运行链。

## 局部规则

- 引擎不得 import FastAPI 等 Web 框架；对服务只暴露稳定 runtime API。
- 交易所访问只走 `ExchangeAdapter`，禁止裸打 OKX URL 或消费原始响应字段。
- 不得绕过单笔风险、名义/总敞口、最小量拒绝和交易所侧止损；失败必须 fail-closed。
- Mixin 对宿主的隐式 `self.*` 依赖要克制；新增跨功能依赖优先显式注入公开接口或 callback。
- 已收线 15m K、同 K 幂等、4h horizon 和拒绝候选反事实留样语义不得静默改变。
- 策略参数只从 `config.py` 引用；活体改动必须完成 paper 沙盘与重启后核验。

## 最小验证

- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_decision_loop.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_signal_sampling.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_exchange_layers.py`
- `python3 tools/params_lint.py && python3 tools/code_graph.py --check`

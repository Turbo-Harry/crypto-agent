# AGENTS.md — factors

> 作用域：`factors/`。继承[根协作规则](../AGENTS.md)；这是 research-only 层，没有交易执行权限。

## 职责

- 特征注册、因子发现/验证、防过拟合、概率模型和极值模型训练。

## 局部规则

- 所有特征写清公式、来源、as-of、缺失策略、理论依据和版本；禁止未来数据泄漏。
- 使用 purged walk-forward、embargo、成本后 EV、DSR/PBO 与稳定性门，禁止同集穷举后择优。
- 数据、候选宇宙、策略、成本、特征和随机种子版本必须进入试验/制品身份。
- `validated=0` 时不得拼装“最佳组合”或扩大模型权限；离线结果只给证伪权。
- 本层不得下单、改风险预算、修改活体模型状态或把历史重放冒充自然 paper 结果。

## 最小验证

- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_factor_gate.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_intraday_factor_gate.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_overfit_guard.py`
- 训练器改动同时跑概率/极值与 replay 专项。

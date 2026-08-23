# AGENTS.md — execution

> 作用域：`execution/`。继承[根协作规则](../AGENTS.md)；数量、台账和持仓所有权属于安全关键边界。

## 职责

- 名义金额到合约数量换算、事件流水、PID/运行目录、交易台账与持仓所有权账本。

## 局部规则

- 数量只能按 `lotSz` 向下取整；最小量不足时拒绝，禁止放大名义凑单。
- 持仓总敞口与 claim/release 必须原子、一致、可恢复；文件写入使用临时文件替换和必要的 flock。
- 台账字段保留单位、venue、策略身份、费用与实际 USDT 语义，禁止把百分比相加冒充盈亏。
- 运行目录和事件文件必须尊重实例环境变量；测试不得回落到默认活体路径。
- 不在本层选择交易所实现、Web 框架或具体数据库 schema。

## 最小验证

- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_exchange_layers.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_fee_accounting.py`
- `python3 tools/test_isolation_lint.py && python3 tools/code_graph.py --check`

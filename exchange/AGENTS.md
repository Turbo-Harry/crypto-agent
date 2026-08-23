# AGENTS.md — exchange

> 作用域：`exchange/`。继承[根协作规则](../AGENTS.md)；AI 只可验证 OKX 模拟盘，默认优先 FakeAdapter 离线测试。

## 职责

- 原生 transport、OKX/CCXT 适配、交易所抽象接口、领域模型和 FakeAdapter。

## 局部规则

- `transport` 负责签名、限速和错误归一；adapter 负责单位、场所、精度和响应翻译。
- 上层只接触 `ExchangeAdapter`、领域模型和 `OrderResult`；原始字段不得向业务层扩散。
- 网络/签名失败抛 `ExchangeError`；业务拒绝返回 `OrderResult(ok=False, message)`。
- 条件单使用 `slTriggerPx`/`tpTriggerPx`；查询 pending 必带 `ordType`，不得弱化交易所侧止损。
- `ctVal`、`lotSz`、现货/合约、USDT 成交额和 reduce-only 语义必须有离线回归。
- 禁止 import 上层业务包；不得读取 live 凭证或把 sandbox 能力解释为 live 授权。

## 最小验证

- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_exchange_layers.py`
- `CRYPTO_AGENT_MODE=paper PYTHONPATH=lib:. python3 tests/test_production_guard.py`
- `python3 tools/code_graph.py --check`

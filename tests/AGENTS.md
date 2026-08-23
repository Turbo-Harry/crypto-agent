# AGENTS.md — tests

> 作用域：`tests/`。继承[根协作规则](../AGENTS.md)；测试必须离线、隔离、可重复。

## 职责

- 每个 `test_*.py` 可独立执行，CI 自动发现全部脚本，不维护固定白名单。

## 局部规则

- 默认使用 Fake/Stub/TestClient；禁止连接 live，网络不是单元测试成功的前提。
- 每个脚本使用独立 `CRYPTO_AGENT_DB`、`CRYPTO_AGENT_EVENTS_FILE` 和
  `CRYPTO_AGENT_RUNTIME_DIR`，不得回落到活体文件。
- 测试必须断言行为和安全语义，不只断言“没有异常”；失败测试不得为迁就实现而弱化。
- 时间、随机、版本、市场和策略身份要冻结；避免依赖执行顺序或前一脚本残留。
- 修改用户已拍板的配置行为时同步更新新旧断言，防止全量回归长期假红。

## 最小验证

- 先单跑改动脚本，再按 `.github/workflows/ci.yml` 的隔离方式自动发现全量脚本。
- `python3 tools/test_isolation_lint.py`
- `python3 tools/fix_guard.py`

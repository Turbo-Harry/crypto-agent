# AGENTS.md — tools

> 作用域：`tools/`。继承[根协作规则](../AGENTS.md)；CLI 是外围入口，核心运行链不得反向依赖本目录。

## 职责

- 运维、自检、数据回填、研究重放、健康检查、协作占用和代码图等命令行入口。

## 局部规则

- 默认优先只读和离线；任何可能构造应用/交易所的命令显式使用 `CRYPTO_AGENT_MODE=paper`。
- 写入型工具必须明确目标、支持隔离路径、幂等/原子操作和失败恢复；不得默认指向活体状态。
- 核心包禁止 import `tools.*`；可复用业务逻辑下沉到所属核心模块，CLI 只做参数解析和输出。
- 不输出凭证、签名、token 或完整敏感环境；网络失败不得伪装成功。
- 新工具要有 `--help`、清晰退出码和对应测试/守卫，避免维护固定测试白名单。

## 最小验证

- 运行工具自身专项或合成输入测试。
- `python3 tools/test_isolation_lint.py`
- `python3 tools/code_graph.py --check`
- AI 入口工具改动运行 `python3 tests/test_ai_repo_check.py`。

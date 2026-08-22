# AI 友好仓库协作契约

> 活文档。2026-08-23 完成第二轮体检：从“有入口文档”升级为“入口可发现、事实有优先级、
> 改动可路由、漂移可机检”。本页面向第一次接手仓库的 AI 与新协作者。

## 一、60 秒接手路径

1. 先读根目录 `AGENTS.md`：它定义安全边界、架构规则与禁止事项。
2. 再读 `llms.txt`：按任务选择最少的代码与文档，不要先吞完整仓库。
3. 运行 `python3 tools/agent_notes.py status` 与 `git status --short`：保护其他协作者和用户改动。
4. 写代码前按 AGENTS.md 要求读 `docs/reports/pitfalls.md`、
   `docs/reports/optimization_notes.md` 与相关架构文档。
5. 用 `python3 tools/code_graph.py --query module:<模块>` 或
   `--query calls:<符号>` 查影响面，再决定修改范围。
6. 写入前 claim 文件，完成后 release；协议见 `docs/AGENT_NOTES.md`。

首次阅读到这里时，不应启动服务、连接交易所、重启进程或修改任何状态文件。

## 二、事实优先级

信息冲突时按以下顺序处理：

1. 当前用户指令与 `AGENTS.md` 的安全不变量；
2. `config.py`、Pydantic schema、接口/实现代码和可重复测试；
3. `README.md` 与 `docs/architecture/` 的当前设计说明；
4. `docs/plans/` 的终审稿；
5. `docs/reports/` 中带日期的历史结论与旧实施记录。

历史文档只说明“当时发生过什么”，不能覆盖当前代码。若高优先级来源彼此冲突，尤其是
paper/live、下单权限、风控上限、止损或策略阈值，必须 fail-closed：停止变更并向用户确认。

当前已知的高风险例子：代码中存在 live/paper 双实例能力，而 AGENTS.md 对 AI 只授权 paper。
因此 AI 的启动命令必须显式写 `CRYPTO_AGENT_MODE=paper`；不得把代码能力解释成操作授权。

## 三、任务路由

| 任务 | 先读 | 主要入口 | 必做验证 |
|---|---|---|---|
| 服务/API | AGENTS §2～5、exchange_layers | `service/app.py`、`service/models.py`、`service/worker.py` | service API 测试 + py_compile |
| 信号/开仓 | pitfalls、optimization_notes、参数规则 | `engines/signal_scan.py`、`engines/position_mgmt.py` | params lint + 决策/分层测试 |
| 止损/风控 | AGENTS 安全不变量、相关 pitfalls | `engines/risk_monitor.py`、`risk/`、`execution/position_ownership.py` | 风控测试；涉及活体必须走沙盘证据链 |
| 复盘/进化 | self_evolution_design、trade_features_schema | `engines/review_pipeline.py`、`decision/` | 验证门测试 + 防过拟合检查 |
| 交易所接入 | exchange_layers、API 类 pitfalls | `exchange/base.py`、适配器、transport | FakeAdapter 离线链路 + 分层检查 |
| 数据库/schema | SQLite 类 pitfalls | `storage/db.py` | 临时库迁移/事务测试，禁止碰活体库 |
| 文档/AI 入口 | 本页、docs/README | `AGENTS.md`、`llms.txt`、`docs/README.md` | `tools/ai_repo_check.py` |
| 运维/状态查询 | AGENTS §10、docs/ops | `tools/health_check.py`、只读 HTTP | 只读证据；未经授权不重启/不写状态 |

## 四、AI 可执行证据清单

按改动范围选择，不能用“应该没问题”代替实测：

```bash
# AI 入口、文档链接、索引完整性
python3 tools/ai_repo_check.py
python3 tests/test_ai_repo_check.py

# 架构、参数与守卫
python3 tools/code_graph.py --check
python3 tools/params_lint.py
python3 tools/fix_guard.py

# 主要离线入口
PYTHONPATH=lib python3 tests/test_exchange_layers.py
PYTHONPATH=lib python3 tests/test_service_api.py

# 仅模拟盘启动；不要省略模式
CRYPTO_AGENT_MODE=paper PYTHONPATH=lib python3 -m service.main --port 8091
```

涉及交易行为、活体进程或状态库时，还要遵守 AGENTS.md 的沙盘实测、重启后心跳与持仓
衔接证据要求。纯文档/守卫改动不应触碰交易所、数据库、心跳、日志或通知通道。

## 五、机器守卫

`tools/ai_repo_check.py` 使用 Python 标准库，检查：

- 根入口 `README.md`、`AGENTS.md`、`llms.txt`、`docs/README.md` 存在；
- 根目录没有散装 Markdown；
- 入口与 docs 中的本地 Markdown 链接存在且不越出仓库；
- `llms.txt` 覆盖安全规则、协作协议、架构入口与历史经验；
- `docs/README.md` 索引每一篇 docs 文档；
- `AGENTS.md` 保留 paper 显式启动、协作占用、AI 自检、分层检查和踩坑阅读护栏。

CI 会运行同一检查及其变异测试。它能抓“链接/索引/关键操作护栏漂移”，不能判断自然语言
中的所有语义冲突；事实冲突仍按第二节人工 fail-closed。

## 六、维护规则

- 新增关键入口：同时更新 `llms.txt`；新增/移动 docs：同时更新 `docs/README.md`。
- 文档引用当前行为时，优先引用 `config.py` 名称，不复制易漂移的数字。
- 报告、计划和事故档案要保留日期语境；不要把历史结果伪装成当前状态。
- 改 import 后运行 code graph；改策略参数只动 config 参数统一维护区。
- AI 自检发现漂移时，先修事实源和引用，再更新索引；不要用跳过列表隐藏失效链接。

## 七、2026-08-23 体检结论

已补齐：AI 接手路径、事实优先级、任务路由、文件占用入口、纯标准库文档守卫、变异测试与
CI 接线；同时移除 `llms.txt` 对已归档套利模块的失效引用，并把 paper 启动模式写成显式命令。

仍需人工决策：代码已实现 live/paper 双模式，但 AGENTS.md 对 AI 的授权仍只允许 paper。
本轮不修改交易模式、策略参数或活体配置；后续若用户改变授权，必须在同一改动中同步
AGENTS.md、README、服务描述、运维手册与机器护栏。

# 接口优先与功能分层（权威实施稿）

> 日期：2026-08-23。目标：模块之间依赖稳定契约，不读取彼此内部状态，不在调用方散写
> 对方的数据结构或 SQL。交易安全约束、策略参数和行为保持不变。

## 1. 本轮审计发现

1. `tools/code_graph.py` 未把 `storage/` 纳入层级，导致 `storage → decision` 反向依赖
   被静默归为 unknown。
2. `service/app.py` 直接读取 `DirectionalTrader.exchange/journal/risk/rt` 以及多个
   `_private` 字段；接口层与引擎内部表示绑定。
3. `service/app.py` 直接 import `storage.db` 并散写 SQL，HTTP 层掌握了表结构。
4. 线上 HTTP 路径反向依赖 `tools/readiness.py`、`tools/entry_accuracy_audit.py`；CLI
   工具错误地成了生产依赖。
5. `DirectionalTrader` 的功能块虽已拆成多个文件，但 Mixin 通过隐式 `self.*` 共享
   可变协作者；当前用服务侧运行接口先隔离外部消费者，内部组件接口继续由契约守卫推进。
6. 存储层 Agent 模块 import 决策层契约，违反底层适配器不得反向依赖业务层的规则。

## 2. 分层与依赖方向

```text
HTTP / 运维入口（service）
            ↓ TradingRuntimePort
交易应用与功能编排（engines）
            ↓ 公开决策/执行接口
决策、执行、风控、策略（decision / execution / risk / strategy）
            ↓ query/repository API、外部适配器接口
持久化、交易所、数据源（storage / exchange / data）
            ↓
稳定契约与集中配置（interfaces / config）
```

`tools`、`tests`、`backtest` 是外围入口，可以消费核心模块；核心运行链不得反向 import
这些外围入口。`factors` 是离线研究功能，不拥有交易执行权限。

## 3. 已落地边界

- `interfaces/trading.py`：定义无框架依赖的 `TradingRuntimePort`。
- `interfaces/agent.py`：承载决策与存储共同依赖的 Agent 数据契约；存储层不再反向
  import `decision`，原入口仅作兼容转发。
- `engines/runtime_api.py`：把方向引擎内部对象图翻译为稳定、只读快照；所有状态、候选、
  信号、台账、实时行情、扫描刷新和对账统一从这里进出。
- `storage/query_api.py`：封装服务所需只读查询，SQL 与 schema 不再泄漏到 HTTP 层。
- `storage/*_repository.py`：集中台账、持仓所有权、运行错误和异常事件的持久化；执行与
  worker 不再掌握这些表的 SQL。
- `decision/api.py`：统一服务侧决策能力入口，内部模块可以重组而不扩散到 HTTP 路由。
- `decision/readiness.py`、`decision/entry_accuracy_audit.py`：生产决策审计回归业务层；
  `tools/` 只保留兼容 CLI 包装器。
- `service/app.py`：只消费上述接口，不再直接访问引擎协作者或 `storage.db`。
- `DirectionalTrader`：组合根支持 journal、决策器、经验库、持仓账本、风控、通知和
  事件记录器注入，并让默认协作者统一使用实例 `db_path`。
- `tests/test_interface_boundaries.py`：以结构化 Protocol、接口行为和 AST 禁止项作为回归证据。

## 4. 接口规则

1. 跨功能包传递领域对象或不可变快照，不传交易所原始响应。
2. 调用方只使用公开接口；下划线开头的符号和字段只允许包内使用。
3. 数据库 schema 和 SQL 归存储/所属仓储接口；HTTP、交易编排层不得散写 SQL。
4. 外部实现必须可替换：交易所继续遵守 `ExchangeAdapter`，服务运行时遵守
   `TradingRuntimePort`，测试使用 Fake/Stub。
5. 接口变化先改契约测试，再同步实现和调用方；失败继续 fail-closed。
6. 接口化不得改变 1% 单笔风险、150 USDT 名义上限、600 USDT 总敞口、交易所侧止损
   和 paper-only AI 授权。

## 5. 验收

- 服务 API 行为回归必须全绿。
- `tests/test_interface_boundaries.py` 必须全绿。
- `tools/code_graph.py --check` 必须识别 `storage/interfaces`，且无反向依赖、import 环或
  跨层共享状态写入。
- `service/app.py` 不得 import `storage.db`、`tools.readiness` 或
  `tools.entry_accuracy_audit`，也不得直接解引用 `_trader(request)` 的内部属性。

# 加密货币交易 Agent 调研报告（第三轮 · 外部情报官 Agent A）

> 本轮聚焦"第 1 轮裁定与实施后仍未覆盖的空白"。已核对 `optimization_plan_agentB_R1.md`（Agent B R1-1~R1-9），避免与已立项项重复。
> 每条 = 标题 + URL + 一句现状差距 + 一句收益 + 优先级。

---

## R3-1 OKX 交易所侧 bracket order（attachAlgoOrds 挂 TP/SL 一体 + OCO）

- **来源URL**：[OKX API V5 attachAlgoOrds 参数文档](https://www.okx.com/docs-v5/en/)、[OKX 挂单参数 tpOrder['tpOrdPx']='-1' market tp（ccxt okx.py 源码）](https://raw.githubusercontent.com/ccxt/ccxt/d71c957c08b8edde26019d1671b3bf89e6428c6a/python/ccxt/okx.py)、[NautilusTrader：Support attached TP/SL orders for OKX SWAP](https://github.com/nautechsystems/nautilus_trader/issues/3693)、[tiagosiebler/okx-api 交易端点（DeepWiki）](https://deepwiki.com/tiagosiebler/okx-api/2.2-trading-endpoints)
- **现状差距**：`directional_trader.open_position`（198–208 行）只挂交易所侧**止损**单（`ordType:"conditional", triggerPrice:stop, reduceOnly`），**止盈只在本地 `monitor()`（255 行 `price >= tp`）判断**，进程崩溃后止盈无人执行；未用 OKX `attachAlgoOrds`（一条主单同时附带 TP/SL，甚至 OCO、split-TP）。
- **预期收益**：把"止盈"也推到交易所侧（与止损同级的进程崩溃保护），用 attachAlgoOrds 一条请求挂 TP+SL 一体，消除"本地进程挂了只能止损不能止盈"的不对称；OCO 可让 TP/SL 任一触发自动撤另一腿，省挂单额度。
- **优先级**：**高**

## R3-2 OKX 子账户隔离（主/子账户 + 资金划转 + API 权限隔离）

- **来源URL**：[OKX 子账户、账户模式及 API 连接 FAQ](https://www.okx.com/zh-hans-sg/help/subaccounts-account-mode-and-api-connections-faq)、[OKX Sub-Account API Key Permissions: How to Isolate Bot Risk Safely (2026)](https://supa.is/article/okx-sub-account-api-key-permissions-isolate-bot-risk-safely-2026)、[OKX Sub-Account vs Main Account: When You Actually Need One (DEV)](https://dev.to/xqliu/okx-sub-account-vs-main-account-when-you-actually-need-one-2026-guide-ge6)、[Fireblocks：OKX API V5 sub-account](https://support.fireblocks.io/hc/en-us/articles/18186360525724-OKX-OKEx-API-V5-sub-account)
- **现状差距**：`directional_trader`/`trading_main`/`funding_arb`/`trading_daemon`/`trading_agent` 全部读同一个 `okx_config.json` 的同一套 key、共用主账户；R1-6 只做"同币种幂等"缓解、未做账户级隔离，套利对冲仓与方向仓在同一个保证金账户里互相覆盖杠杆/占用保证金。
- **预期收益**：套利与方向仓分到两个子账户（独立 API key、独立保证金、独立爆仓线、独立资金划转），从根上消除"跨进程 set_leverage 互相覆盖 / 共账户爆仓连带"；即使一个进程发疯，也只亏自己子账户的钱。
- **优先级**：**高**

## R3-3 进程级健康自检 + 看门狗（heartbeat 文件 + 崩溃自动重启）

- **来源URL**：[My Live Trading Bot Was Hung for 7 Hours. Here's the System That Fixed It](https://www.jeremyknox.ai/blog/live-trading-bot-hung-7-hours-built-horus/)、[Keeping a Trading Robot Alive: Monitoring, Failover and 24/7 Operations](https://pipflow.com/forum/Thread-keeping-a-trading-robot-alive-monitoring-failover-and-24-7-o)、[processWatchdog — must-have tool for everybody who runs bots](https://beta.ninjastic.space/post/65234690)
- **现状差距**：`realtime_okx.py` 有 WS 层监督线程 + `_pinger`，但**没有进程级 watchdog/heartbeat 文件/崩溃自动重启**；`trading_main.run` / `directional_trader.run` 的主循环一旦进程死掉或 hang 住，无任何机制重启或告警（`data/com.okx.collect.plist` 的 launchd 只管采集，不管交易主进程）。
- **预期收益**：写 heartbeat 文件 + 外层看门狗（launchd/systemd/supervisor）检测 heartbeat 超时即重启 + 飞书告警；把"进程崩溃/hang 后无人接管"的静默死机窗口从无限长压到 ≤1 分钟。
- **优先级**：**高**

## R3-4 资金费率套利精确盈亏核算（补充：funding 结算时间对齐 + 开源实现参考）

- **来源URL**：[funding-arb-bot（Hyperliquid+Lighter 套利 bot 开源实现）](https://github.com/Gajesh2007/funding-arb-bot)、[funding-rate-arbitrage Profitability Analysis（DeepWiki）](https://deepwiki.com/50shadesofgwei/funding-rate-arbitrage/2.2-profitability-analysis)、[Loris Tools Funding Arb Backtest](https://loris.tools/docs/guides/funding-arb-backtest)、[hamood1337/CryptoFundingArb（CEX/DEX 费率扫描）](https://github.com/hamood1337/CryptoFundingArb)
- **现状差距**：`trading_main._close_hedge`（362–365 行）`funding_pnl = abs(entry_rate)*3*days_held`、`net_pnl = funding_pnl - 0.003` 是**估算**、忽略基差盈亏；此缺口已被 Agent B **R1-4 立项**（`fetch_ledger(type="funding")` + 成交价 + 基差）。本条的增量点：R1-4 未覆盖 **funding 结算时间对齐**（在结算时刻前后开/平仓会导致 funding 归属错位，需按 `bills` 的 `ts` 精确切分到持仓区间），以及给出开源实现参考。
- **预期收益**：在 R1-4 基础上补"结算时间对齐"，避免 funding 收入被错误计入不持有该仓位的时段；开源 bot（Gajesh2007 等）的 PnL 核算逻辑可直接对照。
- **优先级**：**中**（R1-4 已立项，本条为补充）

## R3-5 借鉴成熟事件驱动框架的订单状态机（NautilusTrader），收敛自研胶水 bug

- **来源URL**：[NautilusTrader（含 OKX 适配与 attached TP/SL 支持）](https://github.com/nautechsystems/nautilus_trader/issues/3693)、[NautilusTrader 官方仓库](https://github.com/nautechsystems/nautilus_trader)
- **现状差距**：自研 ccxt 胶水已多轮暴露同一类 bug（`posSide` 漏传、`round(x, float)` TypeError、空头方向判断错、`max(700,100)` 恒 700、幽灵条件单残留等，见 OPTIMIZATION_NOTES / R1-1）；每次修一处，缺少统一的订单状态机与 OCO/一篮子订单管理。
- **预期收益**：不一定要整体迁移，但借鉴其订单状态机/`attachAlgoOrds`/OCO 建模，把"开仓→挂 TP/SL→取消幽灵单→平仓"收敛成有状态、可审计的订单管理器，从结构上减少重复 bug。
- **优先级**：**中**

---

## 小结（协调者速览）

- **高优先级 3 条（R3-1/2/3）**：均为"进程崩溃/账户串扰"级的结构性防护，零过拟合、直接堵住当前最大的工程空白——止盈未挂交易所侧、套利与方向仓共用账户、进程崩溃无人接管。与 Agent B R1（幽灵单取消、跨进程幂等、状态文件原子写）互补而不重复。
- **R3-4**：方向已被 Agent B R1-4 覆盖，本条只补"funding 结算时间对齐"这一 R1-4 未提的细节，勿重复立项。
- **R3-5**：中等优先级，是"减少自研 bug 的结构性方案"，建议与 R3-1 合并评估（都是订单管理层面）。

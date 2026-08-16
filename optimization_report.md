# 加密货币日内短线交易 Agent 优化提案调研报告

> 调研方式：实际通读 `crypto-agent` 代码（scoring.py / trading_main.py / realtime_okx.py / directional_trader.py / factor_evolution.py / factor_mining.py / funding_arb.py / experience_scoring.py / threshold_learning.py / review_engine.py / config.py / risk/risk_manager.py / backtest/ml_model.py 等）+ 真实 web_search 检索外部资料。每条结论均附检索得到的来源 URL。

---

## 代码现状关键发现（对照背景）

1. **风控模块从未接线**：`risk/risk_manager.py` 实现了 `RiskManager`（回撤 20% 熔断、单日亏损 1.5% 停手、`can_trade()`），但 `trading_main.py`、`directional_trader.py` 都没有 import 它，实时循环里 `can_trade()` 从未被调用。
2. **止损监控是"每 6 小时一次"**：`directional_trader.run()` 里 `time.sleep(3600*6)`，`monitor()` 里的止损/止盈检查 6 小时才跑一次——对日内短线（目标 5%、2:1 盈亏比）等于止损形同虚设。
3. **WebSocket 无重连、无心跳**：`realtime_okx.py` 的 `_on_close` 只把 `_running=False`，不重连；OKX 要求客户端回 `pong`/定时 `{"op":"ping"}`，代码未处理，断线后 `get()` 静默返回空/旧数据。
4. **资金费率套利无平仓逻辑、无基差管理**：`funding_arb.py monitor` 只看不撤；`trading_main` 只在费率翻转时告警，不自动平仓；未跟踪基差（perp−spot）。
5. **因子挖掘的 GA 算子是"空操作"**：`factor_evolution.py` 的 `swap_subtree` 只是随机返回父节点之一、`mutate` 60% 概率原样返回；只有单一 train/test 切分；全样本标准化后再切分（泄漏）；标签是 7 日收益（非日内）；只回测 BTC。
6. **ML/阈值无 walk-forward、无 purged CV**：`ml_model.py` 单一时间切分；`threshold_learning.py` 假设"分数越高收益单调"，无时间衰减。
7. **订单流只有"笔数比"**：`realtime_okx.py` 用 `buys/total`（笔数）而非成交量加权；未订阅 orderbook（books5）；`data/fetch_orderflow.py` 用币安 REST 轮询（1000 笔）做快照，非实时、跨交易所。
8. **执行细节缺失**：`trading_main.execute` 算 `amount=150/price` 后直接下单，未校验最小下单量/最小名义额/合约张数(`ctVal`)单位，DOGE/XRP 类低价币易出错。

---

## 优化提案（10 个，按收益/风险比排序）

### OP-1：止损/止盈改为 tick 级实时 + 交易所侧停损单 + 风控熔断真正接线
- **来源/证据**：freqtrade 官方文档 [Stop Loss](https://www.freqtrade.io/en/2022.12/stoploss/) 与 [Protections](https://www.freqtrade.io/en/2021.9/includes/protections/)（StoplossGuard/MaxDrawdown 等），以及 [WebSocket Failover：Never Miss a Trade](https://voiceofchain.com/academy/websocket-failover-crypto-bot)。
- **现状差距**：`directional_trader.run()` 用 `time.sleep(3600*6)`，`monitor()` 每 6 小时才查一次止损/止盈；`risk/risk_manager.py` 的 `RiskManager`（回撤 20% 熔断、单日亏 1.5% 停手）**从未被 import 或调用**。这是系统当前最大的下行漏洞。
- **实施方案**：
  1. 在 `directional_trader.py` 起一个独立监控线程，直接读 `OKXRealtime.latest[base]["price"]`（已有 WebSocket 推送），每 tick 判断是否触达 `stop_loss`/`take_profit`，替代 6 小时轮询。
  2. 用 ccxt 下**交易所侧**停损单（`create_order(..., params={"reduceOnly": True, "triggerPrice": ..., "ordType": "conditional"})`），这样即使本地进程崩了，止损仍在 OKX 服务器生效——freqtrade 的标准做法是 stoploss 挂交易所而非本地轮询。
  3. 在 `trading_main.py` 与 `directional_trader.py` 的循环里实例化 `RiskManager(initial_equity)`，每次决策前 `if not rm.can_trade(): skip`，每笔平仓后 `rm.update_equity(...)`。
- **预期收益**：把单笔亏损从"无上限（6 小时盲区）"压回预设的 1×ATR（约 1%~1.5%）；单日亏损硬限 1.5%、回撤硬限 20% 从"写死不用"变成"真实熔断"。定量：日内系统止损响应从 ≤6h 降到 <1s，直接消除尾部亏损。
- **风险与成本**：纯风控，无过拟合风险；实现成本低（半天级）。唯一注意：市价止损在插针时滑点，需配合 `review_engine` 已识别的"止损放结构点外+0.3ATR 缓冲"。
- **优先级**：**高**（第 1 优先）

### OP-2：WebSocket 健康检查 + 自动重连 + OKX 心跳 + 数据新鲜度
- **来源/证据**：[WebSocket Heartbeat: Keep Your Crypto Exchange Connection Alive](https://voiceofchain.com/academy/websocket-heartbeat-crypto-exchange)、[WebSocket Failover for Crypto Bots](https://voiceofchain.com/academy/websocket-failover-crypto-bot)、交易所适配规范（OKX 要求 `{"op":"ping"}`、Binance 用 PING/PONG，[datasea 文档](https://datasea.cn/go0330566589.html)）。
- **现状差距**：`realtime_okx.py` 的 `_on_close` 只置 `_running=False`，**不重连**；未处理 OKX 的应用层 `ping`；`get()` 无条件返回可能已过期的 `latest[base]`，事件驱动系统会拿旧价格/旧费率做决策。
- **实施方案**：
  1. `OKXRealtime.start()` 外层加 `while True` 重连循环 + 指数退避（1s/2s/4s…cap 60s），把 `run_forever` 放进带重试的线程。
  2. 在 `_on_message` 增加对 `"event":"error"` 与 `"ping"` 的处理，回复 `{"op":"pong"}`；并用 `websocket.WebSocketApp(..., ping_interval=20, ping_timeout=10)` 做协议层保活。
  3. 给 `latest[base]` 每条数据打 `ts` 时间戳，`get(base)` 返回时判断 `now - ts > 30s` 则视为 stale（返回空），强制走 REST 兜底并告警。
- **预期收益**：避免"断线后拿旧数据下单/漏掉信号"。对事件驱动系统，连接可用性直接决定是否执行；预计消除断线期间的全部误决策。
- **风险与成本**：纯工程可靠性，零过拟合；成本半天级。
- **优先级**：**高**（可与 OP-1 并行，属"快速止血"）

### OP-3：资金费率套利加"挤压陷阱"过滤 + 费率翻转自动平仓 + 基差风险跟踪
- **来源/证据**：[That 300% Funding APR Is Not Free Money: Screening For Squeeze Traps on Binance Perpetuals](https://dev.to/godzilla_dev/that-300-funding-apr-is-not-free-money-screening-for-squeeze-traps-on-binance-perpetuals-a1o)、[The crypto arbitrage playbook: what still pays in 2026 (and what's a trap)](https://docs.ccxt.com/blog/crypto-arbitrage-strategies)、[Crypto Basis Trades: Funding Rates, Exchange Risk & Margin](https://www.cv5capital.io/insights/crypto-basis-trades-institutional-funds)、[Spot-Perp Basis & Funding Strategies: How It Works (and Where It Fails)](https://blofin.com/academy/education/spot-perpetual-futures)、开源实现 [kmrlab/algo-arbitrage](https://github.com/kmrlab/algo-arbitrage)。
- **现状差距**：`trading_main.execute` 开现货+合约对冲后**从不自动平仓**（费率翻转只 `_alert` 不撤单）；`funding_arb.monitor` 只看状态；未跟踪基差（perp−spot index）；极端费率被当成"年化越高越该套"（`score_funding_rate` 单调递增），这正是 squeeze trap 的特征。
- **实施方案**：
  1. 在 `realtime_okx.py` 增加 `mark-price` 或复用 `tickers` 的 perp 价 vs 现货价，维护 `basis = perp_price/spot_price - 1` 的滚动窗口。
  2. `trading_main` 新增 `manage_arb_positions()`：费率**连续 2 个结算周期翻转**（而非单次翻转）→ 自动平对冲仓；基差向不利方向扩大超阈值（如 BTC >0.5%）→ 减仓/平仓。
  3. `score_funding_rate` 改为**非单调**：极端费率（|年化|>80%）反而降分（squeeze 风险），只在"中等偏高且稳定"区间给高分；叠加 OI 突变过滤（OI 快速飙升+高费率=逼空，不进）。
  4. 杠杆降为 1x（对冲本身无需杠杆，2-3x 只降低保证金率、抬高爆仓风险，见 cv5capital 对保证金风险的论述）。
- **预期收益**：保住当前唯一转正方向（年化 8-11%）并规避其最大回撤来源。定量：避免一次"费率反转+基差扩张"即可保住约 1-2 个月的累计费率收益。
- **风险与成本**：中；需核对 OKX mark-price 订阅字段，逻辑本身不涉过拟合，但阈值需用历史费率分布校准（勿拍脑袋）。
- **优先级**：**高**（第 2 优先）

### OP-4：订单流升级——量加权 taker imbalance + OKX books5 + 累计 CVD
- **来源/证据**：[Beyond OHLCV: Crypto forecasts need order flow & liquidity](https://t.signalplus.com/crypto-news/detail/crypto-forecasting-order-flow-liquidity-regimes)、[Explainable Patterns in Cryptocurrency Microstructure (arXiv 2602.00776)](https://huggingface.co/papers/2602.00776)、[Optimal Execution Using Reinforcement Learning（order flow imbalance 综述，Cont et al.）](https://ar5iv.labs.arxiv.org/html/2306.17178)、[tradingstrategy.ai Order Flow](https://tradingstrategy.ai/docs/learn/order-flow.html)。
- **现状差距**：`realtime_okx.py` trades 频道用 `buys/total`（**笔数**，非成交量）；未订阅 orderbook；`data/fetch_orderflow.py` 用币安 REST 轮询 1000 笔做 `taker_buy_ratio`（跨交易所、非实时、限 1000 笔）。`score_orderflow` 只有笔数比一种。
- **实施方案**：
  1. `realtime_okx.py` 增订 `books5` 频道，实时算 `orderbook_imbalance`（前 5 档 bid/ask 量差）。
  2. trades 频道改为**成交量加权** taker imbalance：累加 `side=="buy"` 的 `sz` vs `side=="sell"` 的 `sz`，滚动 5-15 分钟窗口；并维护累计 CVD（`Σ(买量-卖量)`）。
  3. 用币安历史 `aggTrades`（`isBuyerMaker`，数据源已有）先做一次**严谨回测**：量加权 taker imbalance → 未来 5/15 分钟收益的 IC，验证有 edge 再接入 `scoring.score_orderflow` 替换笔数比。
- **预期收益**：量加权订单流是文献里少数"价格之外"的可验证短期 edge；替换 `DIR_WEIGHTS` 里 0.15 权重的最弱信号，方向性入场胜率有望提升 2-5pct（需回测确认，勿直接采信）。
- **风险与成本**：中；回测易过拟合（短周期样本少），必须用 OP-7 的 walk-forward 验证；数据量中等。
- **优先级**：**中**（先回测验证，后接入）

### OP-5：HAR/已实现波动率预测替换 15m 振幅 + 止损宽度自适应
- **来源/证据**：[Predicting the Volatility of Cryptocurrencies' Returns Using High-Frequency Data: GARCH/EGARCH/GJR-GARCH/HAR 对比](https://www.mdpi.com/2227-7072/14/4/90)（HAR 对加密 RV 拟合/预测最优）。
- **现状差距**：`scoring.score_volatility` 用 15 分钟 high-low 振幅（`realtime_okx` 只留 15 根 1m K 线取 max-min），噪声大、无预测性；止损宽度静态 1×ATR（`directional_trader`），未随波动率预测调整。
- **实施方案**：
  1. 在 `realtime_okx.py` 用 1m 收盘价计算已实现波动率 RV（如 5m 收益平方和），并做 HAR-RV：`RV_t = β0 + βd*RV_{t-1d} + βw*RV_{t-1w} + βm*RV_{t-1m}`（用 numpy 线性回归即可，无需 sklearn）。
  2. `score_volatility` 改用 RV 与 HAR 预测值：预测 RV 即将扩张 → 低分（避开波动爆发前入场）；RV 极低 → 低分（无动能）。
  3. `directional_trader.scan_signal` 的止损倍数 `1×ATR` 改为 `max(1.0, 1.5×HAR预测RV/历史均值)`，高波动放宽、低波动收紧（与 `review_engine` 已识别的"止损太紧被插针扫掉"对齐）。
- **预期收益**：降低"止损被噪音扫掉"概率；`review_engine` 里反复出现的"止损太紧"类教训正是此问题。定量目标：减少无效止损出场，方向性净胜率/盈亏比改善（需实盘累计验证）。
- **风险与成本**：中；HAR 参数需在 BTC/ETH 上做样本外验证，避免对单一币过拟合；计算量小。
- **优先级**：**中**

### OP-6：清算级联事件检测（OI 突变 + 价格 + 费率共振）作为事件信号
- **来源/证据**：[CoinGlass API（清算数据/OI/资金费率）](https://www.coinglass.com/zh/CryptoApi)、[jarvis-market-signals（perp liquidations、funding crowding MCP）](https://github.com/casterdly/jarvis-market-signals)、[XRP Liquidation Imbalance Explained](https://www.mexc.ee/learn/article/xrp-liquidation-imbalance-explained-what-traders-need-to-know)。
- **现状差距**：系统完全无清算数据；`check_signal_event` 只有"费率突破/翻转/2 分钟价格动 2%"三类，漏掉 perp 市场最大的日内运动催化剂（清算级联）。
- **实施方案**：
  1. 接入一个清算数据源：CoinGlass 免费 API，或 OKX/Bybit 的强平推送；若外部受限（服务器在北京访问不了 CoinGecko/OKX 的先例），用**代理指标**：`ΔOI（快速下降）+ |价格 1min 变动| > 阈值 + |费率| 飙升` 三者共振判"疑似级联"。
  2. `trading_main.check_signal_event` 增分支：级联进行中 → 方向性交易**不抄底**（避开接飞刀）、资金套利**暂停开仓**（避开 squeeze）；级联后 OI 回稳 + 价格止跌 → 触发"超跌反转"方向性入场信号（均值回归）。
- **预期收益**：避免在最差时点进场（级联是高费率 squeeze 的直接诱因）；级联后的超跌反转是日内可验证的均值回归窗口。
- **风险与成本**：中高；代理指标阈值需历史校准；CoinGlass 免费层有调用限额。
- **优先级**：**中**

### OP-7：防过拟合改造——walk-forward + purged K-fold + 特征稳定性选择 + 修复 GA 算子
- **来源/证据**：[Purged cross-validation (Wikipedia)](https://en.wikipedia.org/wiki/Purged_cross-validation)、[RiskLabAI（López de Prado《Advances in Financial ML》实现）](https://pypi.org/project/RiskLabAI/)、[purgedcv（带 purging/embargo 的时序 CV）](https://pypi.org/project/purgedcv/)、[walk-forward validation 规范](https://github.com/NeverSight/learn-skills.dev/blob/main/data/skills-md/agiprolabs/claude-trading-skills/walk-forward-validation/SKILL.md)。
- **现状差距**（直接对应"遗传复合因子 IC 0.12-0.18 可能虚高"的怀疑）：
  - `factor_evolution.py`：`swap_subtree` 只是 `return b if random<0.5 else a`（**无真正子树交换**），`mutate` 60% 概率原样返回（**几乎不变异**）；`vals` 全样本标准化后再 split（**测试集信息泄漏**）；标签是 7 日收益（**非日内**）；只 BTC。
  - `ml_model.py`：单一 `SPLIT_TS` 切分，无 purge/embargo，标签 5 日 >2%。
  - `threshold_learning.py`：假设分数→盈亏单调，无时间衰减，30 样本就校准。
- **实施方案**：
  1. 把 `factor_evolution.py` 的 `swap_subtree` 改成真正的随机子树替换（选择随机节点切下子树互换），`mutate` 改成真算子替换/子树重生成；`vals` 标准化**只在 train 内**计算 mean/std。
  2. 引入 walk-forward：按时间滚动窗口（如训练 2 年 → 验证未来 3 个月 → 滚动），只统计各窗口**样本外** IC 的中位数与稳定性；标签改为日内（如 15m/1h 收益）以匹配系统定位。
  3. 加 purged K-fold + embargo（标签 7 日重叠需 purge 7 天 + embargo），用于 `ml_model.py` 与阈值校准。
  4. 加**特征稳定性选择**：同一因子多种子跑 N 次，只在 ≥80% 次中同号且 |OOS IC| 稳定的因子才进 `DIR_WEIGHTS`。
  5. `threshold_learning.py` 加时间衰减（旧决策权重指数下降），避免陈旧样本主导阈值。
- **预期收益**：诚实地把样本内 IC 0.12-0.18 打回真实样本外水平（预计 0.05-0.10 或更低），**避免把噪声因子当 alpha 实盘**——这是自进化系统不亏钱的前提。
- **风险与成本**：中；纯验证基建，无实盘风险；工作量中等（1-2 天）。
- **优先级**：**高**（第 3 优先）

### OP-8：资金费率截面因子（cross-sectional funding percentile）替换单币绝对阈值
- **来源/证据**：[OctopusTakopi/funding-rate-alpha：Do funding rates predict returns? 6.1M settlements 无幸存者偏差检验](https://github.com/OctopusTakopi/funding-rate-alpha)（README：[Funding Rates as a Cross-Sectional Factor in Perpetual Futures](https://raw.githubusercontent.com/OctopusTakopi/funding-rate-alpha/main/README.md)）、[Presto Research: Can Funding Rate Predict Price Change?](https://www.prestolabs.io/research/can-funding-rate-predict-price-change)。
- **现状差距**：`trading_main` 只对单个币用绝对年化阈值（≥8%）判断套利；`score_funding_rate` 只认绝对值大小。文献结论：费率作为**截面因子**（long 低费率/空头拥挤、short 高费率/多头拥挤）比单币绝对值更稳定。
- **实施方案**：
  1. 用已有的 Gate.io `fetch_all_funding_rates()` + OKX 5 币 WS 费率，构建**截面费率百分位**：某币费率处于历史/横截面前 10% 分位 → 多头拥挤 → 方向性做空偏好 / 做多降分。
  2. `scoring.py` 新增 `score_funding_percentile(percentile, direction)`，并入 `DIR_WEIGHTS`（如 0.10 权重），从情绪/OI 里拆出。
  3. 套利选币从"绝对值最高"改为"截面拥挤度极端 + 费率方向一致"。
- **预期收益**：方向性策略获得文献验证的截面 alpha；费率拥挤度是逼空/逼多反转的领先信号，预计提升方向性入场的方向正确率。
- **风险与成本**：中；截面需足够币数（当前仅 5 币偏少，建议扩到 20-30 币），否则分位无意义。
- **优先级**：**中**

### OP-9：执行细节——最小下单量/最小名义额/合约张数(ctVal)校验 + 滑点分档 + 低价币处理
- **来源/证据**：[OKX 关于部分合约最小下单数量调整的公告](https://www.okx.com/zh-hans/help/okx-to-adjust-the-minimum-order-quantities-for-several-futures-2024-03-21)。
- **现状差距**：`trading_main.execute` / `funding_arb.open_hedge` 算 `amount=150/price` 后直接下单，未校验 `limits.amount.min`、`limits.cost.min`（最小名义额）、OKX swap 的**合约张数单位（ctVal/contractSize）**；`if amount<=0: amount=0.01` 可能仍低于最小下单量导致拒单；`directional_trader` 有 `max_amt` 校验但另外两个文件没有；低价币（DOGE/XRP）滑点与 lot size 差异未处理。
- **实施方案**：
  1. 新建 `execution.py`，集中封装 `qty_for_notional(exchange, sym, notional_usdt)`：读 `market["limits"]["amount"]["min/max"]`、`market["limits"]["cost"]["min"]`、`market["precision"]["amount"]`、`market.get("contractSize")`/`ctVal`，正确换算**合约张数**，并做 min/max 夹逼 + tick 对齐。
  2. 预过滤：150 USDT 名义 < 最小名义额的币直接跳过（或提高到满足 min notional）。
  3. 复用 `config.SLIPPAGE_*` 分档：低价/低流动性币（成交额<1000 万）自动减仓或跳过。
  4. 下单前做参数自检，失败记录到日志并告警，不再静默吞异常。
- **预期收益**：消除拒单与"数量单位搞错"类事故；对 DOGE/XRP 类执行更稳健。定量：降低执行失败率到接近 0。
- **风险与成本**：低；纯执行正确性，无过拟合；成本半天。
- **优先级**：**中**

### OP-10：freqtrade 式"熔断/冷却"保护 + 健康自监控（借鉴成熟开源机器人）
- **来源/证据**：[freqtrade Protections（StoplossGuard / MaxDrawdown / CooldownPeriod / StagnationProtection）](https://www.freqtrade.io/en/2021.9/includes/protections/)、[protections 完整示例](https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/includes/protections.md)。
- **现状差距**：系统只有 30 分钟决策冷却（`decision_cool`）；`RiskManager.can_trade()` 未接线（见 OP-1）；无 StagnationProtection（长期无盈利自动暂停）、无 MaxDrawdown 自动熔断；经验评分 `experience_scoring` +10/−15 无时间衰减，陈旧教训永久生效。
- **实施方案**：
  1. 在 `trading_main` / `directional_trader` 接入 `RiskManager`（与 OP-1 合并），实现 MaxDrawdown 20% 硬停、单日 1.5% 停手。
  2. 新增 `protections.py`：连亏 N 笔冷却（已有雏形在 `SelfEvolvingTrader`，补进主循环）、N 小时无开仓且净值无增长 → Stagnation 暂停并告警。
  3. 心跳自监控：WS 数据 stale >60s、或 N 小时无任何决策 → lark 告警。
  4. `experience_scoring.validate` 加分改为带时间衰减（旧分数向 50 回归），避免市场结构变化后旧经验误导。
- **预期收益**：系统性回撤控制 + 异常告警，把"回撤 ≤20% 硬约束"从纸面变成真约束。
- **风险与成本**：低；逻辑不涉过拟合；成本 1 天。
- **优先级**：**中**（可与 OP-1 一起做）

---

## Top 3 及理由

1. **OP-1（tick 级止损 + 风控熔断接线）**——**最先做**。这是纯下行保护，零过拟合风险，却直接堵住系统当前最大的两个漏洞（止损每 6 小时才查、`RiskManager` 从未被调用）。日内短线系统若止损不及时，任何 alpha 都会被一次插针/跳空打穿。收益/风险比最高，且是所有其它提案生效的前提。

2. **OP-3（资金费率套利的挤压陷阱过滤 + 费率翻转自动平仓 + 基差跟踪）**——**第二做**。因为已知结论里"资金费率套利年化 8-11% 是唯一转正方向"，而它的主要失效模式（费率反转、基差扩张、squeeze trap、杠杆爆仓）在当前代码里一个都没防。保住唯一正 EV 策略，比新增策略更确定。

3. **OP-7（walk-forward + purged K-fold + 特征稳定性 + 修复 GA 算子）**——**第三做**。因为系统"五层自进化"的 alpha 来源（遗传因子 IC 0.12-0.18）明确标注了"样本内、可能虚高"，而 `factor_evolution.py` 的 GA 交叉/变异实际是空操作、且存在全样本标准化泄漏。不先做诚实验证，就会把噪声因子当 alpha 投到实盘，等于在负 EV 上堆仓位。它是把"自进化"从自我安慰变成可信任引擎的关键。

> 附带建议：**OP-2（WebSocket 重连/心跳）成本半天、收益确定，建议与 OP-1 并行顺手做掉**，它不改变策略、只保证事件驱动系统"不会在断线时瞎决策"。

---

## 附：本报告引用的关键外部来源汇总

| 方向 | 来源 |
|---|---|
| 资金费率套利/基差 | [CCXT playbook](https://docs.ccxt.com/blog/crypto-arbitrage-strategies)、[squeeze traps](https://dev.to/godzilla_dev/that-300-funding-apr-is-not-free-money-screening-for-squeeze-traps-on-binance-perpetuals-a1o)、[basis risk](https://www.cv5capital.io/insights/crypto-basis-trades-institutional-funds)、[kmrlab/algo-arbitrage](https://github.com/kmrlab/algo-arbitrage) |
| 订单流 edge | [SignalPlus](https://t.signalplus.com/crypto-news/detail/crypto-forecasting-order-flow-liquidity-regimes)、[arXiv 2602.00776](https://huggingface.co/papers/2602.00776)、[arXiv 2306.17178](https://ar5iv.labs.arxiv.org/html/2306.17178) |
| 波动率预测 | [MDPI HAR/GARCH 对比](https://www.mdpi.com/2227-7072/14/4/90) |
| 清算级联 | [CoinGlass API](https://www.coinglass.com/zh/CryptoApi)、[jarvis-market-signals](https://github.com/casterdly/jarvis-market-signals) |
| 开源机器人 | [freqtrade stoploss](https://www.freqtrade.io/en/2022.12/stoploss/)、[freqtrade protections](https://www.freqtrade.io/en/2021.9/includes/protections/)、[WS heartbeat](https://voiceofchain.com/academy/websocket-heartbeat-crypto-exchange) |
| ML 防过拟合 | [Purged CV](https://en.wikipedia.org/wiki/Purged_cross-validation)、[RiskLabAI](https://pypi.org/project/RiskLabAI/)、[purgedcv](https://pypi.org/project/purgedcv/) |
| 费率截面因子 | [funding-rate-alpha](https://github.com/OctopusTakopi/funding-rate-alpha)、[Presto](https://www.prestolabs.io/research/can-funding-rate-predict-price-change) |
| 低价币/最小下单量 | [OKX 最小下单量公告](https://www.okx.com/zh-hans/help/okx-to-adjust-the-minimum-order-quantities-for-several-futures-2024-03-21) |

# 加密货币交易 Agent 调研报告（第二轮 · 外部情报官 Agent A）

> 本轮全部为【新】方向，不与 docs/reports/optimization_report.md 的 OP-1~OP-10 重复，不涉及已实施项。
> 已通读 scoring.py / directional_trader.py / trading_main.py / realtime_okx.py / funding_arb.py / weight_learning.py / risk_manager.py / evolution_gate.py / threshold_learning.py / experience_scoring.py / review_engine.py / factor_evolution.py / data/fetch_open_interest.py / data/fetch_orderflow.py / docs/reports/backtest_report.md / docs/architecture/mtf_resonance_design.md。

---

## NEW-1 波动率目标（Volatility Targeting）动态仓位缩放

- **来源URL**：[Moreira & Muir, "Volatility-Managed Portfolios", Journal of Finance](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12513)、[Volatility Targeting: Scaling Risk for Better Returns](https://quantdecoded.com/en/volatility-targeting-scaling-risk-for-better-returns)、[tradingstrategy.ai Volatility Modeling](https://tradingstrategy.ai/docs/learn/volatility.html)
- **现状差距**：`directional_trader.open_position` 用固定 `RISK_PER_TRADE=0.01` + 固定 `min(qty, 150/price)` 名义上限；`risk/risk_manager.py position_size` 用固定 `config.RISK_PER_TRADE=0.015`。已有 `realtime_okx` 的 `vol_15m`（价格流滚动高低点）却只用于 `score_volatility` 打分，**不用于仓位缩放**。
- **预期收益**：按已实现波动率反比缩放仓位（目标组合年化波动恒定，如 15-20%），成熟学术证据显示可显著提升 Sharpe、压平回撤；高波动时自动降仓、低波动时升仓。
- **优先级**：**高**

## NEW-2 分数 Kelly 仓位 + 作为负 EV 过滤器

- **来源URL**：[Dual Momentum Long-Short Crypto Portfolio With an Aggressive Kelly Sizer](https://pyquantlab.com/article.php?file=Dual%20Momentum%20Long-Short%20Crypto%20Portfolio%20With%20an%20Aggressive%20Kelly%20Sizer.html)、[JamesYu-analysis/Crypto-Trading-Strategy-Parameter-Optimization](https://github.com/JamesYu-analysis/Crypto-Trading-Strategy-Parameter-Optimization)、[Size Positions Right or Watch Your Account Vanish](https://dev.to/marketmastersai/python-algo-trading-size-positions-right-or-watch-your-account-vanish-4817)
- **现状差距**：`directional_trader` 无任何 Kelly；`self_evolving_trader.decide` 只有"连亏3笔冷却/2笔半仓"，`trade_journal` 已积累胜率/盈亏比却未用于仓位公式。
- **预期收益**：用历史胜率/盈亏比估计 Kelly，取 1/4~1/2 Kelly 缩放仓位；关键副产品——负 Kelly 直接拒开（当前技术策略回测负 EV，Kelly 会自动给出"不开仓"，契合"宁缺毋滥"）。无法给胜率数字，需用 journal 真实盈亏比校准。
- **优先级**：**中**

## NEW-3 执行算法：限价 maker 单 + TWAP 拆单降成本（滑点控制）

- **来源URL**：[Optimal trade execution in cryptocurrency markets (Springer)](https://link.springer.com/article/10.1007/s42521-023-00103-y)、[Fragmentation, Price Formation and Cross-Impact in Bitcoin Markets](https://ar5iv.labs.arxiv.org/html/2108.09750)、[Maker vs Taker in Crypto: Fees, Rebates, and Better Execution](https://blofin.com/en/academy/education/maker-vs-taker)、[Binance/OKX 2026 费率档 JSON](https://dev.to/jacktrader/i-open-sourced-every-binance-and-okx-2026-fee-tier-as-json-a-calculator-fnj)
- **现状差距**：`directional_trader.open_position`、`trading_main.execute`、`funding_arb` 全部用市价单（`create_order(..., "market")` / `create_market_*`），全是 taker；`config` 的 `SLIPPAGE_*` 分档只在回测用，实盘无控制。无 TWAP、无限价单。
- **预期收益**：taker 0.05-0.1% vs maker（常零费/返佣）差，150 USDT 小单成本占比最高；每笔省 0.05-0.15% 成本，直接改善净收益且零过拟合。
- **优先级**：**高**

## NEW-4 止损位分位数校准（结构点 + ATR 分位数最优距离）

- **来源URL**：[Stop-Loss Placement Is a Regime Problem, Not a Fixed Percentage Problem](https://www.hotmolts.com/post/stop-loss-placement-is-a-regime-problem-not-a-fixe-8f4454b4-6e6b-4e4a-a1e6-30e1b400df53)、[How to Set a Stop Loss That Survives Crypto Volatility (Cryptohopper)](https://www.cryptohopper.com/blog/how-to-set-a-stop-loss-that-survives-crypto-volatility-13240)、[sl_calibrator.py（止损校准器开源实现）](https://github.com/eltonaguiar/findtorontoevents_antigravity.ca/blob/main/alpha_engine/sl_calibrator.py)
- **现状差距**：`directional_trader.scan_signal` 固定 `"stop": last["close"] - atr_val`（1×ATR），`config.STOP_ATR_MULT=1.5` 未被方向仓使用；`review_engine` 反复产出"止损太紧被插针扫掉"教训。
- **预期收益**：用历史 1m/5m 数据算"止损距离分位数 vs 被插针扫掉概率"，找到最优距离（预期降低无效止损出场次数）；无法给精确胜率提升，需用项目自身 K 线回测。
- **优先级**：**中**

## NEW-5 HMM/Regime 检测做策略开关

- **来源URL**：[Step-by-Step Python Guide for Regime-Specific Trading Using HMM and Random Forest](https://blog.quantinsti.com/regime-adaptive-trading-python/)、[DaruFinance/strategy-regime（Gaussian HMM，BIC 扫描选 K=4）](https://github.com/DaruFinance/strategy-regime)、[Regime switching forecasting for cryptocurrencies (Springer)](https://link.springer.com/article/10.1007/s42521-024-00123-2)、[alfredang/hmm-ai-trader](https://github.com/alfredang/hmm-ai-trader)
- **现状差距**：全项目无任何 regime/HMM 检测；`directional_trader.scan_signal` 直接开仓不看市场状态；`strategy/filters.py` 的 BTC EMA20/50 大盘关未被方向仓调用。lib 有 scikit-learn（可用 GaussianMixture，或补装 hmmlearn）。
- **预期收益**：识别趋势/震荡/高波动状态，震荡市关闭方向仓、趋势市开启（回测 report 已证"币圈趋势市才赚钱、震荡均值回归全亏"）；可作为评分/仓位开关门。
- **优先级**：**中**

## NEW-6 链上稳定币交易所净流（on-chain exchange netflow）日内预测力

- **来源URL**：[Return and Volatility Forecasting Using On-Chain Flows in Cryptocurrency Markets (arXiv 2411.06327)](https://arxiv.org/pdf/2411.06327)、[SSRN 4630115](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4630115)、[Stablecoin Exchange Netflow 三大条件 (DA Labs)](https://dalabs.org/on-chain/stablecoin-exchange-netflow/)
- **现状差距**：`trading_main.py` 注释明确"⑨ 链上（待接）"，`gather_signals` 无任何链上字段；`fetch_open_interest.py` 只有 Gate.io 合约 OI/多空比/爆仓，无稳定币流/交易所 BTC 流量。
- **预期收益**：稳定币交易所净流入是价格之外的领先资金流信号（文献验证其日内预测力），可替换/补充当前最弱的 OI 情绪因子；需 CryptoQuant/Glassnode 类数据源，接口受限则退化为代理指标。
- **优先级**：**中**

## NEW-7 组合层相关性过滤 + 风险平价/HRP 分配

- **来源URL**：[borghei/orbiter（crypto 组合优化器：HRP/风险平价）](https://github.com/borghei/orbiter)、[Hierarchical risk parity variants for cryptocurrency portfolios (UCEMA working paper)](https://econpapers.repec.org/paper/cemdoctra/928.htm)、[Crypto Synthetic Index for All Market Conditions (Binance Research)](https://www.binance.bh/lo-LA/research/analysis/crypto-synthetic-index-for-all-market-conditions)
- **现状差距**：`directional_trader.SYMBOLS = ["BTC","ETH","SOL","XRP","DOGE"]` 5 个高 beta 强相关币，`scan_signals` 逐个**独立**开仓，无相关性矩阵、无风险平价；`config.MAX_HOLDINGS=4` 只限数量不限相关性。
- **预期收益**：同时开 4 个同向高相关仓 = 变相 4 倍 beta 集中风险；回测 report 已自证"组合分散化把回撤 52%→19.4%"。加相关性上限 + 简单等风险/HRP 即可摊薄回撤。
- **优先级**：**中**

## NEW-8 日内时段效应（time-of-day / day-of-week）作入场时机闸门

- **来源URL**：[Bitcoin's 30% price surge has a hidden rhythm (CoinDesk)](https://www.coindesk.com/markets/2026/05/06/bitcoin-s-price-rally-has-a-hidden-rhythm-here-are-the-hours-and-days-driving-gains)、[Day-of-the-Week Effects in Cryptocurrencies (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S1544612326011621)、[Intraday and daily dynamics of cryptocurrency (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S1059056024006506)、[Bitcoin gains 31% mainly during APAC and US hours (CoinMarketCap)](https://coinmarketcap.com/community/articles/69fb14a50706e668c1d47f43)
- **现状差距**：`directional_trader.run` 固定 `if now - self._last_scan >= 3600*6` 6 小时周期扫描，`trading_main.run` 每分钟轮询，均无时段闸门；`economic_calendar.py` 只处理宏观事件，无时段效应。
- **预期收益**：加密有显著时段/周内效应（APAC+US 时段贡献主要涨幅、周末流动性差），作为开仓时段闸门可避开低流动性时段、提高时段内信号权重。
- **优先级**：**低**

---

## Top 3 及理由

1. **NEW-3（限价 maker + TWAP 执行）——最先做**。纯执行成本优化、零过拟合、证据最硬；当前全链路 taker 市价单，150 USDT 小单里 taker/maker 差 0.05-0.15% 是确定性损耗，直接落到净收益。
2. **NEW-1（波动率目标仓位缩放）——第二做**。有 JF 级学术证据（Moreira & Muir），且项目已有 `vol_15m` 数据可复用，实现成本低，主要改善 Sharpe 与回撤平滑度。
3. **NEW-7（组合相关性过滤 + 风险平价）——第三做**。当前 5 个高相关币独立开仓 = 隐藏的集中 beta 风险，回测报告已用内部数据自证"组合分散化回撤 52%→19.4%"；加相关性上限即可摊薄下行。

> 未完成：NEW-2/4/5/6/8 的"定量预期收益"无法给出精确数字，需项目自身 K 线/journal 数据回测校准（本轮已到交付时限，未做回测）；NEW-6 链上数据源（CryptoQuant/Glassnode）接口可用性未实测验证。

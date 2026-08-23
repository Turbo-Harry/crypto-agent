# 开仓准确率、自动因子挖掘与极值预测实施计划

> 状态：权威实施稿（FINAL）
> 日期：2026-08-23
> 范围：OKX 模拟盘方向性日内短线
> 原则：固定止损 -1R、止盈 +2R；先采集、后验证、再影子、最后晋升；模型输出不得绕过既有风控。
> 2026-08-23 口径修订：用户指定 15m 日内短线；主信号=15m，最大持有/标签/预测窗口=4h（16 根），1H/4H 仅作环境。
> 实施状态：T0～T10 的代码与离线验收、独立 SWAP 历史重放已完成；历史结果未过成本与模型门，模拟盘自然平仓仅 1 笔、Agent 成熟结果仍为 0，不能宣称已提高准确率。

## 1. 目标与非目标

### 1.1 目标

1. 把“开仓准确率”定义为可测量的净期望，而不是单纯胜率。
2. 为所有候选信号建立无选择偏差、可重复计算的监督学习样本。
3. 建立与 15m 回踩、4h 最大持有窗口一致的日内因子挖掘流水线。
4. 输出 TP/SL 首触概率、未来最高/最低区间及其校准误差。
5. 单独验证 Agent 是否提供样本外增量，而不是把 LLM 判断当作天然 alpha。
6. 所有新能力先影子运行；用户于 2026-08-23 明确“开仓前先预测 2:1”后，真实 OKX 模拟盘改为失败关闭：没有已通过独立验证的开仓概率模型则不开仓，但继续结算拒绝候选的反事实标签。

### 1.2 非目标

- 不预测一个保证会到达的精确最高价或最低价。
- 不用短期盈利代替长期样本外正期望证据。
- 不扩大单笔风险、150 USDT 名义上限或 600 USDT 总敞口。
- 不开放 HTTP 下单接口，不弱化交易所侧止损。
- 本计划阶段不启动、不重启、不操作 live 实例。

## 2. 当前基线与主要缺口

2026-08-23 改造前只读库快照：

- 模拟盘 42 笔交易，41 笔已平；37 笔有 MFE/MAE。
- 带完整六维子分的已平仓仅 1 笔。
- AI 判断 32 条，只有 1 条已有结果。
- 预测校准只有 1 条。
- live 库 2 笔交易、0 笔已平，不能评价准确率。
- 4 个现有 factor_trials 全部为 reject。
- 多单 27 笔胜率约 66.7%，空单 14 笔胜率约 21.4%；样本跨版本，暂时只能支持“分方向验证”，不能直接改策略。

代码缺口：

1. 5 分钟扫描、0 冷却读取 1H 信号，同一根 K 线可能产生重复且高度相关的样本。
2. `trade_features` 只在成交后采集，规则拒绝和 AI 否决样本缺失，存在选择偏差。
3. 模拟盘正式门控仍主要使用固定 `SIGNAL_SCORE`，六维分尚未证明有效。
4. `factor_evolution.py` 的 BTC 日线 7 日标签与当前多币种 15m/4h 策略不一致。
5. 预测模块存在空单障碍方向、终值分布提前截断、固定百分比校准替代真实 ATR 障碍等语义问题。
6. 当前 iid 单步 bootstrap 破坏趋势连续性与波动聚集，且只检查小时末价格，不能可靠判断小时内 TP/SL 首触顺序。
7. DSR 实现返回 0～1 概率，但配置与文档使用“≥1”的比率式口径，定义需要统一。

## 3. 统一问题定义

### 3.1 开仓标签

对每个去重候选信号，在固定 H=4H（16 根 15m）窗口内用 1m K 线路径计算：

- `tp_first=1`：止盈先于止损触达。
- `sl_first=1`：止损先于止盈触达。
- `timeout=1`：窗口内两者均未触达。
- `ambiguous=1`：同一根 1m K 同时覆盖 TP 与 SL；默认按止损结算，另保留歧义标记。
- `mfe_r`：最大有利偏离 / 初始风险距离。
- `mae_r`：最大不利偏离 / 初始风险距离。
- `high_ret_h`、`low_ret_h`：窗口内最高/最低相对入场价的对数收益。
- `time_to_tp/sl/high/low`：首次触达或极值出现所需时间。

### 3.2 净期望

开仓评价使用：

`EV_R = 2×P(TP first) - 1×P(SL first) + P(timeout)×E[R_timeout] - cost_R`

固定 2:1 的无成本盈亏平衡胜率为 `1/(1+2)=33.33%`；实际门槛还要加入双边手续费、滑点和 timeout 收益，因此不能写死成 33.33% 或 50%。模型只有在样本外优于经验频率基线，且 `EV_R` 的单侧 95% 保守下界为正时才有资格影响开仓。提高胜率通过同覆盖率 precision lift 验证，不能以减少交易数伪装。

### 3.3 极值预测

不预测单点，预测条件分位：

- `U_H = max(log(high[t+1:t+H] / entry))`
- `D_H = min(log(low[t+1:t+H] / entry))`
- `high_qτ = entry × exp(Qτ(U_H | X))`
- `low_qτ = entry × exp(Qτ(D_H | X))`

首版输出 q10/q50/q90，以及经过在线校准后的 80% 预测区间。

### 3.4 15m/4h 口径的理论与边界

- 15m 是信号离散粒度，不是“买入后 15 分钟必须卖出”。选 15m 是在 1m/5m 微观结构噪声与 1H 反应迟缓之间的工程假设，不是已证明的最优周期；最终只能由本系统的样本外数据证伪或证实。
- 4h=16 个 15m bar，作为 TP/SL/timeout、极值和实际时间退出的共同 horizon。这使训练目标与执行口径一致。旧持仓没有 `max_hold_hours` 字段，部署时不追溯强平。
- 短期价格变化更适合把订单流失衡、深度和撤单作为候选信息。Cont、Kukanov 与 Stoikov 发现短间隔价格变化与 OFI 呈稳定关系，且影响斜率与市场深度反向；本项目因此采集 OFI/深度，但仍必须通过加密市场 OOS 门。
- 波动聚集与多时标波动成分使 HAR-RV 成为低复杂度基线；移动区块 bootstrap 保留短期相关性，不把 15m 收益当成 iid。
- “最高/最低”只以条件分位和 conformal 区间输出。分位回归估计条件分布的不同分位，不存在可以保证的单一顶/底价。

理论来源：

- [Cont, Kukanov & Stoikov, The Price Impact of Order Book Events](https://arxiv.org/abs/1011.6402)
- [Corsi, A Simple Approximate Long-Memory Model of Realized Volatility](https://academic.oup.com/jfec/article-abstract/7/2/174/856522)
- [Koenker & Bassett, Regression Quantiles](https://www.jstor.org/stable/1913643)
- [Romano, Patterson & Candès, Conformalized Quantile Regression](https://arxiv.org/abs/1905.03222)
- [Bailey & López de Prado, The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Bailey et al., The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)

## 4. 目标架构

数据链：

`SignalScan → signal_samples → outcome_settler → signal_outcomes`

研究链：

`signal_samples/outcomes → feature_registry → intraday_factor_mining → model_artifacts`

决策链：

`model_artifacts → decision.entry_probability → shadow_decision → evolution gate → active gate`

预测链：

`entry features → extrema quantiles + first-passage probabilities → calibration → notification/AI snapshot`

分层约束：

- `engines` 负责形成候选事件和调用决策接口。
- `decision` 负责标签语义、概率模型消费、校准与晋升门。
- `factors` 只做离线研究和模型训练，不被交易引擎直接 import。
- `storage` 提供表结构与短事务。
- `service` 只提供只读观测与既有控制，不增加下单接口。

## 5. 数据表设计

### 5.1 signal_samples

每个候选信号一行，唯一键 `(symbol, direction, timeframe, kline_ts, strategy_id, strategy_version)`。

主要字段：

- 身份：`signal_id/symbol/direction/event_ts/kline_ts/timeframe`。
- 版本：`strategy_version/config_hash/feature_schema_version`。
- 障碍：`entry/stop/tp/atr/horizon_hours`。
- 当前六维：`wick/depth/trend/volume/funding/book`。
- 新候选：动态 OFI、microprice、spread、depth、OI、basis、资金费分位、BTC beta、市场宽度、时段等。
- 决策轨迹：`rule_decision/ai_verdict/final_decision/reject_reason/trade_id`。
- 数据质量：`missing_features/source_latency_ms`。

### 5.2 signal_outcomes

- 主键：`signal_id`。
- 路径标签：`tp_first/sl_first/timeout/ambiguous`。
- 连续标签：`pnl_r/mfe_r/mae_r/high_ret_h/low_ret_h`。
- 时间标签：`time_to_*`。
- 结算元数据：`settled_at/bar_resolution/label_version`。

### 5.3 model_artifacts 与 model_evaluations

- 模型类型、版本、特征清单、训练截止时间、数据哈希。
- 每折 Brier/log-loss/净 EV/coverage/pinball loss。
- 基线对照、试验次数、DSR/PBO、分方向/币种/regime 稳定性。
- 状态：`candidate/shadow/accepted/active/rejected/rolled_back`。

## 6. 任务拆分与依赖

### T0：口径与安全护栏锁定

改动：

- 把本文件中的标签、EV、极值分位和晋升口径固化为测试夹具。
- 所有新参数仅放 `config.py` 参数统一维护区。
- 新模型先 `shadow`；开仓概率模型通过样本外与独立 shadow 门后可自动进入 active。真实 OKX 模拟盘没有 active 模型时失败关闭，FakeAdapter 与未重启 live 保持隔离。

验收：

- 参数 lint、代码图和既有全量测试不回归。
- 模型文件缺失/损坏时，严格模拟盘开仓门 fail-closed；其他兼容调用方仍 fail-safe 回现役规则。两种语义必须使用不同接口，不能混用。

依赖：无。

### T1：候选事件留样与同 K 去重

改动文件建议：

- `storage/db.py`：新增 signal_samples/signal_outcomes 表和索引。
- `engines/signal_sampling.py`：候选快照构建、配置哈希、幂等写入。
- `engines/signal_scan.py`：结构信号命中后先留样，再进入规则/AI/执行链。

规则：

- 一根已收线 15m K、同币同方向同策略版本只产生一个 signal_id。
- 规则拒绝、AI 否决、额度拒绝仍保留候选和后续反事实结果。
- 不把 5 分钟重复扫描计为独立样本。

测试：

- 同 K 扫描 12 次只写 1 行。
- 多空、跨 K、策略版本变化分别生成不同样本。
- FakeAdapter 与临时数据库完全隔离。

依赖：T0。

### T2：4H 路径结算器

改动文件建议：

- `decision/signal_outcomes.py`：纯函数首触/MFE/MAE/极值标签。
- `engines/review_pipeline.py` 或 worker 周期任务：结算到期候选。
- `tools/backfill_signal_outcomes.py`：只对有完整入场快照的历史记录回填；禁止伪造缺失特征。

规则：

- 使用 1m OHLC；同 bar 双触按 SL，另记 ambiguous。
- 结算幂等，重复执行结果一致。
- 数据不足保持 pending/missing，不用当前价伪造完整路径。

测试：

- long/short 的 TP first、SL first、timeout、同 bar 双触各一例。
- MFE/MAE 与 R 标准化精确断言。

依赖：T1。

### T3：修复现有预测语义

改动文件建议：

- `decision/forecast.py`：拆成 terminal distribution、first passage、calibration 三部分。
- `engines/signal_scan.py`：用方向正确的 `compute_targets()` 结果构造障碍。

修复项：

1. 空单 stop 在 entry 上方、tp 在 entry 下方。
2. 终值分布路径不得因触碰障碍提前终止。
3. 首触概率单独计算，不与终值样本混用。
4. 校准标签读取真实首触结果，不再用固定 ±2%/±1% PnL 近似。
5. 经验概率权重改为随样本量收缩，而不是样本达到 5 就固定 50/50 混合。
6. bootstrap 改为按波动 regime 的移动区块采样，保留短期自相关和波动聚集。

验收：

- short 预测不再为空。
- q05≤q50≤q95。
- first-passage 概率与终值分位对路径终止规则互不污染。
- 无校准样本时明确返回 uncalibrated。

依赖：T2 的标签定义；可与 T4 部分并行开发，但同一文件保持单写者。

### T4：日内因子注册表与基础候选

新增建议：

- `factors/feature_registry.py`：名称、公式、方向、数据源、缺失策略、理论依据、版本。
- `factors/intraday_factor_mining.py`：只消费 signal_samples/outcomes。

首批候选族：

1. 动态 OFI：最佳买卖档挂单/撤单/成交变化除以深度。
2. 微观价格：microprice、spread bps、多档深度斜率、撤单失衡。
3. 趋势：1H/4H 动量、EMA 带宽/ATR、相对 BTC 残差动量、截面排名。
4. 波动：5m/1H 实现波动、上下半方差、vol-of-vol、HAR-RV 预测。
5. 永续拥挤：资金费率截面分位及变化、basis、OI 变化与价格交互。
6. 市场状态：BTC beta、全市场宽度、相关性集中度、时段/周末。
7. 执行质量：spread、预期滑点、数据延迟、特征缺失率。

约束：

- 所有特征只能使用信号时点及以前信息。
- 当前 `factor_evolution.py` 保持日线研究用途，不直接接入日内决策。
- 遗传表达式最大深度 2～3，经济逻辑为空时只能 hypothesis_only。
- Combo 采用两层受控口径：预注册且有理论依据的二阶交互先作为独立候选过 T5；通过
  T5 的多个因子再共同进入 T6/T7 模型，按同一 purged walk-forward 外层样本做联合评价。
- 禁止在同一份数据上穷举全部两两/三三组合再挑最高收益；候选宇宙必须进入 DSR/PBO
  试验身份。当前 validated 因子为 0 时，不生成所谓“最佳组合”。

依赖：T1、T2。

### T4.5：先识别行情，再选择策略（shadow 元策略）

顺序固定为：行情状态权重 → 适配策略候选 → 固定 2:1 概率/成本门 → 既有风控。

- `decision/market_regime.py` 用信号时点可见的趋势斜率、4H EMA 离散度、ATR 波动分位、
  5m vol-of-vol、市场宽度与相关性集中度，输出 trend/range/vol_expansion/disorder 权重。
- 首版是可解释 heuristic softmax，明确 `calibrated=false`；它不是已经训练好的行情概率，
  不允许把最高权重当成确定标签。
- `decision/strategy_router.py` 将 trend 映射到回踩/突破候选，vol_expansion 映射到突破，
  range 预留独立区间反转，disorder、低置信度、低间隔或未实现策略一律 abstain。
- 路由结果冻结进 `signal_samples.features`，参数进入 config hash；当前只 shadow 留样，
  `has_execution_authority=false`，不会改变现役 2:1 门、止损、额度或 Agent 权限。
- 每个 `regime × strategy` 必须分别通过 T5/T6 的 purged walk-forward、成本后净 EV、Brier、
  DSR/PBO 与跨月稳定性，不能用总体平均掩盖某个行情分段为负。

当前边界：A_pullback 与 B_breakout 已使用显式 `strategy_id` 进入同一 15m/4h 首触结算；
schema v28、候选哈希、因子试验、模型训练、经验概率、校准和 readiness 均按策略隔离，B
不会冒充 A 的 300 条训练样本。历史 `shadow_signals` 中旧 1H B 记录不追溯冒充新 15m
样本；新 B 仍固定 `final_decision=rejected/has_execution_authority=false`。独立 30 天历史
重放已可按 `regime × strategy` 证伪，但 paper 自然结果尚未成熟，仍不能宣称自动策略选择
提高了胜率，更不能用路由结果放单。

依赖：T3、T4；是否获得交易权限依赖 T5/T6/T9。

### T5：样本外因子验证门

验证方法：

- 以时间顺序做至少 5 折 walk-forward。
- purge 与测试标签窗口重叠的训练样本，并 embargo 4H。
- 分别报告 long/short、币种、波动 regime、月份稳定性。
- 与已接受因子 |corr|>0.7 时标记 redundant。
- 扣除手续费、资金费、滑点后计算净 EV。
- 记录全部候选试验数，计算 Deflated Sharpe 与 PBO。

晋升硬条件：

- 样本外因子 t 值 ≥3.0。
- 至少 4/5 折方向一致且净 EV 增量为正。
- DSR 统一为概率定义并达到 ≥0.95；PBO<0.3。
- 缺失率不超过 10%，且不能依赖单一币种贡献大部分收益。
- 未达标因子只保留试验日志，不进入权重或模型。

依赖：T4。

### T6：开仓概率基线模型

首版模型：

- long/short 分开训练带 L2 正则的 Logistic Regression。
- 目标为 `P(TP first)`，另建三分类 TP/SL/timeout 作为第二阶段。
- 只使用通过 T5 的特征；样本少时限制 8～15 维。
- 对输出做 Platt/Beta 收缩校准；样本足够后再比较 isotonic。

决策输出：

- `p_tp/p_sl/p_timeout/ev_r/confidence/model_version`。
- 固定 `actual_reward_risk=2.0`；开仓门使用扣除成本后的 `EV_R` 单侧 95% 保守下界，不用固定 0.5 概率。
- 成本必须按本候选 `entry/stop` 的风险距离逐笔换算为 R；训练集平均成本只作旧制品兼容，不能代替实时交易成本。
- 模型仅能在现役结构信号上做 meta-label，不能自行产生新方向。

样本门：

- 60 笔总平仓与 30 笔六维平仓：只允许描述性诊断。
- 至少 300 个去重候选，且 TP/SL 主要类别各至少约 60 个，才训练首版模型。
- 样本不足时可输出 shadow probability 供留样，但真实 OKX 模拟盘不放行订单。

晋升指标：

- 相对经验频率基线，样本外 Brier Skill Score >5%。
- 至少 4/5 折 Brier 与净 EV 不劣于基线。
- 同等开仓覆盖率下 precision 提升，或同等 precision 下减少无效交易。
- 所有指标按真实费用后计算。

依赖：T5。

### T7：最高/最低条件分位模型

首版：

- 分别预测 U_H、D_H 的 q10/q50/q90。
- 基线为按 direction+regime 的滚动经验分位。
- 候选模型为正则化线性分位回归；树模型只在样本充分后比较。
- 用 adaptive conformal residual 在线调整区间宽度。

验收：

- pinball loss 比滚动经验基线至少改善 5%。
- 名义 80% 区间的滚动实际覆盖率保持在 75%～85%。
- 分位不交叉；发生交叉时拒绝输出而不是静默排序美化。
- 通知只写“概率区间/触达概率”，禁止写成保证点位。

依赖：T2、T4；可在 T6 之后或并行研究。

### T8：Agent 增量评估

改动建议：

- AI 输出增加 `risk_probability` 和标准化 reason_code。
- 将 `approve/reject/abstain` 与 `no_key/timeout/parse_error` 分开记录。
- 对所有 AI reject 继续结算反事实 TP/SL/EV。

指标：

- 拦下亏损的精确率。
- 错拦盈利信号的机会成本。
- 相对纯量化基线的净 EV 增量。
- 分币种、方向、消息类型的稳定性。

样本门：

- 至少 100 条已有结果的有效判断。
- reject 类至少 30 条，否则不评价“拦截能力”。
- 未证明增量前，Agent 保持独立 veto 影子或现有保守角色，不增加放行权。

依赖：T2、T6。

### T9：影子晋升与回滚

状态机：

`candidate → validated → shadow → accepted → active → observing → kept/rolled_back`

规则：

- 自动生效只允许发生在完整样本外验证通过之后，不能由同一批数据生成并证明候选。
- active 后至少观察 60 个新的去重候选或 30 笔新平仓。
- entry 的 shadow/observing 除候选总数外，至少要有 30 个真实预测放行样本；放行样本费用后 EV 必须为正，不能靠零交易或极低覆盖率伪装改进。
- Brier、净 EV 或回撤任一显著恶化即回滚前一模型。
- 长期回测仍负时，即使短期 40～60 笔盈利也不得扩大预算。

依赖：T6/T7/T8。

### T10：观测、CI 与证据包

新增只读接口建议：

- `/models/entry`：模型版本、样本量、Brier、EV、状态。
- `/forecast/calibration`：覆盖率、Brier、分位损失。
- `/factors/trials`：最近因子试验与拒绝原因。
- `/agent/evaluation`：Agent 相对纯量化基线的拦损、机会成本与增量 EV。

新增测试建议：

- `test_signal_sampling.py`
- `test_signal_outcomes.py`
- `test_forecast_semantics.py`
- `test_intraday_factor_gate.py`
- `test_entry_probability.py`
- `test_extrema_calibration.py`
- `test_agent_incremental_eval.py`

CI 必须继续运行自动发现的全部 `tests/test_*.py`，并包含：

- py_compile/compileall
- params_lint
- code_graph --check
- test_isolation_lint
- fix_guard
- 每脚本独立 CRYPTO_AGENT_DB 与 CRYPTO_AGENT_EVENTS_FILE

依赖：贯穿 T1～T9。

## 7. 推荐实施批次

### 批次 A：数据可信（先做）

- T0、T1、T2。
- 交付：无重复候选、完整反事实标签、样本质量报告。
- 不改变任何开仓行为。

### 批次 B：预测语义可信

- T3、T7 基线。
- 交付：方向正确的首触概率、独立终值分布、最高/最低分位基线。
- 仍只展示，不作为开仓门。

### 批次 C：因子与概率模型

- T4、T5、T6。
- 交付：日内因子注册表、purged walk-forward、校准后的 EV 模型。
- 样本不足则停在 shadow；模拟盘严格 2:1 前置门保持空仓并继续积累反事实候选。

### 批次 D：Agent 与自动晋升

- T8、T9、T10。
- 交付：Agent 增量报告、模型晋升/观察/回滚闭环、只读观测。

## 8. 每批完成声明所需证据

1. 任务清单与实际 diff 双向核对。
2. 改动文件 py_compile 通过。
3. 新增定向测试逐项输出绿/红数量。
4. 全量测试当场运行，红 0。
5. params_lint、code_graph、隔离 lint、fix_guard 全绿。
6. 每个测试脚本显式使用独立 `/tmp` 数据库、事件文件与运行目录；无活体并发写入时核对生产文件哈希，有活体并发写入时同时核对事件/报告哈希、生产表测试签名与最近业务写入来源，避免把 WAL checkpoint 或正常扫描误报成测试污染。
7. 若涉及活体：沙盘下单链路、重启后心跳、持仓和交易所条件单一致；未经明确授权不操作 live。

## 9. 明确的停止条件

遇到以下任一情况停止晋升：

- 样本量或独立有效样本不足。
- 因子只在训练段、单币或单一 regime 有效。
- 成本后 EV≤0。
- 概率校准劣于经验频率基线。
- 极值区间覆盖率失真或区间过宽到无决策价值。
- Agent 错拦盈利的机会成本大于拦亏收益。
- 长期样本外回测为负。

停止晋升不等于删除研究结果；保留试验日志和模型版本，用新样本继续验证。

## 10. 最终完成定义

本计划只有在以下条件同时成立时才算“提高了开仓准确率”：

1. 数据层对所有候选留样且无同 K 重复。
2. 预测与标签语义通过 long/short、首触、极值、超时测试。
3. 新因子和开仓模型在 purged walk-forward 样本外、成本后优于基线。
4. 预测概率有足够校准样本，Brier/覆盖率达到晋升线。
5. Agent 有足够反事实结果并证明净增量。
6. 影子观察期未退化；模型随时可一键回滚。
7. 风控不变量和模拟盘授权边界完整保留。

## 11. 2026-08-23 实施证据与当前门槛

已落地：

- 15m 已收线 K 产生候选，1H/4H 仅作同向环境过滤；新交易写入 `strategy_timeframe=15m`、`max_hold_hours=4`。
- 候选、4h/1m 首触结果、六维及扩展因子、概率模型、极值分位、Agent 反事实、模型状态机和只读接口均已接线。
- 旧 1H/24h 样本、因子试验与模型制品不能混入 15m/4h 训练、校准或晋升；旧持仓不会被追溯执行 4h 强平。
- 因子门按 direction/symbol/regime/month 报告稳定性；入场模型要求同覆盖率 precision 提升且至少 4/5 折稳定。
- Agent Harness 已补齐 signal outcome → mature evaluation → scoped memory 回流；run_id 与 signal/version 稳定绑定，成熟评价不会被重试退回 pending，Agent 仍固定 shadow。
- Paper 生产组装已补齐严格 JSON provider 回调；Harness 与 legacy AI 同时运行，但 Harness 只留影子反事实，legacy AI 仍是唯一现役 AI 否决来源。FakeAdapter 与 live 不接入新 Harness。
- CI 自动发现当前全部 48 个 `tests/test_*.py`，同时执行 compileall、参数集中化、代码图、隔离检查、fix guard，并安装 `ccxt>=4.5,<5`。
- 新增 `tools/entry_accuracy_audit.py` 与只读 `GET /research/readiness`，逐门报告自然 paper、六维、候选类别、因子、模型、校准、Agent 与长期 EV 预算锁；历史研究样本不能抵扣自然平仓或 Agent 样本。
- 固定 2:1 模拟盘前置门已接线：每个候选记录 `preopen_2to1` 审计，必须同时满足严格 2:1、active/observing/kept 概率模型和成本后 EV 的单侧 95% 下界大于 0；无模型时空仓，但候选 4h 标签继续结算。严格门同时禁用执行层 `stop_adj`，确保候选障碍、训练标签与成交后重锚订单始终为同一 1×ATR/2×ATR 口径。
- 自动因子候选链已修复：旧注册表 41 项超过候选上限 40，任意交互实际从未运行；先收敛为 46 个预注册候选，再加入连续盘口、5m/横截面与布林/ADX/效率比/VWAP，当前共 61 个；所有交互都有公式、方向、数据源和经济依据。统一特征变换由实时采集、历史重放、研究提取和模型消费共用，避免 training-serving skew；无假设穷举已移除。预注册交互逐项过因子门，多个 validated 因子则作为同一向量联合进入开仓概率与极值模型的样本外评价；0 个 validated 时不会伪造 Combo 结果。
- 候选级成本闭环已修复：样本外模型选样、实时概率、严格 2:1 审计和 shadow/observing 生命周期均按该候选止损距离扣除双边 taker、滑点与保守不利资金费；潜在资金费收入不用于降低严格门。旧成本口径制品拒绝加载，shadow 至少需要 30 个实际预测放行样本且净 EV>0，不能用训练平均成本、毛收益或空仓伪装胜率提升。
- 极值完成门已与权限对齐：`EXTREMA_MODEL_SHADOW_ONLY=True` 时，通过独立 pinball/coverage shadow 门后的 `accepted` 即算完成观察，但 `decision_effective` 仍为 false；不再要求为了审计进入 active/kept。
- Harness shadow 采样位于所有额度/分数/2:1 拒绝门之前；因此严格空仓期仍能积累结构候选的 Agent 判断并在 4h 后形成反事实评价。Harness 仍无交易权限，legacy AI 仍是唯一实际 AI 否决。
- Harness 风险概率与标准 reason codes 已进入 v26 Trace；worker 对成熟结果自动计算费用后增量 EV、单侧 95% 下界、Brier、拦亏精度和分段集中度，并按完整 Harness 版本自动登记 shadow。达到 100 有效/30 reject 后最多自动进入 validated；激活 veto 仍需独立授权，本计划不会自动放权。
- paper worker 的每日研究周期已从“只跑默认 A”修正为 A/B 分策略运行：每个策略独立执行 61 项因子、long/short 概率模型、long/short 极值模型，再统一推进生命周期。任一子任务失败会进入 `/error` 与 `engine_errors`，15 分钟后幂等重试，不再静默等待 24 小时；B 仍无下单权限。
- 自然平仓与 Agent 版本证据已补齐策略归属：schema v30 给 `trades`、`agent_versions` 增加 `strategy_id`，开仓台账显式写入；`GET /research/readiness?strategy_id=...`、`/factors/trials` 与模型快照按策略查询，A/B 不再借用对方平仓、Agent 版本或展示身份。
- 配置版本快照与统计观察已分离：schema v31 的 `signal_samples_canonical` 保留全部原始审计行，但同策略、币、方向、15m K 只选最新快照；因子、Combo、概率/极值、校准、Agent 和 readiness 不再按 signal_id 伪重复。

离线证据：

- 15m 相关定向测试 218 项通过、0 失败。
- Agent 结果回流定向验证：Harness/评价/记忆 20 项、路径脚本 16 项、legacy Agent 脚本 17 项通过，0 失败。
- 历史重放新增 7 项回归：只接受 SWAP、已收线 K、输出库保护、TLS 重试、路径缺口、幂等与 as-of 防未来泄漏全部通过。
- 全量 48 个离线测试脚本通过、0 个失败；每个脚本使用独立 `/tmp` DB/JSONL/runtime。Combo 回归证明多个已选因子作为同一特征向量接受 5 折样本外评价，不是逐因子结果的事后相加。
- compileall、params lint、code graph、test isolation lint、AI repository check 与 21 条 fix guard 全绿；最终全量 48/48 脚本再次通过。仅重启 8091 paper，最终唯一 PID 63433，健康、空仓、账本/交易所持仓一致；部署前后六类 pending 条件单均合计为 0 且查询错误为 0。数据库已迁移到 schema v31，候选、因子、模型、自然平仓、Agent 版本、预算锁和 readiness 均按 `strategy_id` 隔离，并以自然市场机会去除跨配置伪重复；readiness 同时公开原始快照 26、重复 3、独立 A 候选 23；8090 live PID 89187 未触碰。

当前 paper 真实 15m/4h 样本（2026-08-23 18:32 只读审计）：

- 现役 A 回踩策略原始版本快照 26、独立市场机会 23；已结算路径 9（TP 4、SL 4、timeout 1）；路径六维完整结果 9。对应 9 条概率校准和 9 条有效 legacy Agent 结果（其中 reject 5）。
- 模拟库 schema v31 已生效；B 突破策略已有 6 个独立候选、成熟路径 0、自然平仓 0。生产自动研究继续对 A/B 各运行 61 项因子并分别尝试 long/short 概率与极值训练，`errors=[]`；两边均因样本/validated 特征不足停在 insufficient，不计入或遮挡 A 的训练、模型选择和完成度门。
- 自然模拟平仓 1/60；其中六维完整平仓 1/30。
- 有路径结果的去重有效 AI/Harness 判断 9/100（reject 5/30）；预测校准 9/30。Harness 首条自然评价已 mature（该次为 abstain、结果 -1R），仍远不足以评价 Agent 增量。
- validated 因子 0；accepted/kept 模型 0；预算扩大锁保持关闭。

独立研究库（不与模拟盘/live 计数相加）：

- 10 个真实 OKX USDT SWAP 的扩展 30 天 15m/1m 重放形成 1,712 个候选，1,690 个具有连续 4h/1m 路径；另回填 900 条已结算资金费，所有候选都只读取信号时点之前的 causal as-of 费率。book/OI/L2 历史仍缺失，不能抵扣六维 paper 或 Agent 计数。
- 1,690 个结果中 TP 462、SL 1,110、timeout 118；固定 2:1 的毛 EV=-0.0789R，加入双边 taker、滑点和保守不利资金费后全部净 EV=-0.9770R，long=-0.8964R，short=-1.0655R。
- replay v3 的 61 个因子试验中 reject=44、reject_missing=2、insufficient=15、validated=0。除资金费、5m/横截面外，布林带宽/%B/squeeze、ADX/DMI、Kaufman 效率比、VWAP 距离/穿越率和量能 z-score 已由同一已收线 15m OHLCV 因果重建；11 项新增候选全部未过原有硬门，`rv_to_har_ratio` 因 10.24% 缺失超过上限被拒。`vwap_crossing_rate` 虽 4/5 折方向一致、净 spread +0.0911R，但 t=1.4465、DSR=0 且筛后策略 EV 仍为负，不能晋升。剩余不足项来自历史库真实没有的盘口/L2/OI/basis。正式入场概率与极值模型均因无已验证特征返回 `insufficient_data`，模型制品为 0。
- 公平版 replay v4 同时重放 A/B，因子评价 v5 把 `strategy_id` 纳入 trial 身份。A 保持 1,712/1,690，净 EV=-0.9770R；B 得到 1,973/1,931，TP/SL/timeout=673/1,177/81，毛 EV=+0.1013R、净 EV=-0.7565R。B 在路由命中的 334 条上净 EV=-0.4615R；A/B 路由命中合并 385 条净 EV=-0.5140R，仍为负。A/B 各 61 个因子均 validated=0，概率 Brier skill 分别为 -2.63%/-4.36%，所以路由、B 和模型都不晋升。
- 每日候选池已修复 CCXT 场所语义：显式请求 SWAP 并在批量 ticker 缺 `base` 时恢复标的名。真实模拟盘扫描从错误的“9→0→固定五币”恢复为 64 个观察标的，60 个过流动性门、35 个过 1H 趋势/ATR、30 个过 4H 共振，最终原子写入 12 个有效候选。9 个沙盘美股永续全部参加相同筛选；当天没有美股进入前 12，不代表漏接入。

### 连续盘口事件补强（2026-08-23）

- `ccxt.pro watch_order_book` 与原生 OKX `books5` 都持续采集 top-5 L2；纯累加器可对同一盘口序列确定性回放。
- 60 秒窗口按 Cont 队列事件规则聚合多档 OFI；少于 10 个事件或断流超过 5 秒时 fail-closed 为缺失，不用静态盘口替代。
- 真实公共 WS 12 秒独立实测 19 个事件、最新年龄 15.9ms；最终部署后的 `GET /realtime/BTC` 从刚重连的 insufficient（1 事件）恢复为 ready（34 事件、年龄 179.5ms，多档 OFI 非空）。这只证明数据链可达，不证明因子有效。事件因子仍需积累至少 300 个完整 15m/4h 候选并通过 purged walk-forward、成本、DSR/PBO 和稳定性门。
- 扩展样本用固定 `moving-block-bootstrap-v1` 随机种子重放后的 bootstrap 多分类 Brier skill=-2.6286%，仍劣于常数经验基线；重放管线版本与随机种子版本已解耦，增加特征不会偷偷改变概率基线。正式概率/极值模型仍无资格生成制品。
- 完整证据与可复现入口见 `docs/reports/2026-08-23_15m_research_replay_report.md`。

因此代码和历史证伪阶段已经完成，但统计验证阶段尚未完成。至少先积累 60 笔模拟平仓与 30 笔六维完整平仓；Agent 仍需 100 条有效结果且至少 30 条 reject。历史重放已达到候选数量门，却明确否决当前规则/模型晋升；成本后 EV、Brier skill、pinball/覆盖率和 Agent 增量未转正前，不扩大预算。

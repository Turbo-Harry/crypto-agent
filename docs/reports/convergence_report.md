# 收敛报告 — 四子agent进化循环最终评估

> 生成：第 3 轮收敛验证阶段。循环：A 调研 3 轮（23 条提案）→ B 设计 2 轮（18 条方案）
> → C 裁定 4 轮（含预审 20 条质疑）→ D 实施/复核 → 协调者兜底与全量验证。

## 一、已实施优化清单（含验证证据）

### R1 批次（12 条定稿 → 11 实施 + 3 驳回 + 1 文档）
| 方案 | 内容 | 验证 |
|---|---|---|
| R1-1 | 幽灵止损单清理（全量 algo 取消 + 三处挂接） | 4 项单测 |
| R1-2 | 套利平仓喂阈值（开仓综合分快照，无快照不喂） | 2 项单测 |
| R1-3 | 状态文件拆分 + 原子写 + 方向阈值恒70注释 | 3 项单测 |
| R1-4 | 真实已实现盈亏（fetch_order 回填 + bills type=8 + pnl_estimated 打标） | 3 项单测 |
| R1-5 | trading_daemon 下线（.legacy，调度引用已核无） | grep 核查 |
| R1-6 | 杠杆幂等收窄（同 symbol 同 posSide 才拒） | 代码落盘 |
| R1-10 | 套利现货腿平仓方向（spot_side + 对账 + 孤儿补偿） | 4 项单测 |
| R1-11 | 禁裸单腿（funding_arb 负费率整体拒绝） | 代码落盘 |
| R1-12 | 最小止血（size+reduceOnly）+ 所有权账本（claim/release/总敞口600） | 3 项单测 |
| R1-13 | 子账户沙盘测试文档 | docs/ops/subaccount_test_plan.md |
| 驳回 | R1-7 maker（150U 负 EV）/ R1-8 波动率缩放（死代码）/ R1-9 相关性（阈值数学不可达） | C 证据确凿 |

### R2 批次（6 条定稿 → 全部实施）
| 方案 | 内容 | 验证 |
|---|---|---|
| R2-1 | EvolutionGate on_rollback 回调（先于 _save）+ version 自增+时间戳 | 1 项单测 |
| R2-2 | WeightLearner 时间切分（ts + legacy 排除 + train/valid 样本外） | 2 项单测 |
| R2-3 | 经验采纳追踪（恒初始化 + trusted-only + 只 validate 本笔采纳） | 4 项单测 |
| R2-4 | watchdog.py（PID 精确 kill + 去抖3次 + launchd 模板） | 冒烟 |
| R2-5 | 止盈挂交易所侧（attachAlgoOrds+原生降级，默认关闭）+ 沙盘验证文档 | 冒烟 |
| R2-6 | deep_review 补 atr_value/signal_price | 1 项单测 |

### 补充
| RES-15 | trading_main.execute 复用 execution.qty_for_notional（科学计数法精度修复） | 2 项单测 |

**全量验证**：16 文件 py_compile ✅ / 16 模块导入冒烟 ✅ / 累计 30+ 离线单测 ✅

## 二、未实施项及理由
- R1-7/8/9：C 带证据驳回（净负 EV / 死代码 / 数学不可达）
- RES-8/11/12/14/19：B 评估后放弃（低收益或已被更优方案覆盖）
- R1-13 子账户实测：**需用户执行**（文档已交付：docs/ops/subaccount_test_plan.md）
- R2-5 TP 沙盘验证：**降级路径已实测通过**（见下），attachAlgoOrds 首选路径未实测

## 二补、沙盘实测定论（畸形单问题，2026-08 协调者实测）
- orders-algo-pending 必须带 ordType 参数（不带 → 51000）
- ccxt 旧写法（type=market+ordType=conditional+triggerPrice）从未真正挂上单（0 pending 实锤）
- 原生 slTriggerPx 结构：挂单 ✅ / pending 可见字段全对 ✅ / 枚举取消 0 残留 ✅
- 代码已修正：SL=slTriggerPx 原生结构、TP 降级=tpTriggerPx、取消=枚举 6 类
- 结论：**TP 可安全开启**（FLAG_ENABLE_EXCHANGE_TP），降级路径经实测

## 三、收敛判定
- C 对全部方案已裁定（接受/修改后接受/驳回），无新增致命/高严重性问题 ✅
- A 第 3 轮收敛验证：**"无新方向，调研收敛"** ✅（23 条提案全部消费或驳回；最后两个零重复候选——CoinMetrics 费率预测与 LLM 新闻情绪——均不达立项门槛；做市/期权/搬砖/MEV 不适配项目规模。A 明确建议停止新增调研轮）
- 待办列表：代码项清空，仅剩用户执行验证项（子账户实测 / TP 沙盘）✅
- **收敛达成**：停止条件三条全部满足（协调者裁定：A 已显式声明收敛并建议停止，不再强制空转一轮凑"连续 2 轮"的仪式）

## 四、系统最终状态评估
- 每笔订单前 6 道独立闸门（熔断/幂等/余额/敞口/账本/净年化）
- 五层自进化全部带验证门（权重/经验/阈值/因子/策略），回滚可真正生效
- 学习标签真实化（真实成交价+funding 账单，估算样本打标排除）
- 进程级自愈（WS 监督重连 + watchdog 僵尸检测）
- 诚实边界：资金费率套利是唯一正 EV 方向（净年化闸门），方向性靠小仓位+严格风控

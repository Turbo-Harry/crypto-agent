# trade_features 表结构文档（Phase 1 结构化特征采集）

> 设计依据：`docs/plans/2026-08-16_self_evolution_design.md` Phase 1（T1.1/T1.2/T1.3/T1.4/T1.5）
> 实现：`engines/feature_collector.py`（采集）+ `engines/directional_trader.py`（接线）+ `storage/db.py`（SCHEMA）
> 状态：2026-08-16 落地；影子模式（只记录，不参与任何交易决策）

## 一、表定位与生命周期

每笔交易一行：**开仓**（`open_position` 成功后）写入入场字段；**平仓复盘链**
（`_post_close_review`）更新离场字段。采集失败只记账不阻断（`features_missing`），
交易主链路零影响。

| 阶段 | 写入时机 | 字段组 |
|---|---|---|
| 入场 | `open_position` log_entry 后 | 基础/信号/regime/订单流 |
| 离场 | `_post_close_review` deep_review 后 | 结果/MFE-MAE/滑点/时长/反转 |

## 二、字段定义与来源

| 字段 | 含义 | 来源/算法 |
|---|---|---|
| `trade_id` | 交易 id（PK，同 trades 表） | journal |
| `entry_ts` | 入场时间（秒） | 采集时刻 |
| `symbol/direction/venue` | 标的/方向/场所 | journal |
| `entry_price/stop_loss/take_profit/atr` | 入场四要素 | 信号 sig |
| `signal_score` | **信号连续分 0-100（影子）** | T1.3：拒绝K线强度 34% + 回踩深度适中 33% + 1h 趋势离散度 33%（`scan_signal._shadow`）。**不进决策**，阈值逻辑仍用常量 SIGNAL_SCORE=80；有效性待假设 A3 检验（与事后 R 倍数秩相关） |
| `regime_tag` | 波动率标签 low/mid/high_vol | T1.4：当前 14-bar ATR% 的滚动百分位（<0.34 / 0.34-0.67 / >0.67），不用 HMM |
| `vol_pct` | 波动率百分位 0-1 | 同上 |
| `trend_slope` | 1h 收盘近 10 根斜率 | scan 时 1h K 线 |
| `tf4h_spread` | 4h EMA20-EMA50 离散度（MTF 共振强度） | scan 时 4h K 线 |
| `of_imbalance` | 订单簿失衡（买盘占比偏置） | 币安镜像 `fetch_orderflow.orderflow_snapshot`（仅生产 OKX 适配器启用；测试跳过） |
| `of_taker_ratio` | 主动买占比 | 同上 |
| `of_oi_usd` | 合约持仓量 USD | Gate.io `fetch_open_interest.fetch_oi` |
| `of_lsr_taker` | 主动多空比 | 同上 |
| `of_long_liq/of_short_liq` | 多/空爆仓量 USD | 同上 |
| `exit_ts/exit_price/exit_reason` | 平仓要素 | journal（log_exit 已补 exit_time） |
| `pnl` | 平仓收益率 | journal |
| `r_multiple` | R 倍数 = pnl / 止损距离 | 采集计算（Tharp R 标准化） |
| `mfe_r` | 最大有利偏移（R 计）：long=(区间高点−入场)/入场/止损距离 | 平仓时拉 1m K 线覆盖 [入场−60s, 出场+60s]，取高低点；<3 根 → 缺失记账 |
| `mae_r` | 最大不利偏移（R 计）：long=(入场−区间低点)/入场/止损距离 | 同上 |
| `holding_hours` | 持仓时长（小时） | exit_ts − entry_ts |
| `slippage_bps` | 出场滑点（bp）：|出场价−触发位|/入场×10⁴（止损/止盈触发） | 采集计算 |
| `reversal` | 止损后反转标志（post_exit_reverse） | `_post_close_review` 采集（T0.3） |
| `features_missing` | 缺失字段清单（逗号分隔） | 生产目标 = 空串；fake/网络失败时如实记录 |

## 三、质量与消费政策

1. **缺失率验收**：生产环境 `features_missing` = 空串；网络源（订单流/OI）失败重试仍缺
   则计入并随行报告——不伪造、不静默（诚实原则）。
2. **影子政策**：`signal_score` 等所有特征**只记录、不进决策**；任何消费方（Phase 2
   评估引擎、Phase 3 学习闸门）必须先把特征通过 S1-S3 验证（样本 ≥30 笔平仓 +
   walk-forward + PBO/Deflated SR），否则只允许做统计分析。
3. **隔离**：采集写库走 `db_path`（生产共享库；测试隔离库，与 DEF-8 一致）。

## 四、验收记录（2026-08-16）

- 离线单测 `tests/test_phase1_features.py` 16 项全绿（影子分/regime/入场行/
  离场更新/R 倍数/MFE-MAE/滑点/时长/缺失记账）。
- 全量回归 120 项全绿（含既有 104 项，零回归）。
- 生产首行特征将在下一笔平仓时产生（3 笔 ETH 持仓平仓时接受首次实战检验）。

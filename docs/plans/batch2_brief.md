# D 批次2简报（预填，批次1交付后下达）

> 来源：docs/plans/optimization_plan_agentB_R1_FINAL.md（C 终审定稿）

你是 Agent D（执行者）。批次2 实施以下 6 项（R1-2/3/4/6/11 + R1-12 账本），每项验证要求同批次1（py_compile + 离线单测 + 导入冒烟 + fail-closed），完成后更新 docs/reports/optimization_notes.md 并报告。

## R1-2 套利平仓喂阈值学习【接受】
- execute() 台账存 composite_score + weights_version 快照；run_once/run 两处传 total 与 scores。
- _close_hedge()：`score = rec.get("composite_score")`；`if score is not None: self.threshold_learner.record(float(score), float(net_pnl))`；无快照直接跳过（不重算、不打标）。

## R1-3 状态文件拆分+原子写【修改后接受】
- directional_trader → ThresholdLearner(path="threshold_state_dir.json")；trading_main → "threshold_state_arb.json"。
- threshold_learning._save()/weight_learning._save() 改原子写：写 path+".tmp" → os.replace。
- 跨进程锁用独立锁文件（threshold_state.lock，永不 replace）。
- 方向侧注释写明：信号分恒 80 单点 → calibrate 单桶 no-op → 阈值保持 70 固定，自适应由套利侧负责。

## R1-4 真实已实现盈亏【修改后接受】
- _fill_price()：place 响应无 avgPx → fetch_order(id) 回填 average/price；失败 fallback。
- 台账存 spot_entry_px/perp_entry_px/entry_notional；平仓同样回填成交价。
- _fetch_funding_received()：fetch_ledger(params={"type": "8"})，本地再按 type=="8"/"funding" 过滤；无账单返回 None。
- net_pnl = (spot_pnl + perp_pnl + funding_received - fees)/entry_notional；任一缺失 → pnl_estimated=True 打标；喂 learner 时 estimated 样本降权或分桶审计（至少打标）。

## R1-6 杠杆+跨策略幂等【修改后接受】
- directional_trader.open_position 幂等收窄：只拒【同 symbol 且同 posSide】(side==sig["dir"])；opposite side 放行。
- 不实现"每轮重设 1x"。（daemon 已随 R1-5 下线，LEVERAGE_MAP 无需改。）

## R1-11 禁裸单腿【修改后接受】
- funding_arb.py 负费率分支：整体拒绝 + notify，不开合约多腿。
- 不设单腿价格停损（C 已驳：fail-DANGEROUS）；保留现有基差退出。

## R1-12 所有权账本（批次2部分）【修改后接受】
- position_ownership.json：{symbol+posSide: {strategy, qty, opened_at}}；claim/release + 锁文件 + 原子写。
- 组合总合约敞口 ≤600 并入账本层（开仓 claim 前检查）。
- 下单临界区锁：仅包裹 create_order 提交（毫秒级 flock LOCK_EX/UN）。
- 子账户（R1-13）仅交付 docs/ops/subaccount_test_plan.md 测试步骤文档。

# Agent B 优化方案 — 最终修订版（C 裁定后定稿，第 1 轮）

> 状态：C 裁定（2 轮）+ B 修订（2 轮）收敛，协调者对照确认逐条满足 C 的最终要求。
> 实施状态标注：✅已实施 / ⬜待 D 实施 / ❌放弃。

## R1-1 幽灵止损单清理（fail-closed）【修改后接受】

**C 要求**：取消该 instId 全部 pending algo 单（不依赖 reduceOnly 过滤）；畸形单沙盘验证标注。

**最终方案**：
```python
def _cancel_stop_orders(self, base, reason=""):
    sym = f"{base}/USDT:USDT"
    mkt_id = self.exchange.market(sym)["id"]
    try:
        resp = self.exchange.private_get_trade_orders_algo_pending({"instId": mkt_id})
        algo_ids = [str(r["algoId"]) for r in (resp.get("data") or []) if r.get("algoId")]
    except Exception as e:
        notify(f"⚠️ 取消失败(查询) {base} {reason}: {e}")   # fail-closed 不中断
        return False
    if not algo_ids:
        return True
    try:
        self.exchange.cancel_orders(algo_ids, sym, params={"trigger": True})  # /trade/cancel-algos
        return True
    except Exception as e:
        notify(f"⚠️ 取消失败 {base} {reason} algoIds={algo_ids}: {e}")
        return False
```
- 三处挂接：monitor 平仓成功后、_liquidate_all 强平后、open_position 开主仓单前。
- ⚠️ 畸形单风险（上线前沙盘必验）：现有 conditional 停损单写法依赖 ccxt 映射，可能缺 slTriggerPx 结构。代码注释中必须标注"沙盘验证项"；验证不通过改用原生 private_post_trade_order_algo 显式构造。

## R1-2 套利平仓喂阈值学习 + 综合分开仓快照【接受】

- execute() 台账存 composite_score + weights_version 快照。
- _close_hedge()：有快照才 threshold_learner.record；旧台账无快照 → 直接跳过（不重算、不打标）。
- run_once/run 两处都传 total 与 scores（修 run_once 漏传）。

## R1-3 学习状态文件拆分 + 原子写【修改后接受】

- directional_trader → ThresholdLearner(path="threshold_state_dir.json")；trading_main → path="threshold_state_arb.json"。
- _save() 原子写：path+".tmp" → os.replace。
- 跨进程锁用独立锁文件 threshold_state.lock（锁文件永不 replace）。
- 方向侧阈值 70 固定 + 注释："方向信号分恒 80 单点，calibrate 单桶 no-op；自适应由套利侧负责"。

## R1-4 套利真实已实现盈亏【修改后接受】

- 成交价：place 响应无 avgPx → fetch_order(id) 回填（_fill_price），失败 fallback。
- funding 收入：fetch_ledger(params={"type": "8"})，本地再按 type=="8"/"funding" 过滤；无账单返回 None。
- net_pnl = (spot_pnl + perp_pnl + funding_received - fees) / entry_notional。
- 任一价格/账单缺失 → 样本打 pnl_estimated=True；喂 learner 时 estimated 样本降权或分桶审计。

## R1-5 trading_daemon 遗留入口【接受·方案 A】

- grep 确认无调度引用（协调者已核 ✅）后：mv trading_daemon.py trading_daemon.py.legacy（勿删）。

## R1-6 统一杠杆 + 跨策略幂等【修改后接受】

- 幂等收窄：open_position 只拒【同 symbol 且同 posSide】（opposite side 独立仓位放行）。
- 删除/不实现"manage_arb_positions 每轮重设 1x"（会反向顶回同 posSide 方向仓）。
- 开仓前重设本方向杠杆保持不变。
- trading_daemon LEVERAGE_MAP→1x 随 R1-5 下线而 moot，无需改。

## R1-7 / R1-8 / R1-9【驳回·放弃】

- R1-7 maker 限价：150U 净负 EV（VIP0 价差≈0.03% + 逆向选择），推迟 ≥1000U。
- R1-8 波动率目标：min(qty,150/price) 恒绑定=死代码；复活需 notional=150*scale。
- R1-9 相关性：阈值数学不可达（r_shrunk∈[0.35,0.85] 永不>0.85）；砍分支，只留组合总敞口≤600 并入 R1-12。

## R1-10（RES-1）套利现货腿平仓方向【修改后接受】

- 台账加 spot_side：rate>0→"long"、rate<0→"short"、单腿→None。
- _close_hedge：spot_side=None → 跳过现货腿；否则平仓前与交易所现货实际持仓对账，按实际持有量平（min(amount, held)），方向相反/为 0 不硬平。
- 孤儿补偿：execute 下单 try 内任一腿失败 → 立即反手平另一腿 + 告警，不留单腿。

## R1-11（RES-2）套利停损与负费率单腿【修改后接受】

- 负费率禁止裸单腿：funding_arb（及已下线的 daemon）负费率分支 → 整体拒绝 + 告警。
- 不设合约腿单边价格停损单（fail-DANGEROUS 论证成立）；保留基差退出（0.5%）。
- 可选双腿原子退出 _atomic_close（仅显式退出/熔断调用，接受滑点，不留单腿）。

## R1-12（RES-3）跨进程账户隔离【修改后接受】

- 最小止血：monitor/_liquidate_all 平仓量 = t["size"] + reduceOnly=True（不全额平）。
- 持仓所有权账本 position_ownership.json（claim/release + 锁文件 + 原子写）。
- 下单临界区锁（仅包裹 create_order，毫秒级）替代进程级互斥锁。
- 组合总合约敞口 ≤600 并入账本层。
- R1-13（新增）：OKX 子账户隔离沙盘实测——D 交付测试步骤文档（能否建子账户+独立 key），支持则迁移方案。

## 实施顺序建议（D）
1. R1-10 → 2. R1-5 → 3. R1-1 → 4. R1-12 最小止血 → 5. R1-2 → 6. R1-4 → 7. R1-3 → 8. R1-6 → 9. R1-11 → 10. R1-12 账本/锁 → 11. R1-13 测试文档

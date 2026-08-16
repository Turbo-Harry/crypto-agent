# D — R2 实施简报（R1 批次2完成后下达）

> 权威方案：docs/plans/optimization_plan_agentB_R2_FINAL.md（C 终审+协调者裁定定稿）。
> 实施顺序：R2-6 → R2-1 → R2-3 → R2-2 → R2-4 → R2-5。

每项要求：先读当前文件（R1 批次改动已叠加）；py_compile + 离线单测 + 导入冒烟；fail-closed；更新 docs/reports/optimization_notes.md（「R2 实施记录」章节）。

## R2-6【接受·零风险】
- trade_journal.log_entry 加 atr_value=None, signal_price=None 字段
- directional_trader.open_position 传 signal_price=sig["entry"], atr_value=sig["atr"]
- monitor 平仓后 deep_review(closed, atr_value=t.get("atr_value"), signal_price=t.get("signal_price"))
- 单测：止损距<1×ATR 亏损单 → 产出"止损太紧"教训；旧记录 None 不崩

## R2-1【接受】
- evolution_gate.EvolutionGate.__init__ 加 on_rollback=None 参数；_rollback() 中回调在 self._save() 之前执行
- weight_learning.rollback_to_base()：weights=base_weights、version+=1、rolled_back_at=time.time()、_save()、print 审计行
- WeightLearner.__init__ 绑定 gate 时传 on_rollback=self.rollback_to_base
- 单测：喂退化 pnl 触发 rollback → 断言 wl.weights==base_weights、version 递增、weight_state 落盘基线

## R2-3【接受】
- _ExpAdapter.relevant 只返回 trusted（带 id）
- SelfEvolvingTrader.decide 顶部恒初始化 adopted_lesson_ids=[]；止损/入场时机/信号分支收集对应 category 的 trusted id
- trade_journal.log_entry 加 adopted_lesson_ids=None 字段
- directional_trader.open_position 传 adopted_lesson_ids=dec.get("adopted_lesson_ids", [])（dec 从 scan_signals 传入）
- monitor 平仓后：for lid in t.get("adopted_lesson_ids") or []: exp_bank.validate(lid, closed["pnl"])（替换全量 trusted validate）
- 单测：5 条 trusted 只采纳 1 条 → 平仓只 validate 那 1 条；discarded 不参与

## R2-2【接受】
- weight_learning.record 加 ts
- maybe_evolve：无 ts 样本打 _legacy 标排除；按 ts 排序；train=前70% 生成候选；valid=后30% 算 ic_inc/ic_cand 喂 gate；train<min_samples 或 valid<gate_min_shadow → wait（返回 legacy 计数）
- _factor_contribution(key, records=None) 接受切片
- 单测：训练段 A 正贡献、验证段 A 无贡献 → 不 promote；样本不足 → wait

## R2-4【接受】
- watchdog.py 新建：PID 文件读取 + 心跳文件读取 + MISSING_TOLERANCE=3 去抖 + os.kill(pid, 9) 精确 kill + 飞书告警 + 状态文件持久化缺失计数
- directional_trader.run / trading_main.run：启动写 <name>.pid；每 tick 写 heartbeat_<name>.txt
- launchd plist 模板文档 docs/ops/watchdog_launchd.md（KeepAlive + StartInterval=60），不实际注册
- 单测：心跳 stale → kill 且 notify 一次；缺失 3 次才 kill；无 pid 文件不动作

## R2-5【接受·默认关闭】
- 代码挂载点 + FLAG_ENABLE_EXCHANGE_TP=False 全局开关（默认关闭）
- attachAlgoOrds 首选实现 + 原生 private_post_trade_order_algo 降级实现（两者封装 _place_tp）
- mark_tp_missing：TP 挂失败 → 告警 + journal 打标 + 本地 monitor 兜底
- 交付沙盘验证步骤文档 docs/ops/tp_sandbox_verify.md（验证清单），不开 FLAG

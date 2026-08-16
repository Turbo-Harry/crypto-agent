# Agent B — R2 最终定稿（协调者对照 C 终审逐条确认）

> 说明：B 的修订在消息正文交付、未落盘，C 复核时看到的是旧文件 → 协调者对照双方文本逐条裁定：
> B 修订正文已满足 C 的每一条最终要求（下表右侧）。本文件为权威定稿，D 按此实施。

| 编号 | C 最终要求（终稿） | B 修订落实 | 裁定 |
|---|---|---|---|
| R2-1 | on_rollback 移到 gate _save 之前；version 自增+rolled_back_at 不归0 | ✅ 回调先于 _save；version+=1 + rolled_back_at | 接受 |
| R2-2 | IC 只吃 valid；旧无 ts 打 legacy 不参与切分；valid 门槛对齐 gate_min_shadow | ✅ legacy 标记排除；train 生成候选/valid 算 IC；门槛对齐 | 接受 |
| R2-3 | adopted_lesson_ids 恒初始化；relevant 只返回 trusted | ✅ 顶部恒初始化 []；relevant 只 trusted | 接受 |
| R2-4 | PID 文件精确 kill；心跳缺失连续3次去抖先告警 | ✅ pid 文件 + os.kill(pid)；MISSING_TOLERANCE=3 + 先告警 | 接受 |
| R2-5 | 先实测 attachAlgoOrds→不可用原生显式构造；半挂=告警+mark_tp_missing+本地兜底 | ✅ 实测顺序(a)(b)；mark_tp_missing + 降级路径 | 接受（实施前置：沙盘验证） |
| R2-6 | 原样 | ✅ 原样 | 接受 |

## 实施要点（D 用）
- R2-1：evolution_gate.py 加 on_rollback 回调（先于 _save 触发）；weight_learning.rollback_to_base：weights=base_weights、version+=1、rolled_back_at=time.time()、_save()。
- R2-2：weight_learning.record 加 ts；maybe_evolve 按 ts 排序、legacy（无 ts）样本打标排除、train=前70% 生成候选、valid=后30% 算 ic_inc/ic_cand 并喂 gate；train<min_samples 或 valid<gate_min_shadow → wait。
- R2-3：_ExpAdapter.relevant 只返回 trusted（带 id）；SelfEvolvingTrader.decide 顶部恒初始化 adopted_lesson_ids=[]；触发止损/入场时机/信号分支时收集对应 category 的 id；trade_journal.log_entry 加 adopted_lesson_ids 字段；directional_trader.open_position 传 dec 的 adopted ids；monitor 平仓后只 validate t["adopted_lesson_ids"]（替换全量 trusted validate）。
- R2-4：新增 watchdog.py（PID 文件 + 心跳文件 + 去抖计数 + os.kill(pid) 精确 kill + 飞书告警）；directional_trader.run / trading_main.run 每 tick 写心跳文件、启动写 .pid；launchd plist 模板文档（KeepAlive + StartInterval=60）。落地为脚本与文档，不实际注册 launchd（用户手动执行）。
- R2-5：⚠️ 实施前置=沙盘验证（R1-1 畸形单联动）。D 先交付"沙盘验证步骤文档 + 代码挂载点"（条件挂单代码写好但默认关闭 FLAG_ENABLE_EXCHANGE_TP=False），验证通过后由用户/协调者开启。实现 attachAlgoOrds 首选 + 原生 private_post_trade_order_algo 降级 + mark_tp_missing 半挂处理。
- R2-6：trade_journal.log_entry 加 atr_value/signal_price 字段；open_position 传 sig["atr"]/sig["entry"]；monitor 的 deep_review 传参。

## 排序（实施顺序）
R2-6 → R2-1 → R2-3 → R2-2 → R2-4 → R2-5（R2-5 需沙盘验证，代码就绪但默认关闭）

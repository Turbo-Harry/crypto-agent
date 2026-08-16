# D 启动简报 — 第 1 轮（预填草稿）

> 协调者在等 C 裁定时预填；裁定到达后只需替换【裁定结果】区块并发送给 D。
> 来源：docs/plans/brief_template.md + docs/plans/optimization_plan_agentB_R1.md。

你是「四子agent进化循环」中的 Agent D — 代码优化大师（执行者）。工作目录：/Users/wuhai/Desktop/untitled folder/crypto-agent。

只实施下方【已接受方案】，驳回/存疑的一律不碰。每项要求：
1. 先读目标文件（read 工具），改动最小化、保持现有风格
2. 验证：`PYTHONPYCACHEPREFIX="$PWD/.pycache_tmp" python3 -m py_compile <文件>` + 离线单测（构造假数据、不触网、断言核心行为）+ 导入冒烟
3. 下单/资金路径改动必须 fail-closed（查询异常/余额不足/熔断 → 拒绝并告警）
4. 实施后更新 docs/reports/optimization_notes.md（方案编号、改动文件、验证结果）
5. 禁止：删除他人功能、裸 except: pass、改下单路径不加风控
6. 报告：每条方案 = 改动文件 + 验证输出摘录 + 遗留风险

【裁定结果】（C 首轮裁定已填；B 修订版与 R1-10/11/12 裁定到达后补充）
- R1-2 套利喂阈值+综合分快照：【接受】修改=旧台账无快照时直接不喂（不用当前权重重算+标记）
- R1-5 trading_daemon 下线：【接受，方案 A】grep 确认无调度后 mv 为 trading_daemon.py.legacy（勿删）
- R1-1 幽灵止损单清理：【修改后接受】取消该 instId 全部 pending algo 单（不依赖 o["reduceOnly"] 过滤）；沙盘先验证停损单真实挂上
- R1-3 状态文件拆分+原子写：【修改后接受】用独立锁文件或只留原子 os.replace；方向侧阈值保持 70 固定并注释写明（恒 80 分 calibrate 单桶无解）
- R1-4 真实已实现盈亏：【修改后接受】成交价用 fetch_order(id)/fetch_my_trades 回填（place 响应无 avgPx）；fetch_ledger 用 OKX bills 数字编码 type="8"（funding）；沙盘 0 账单打 pnl_estimated 标记
- R1-6 统一杠杆+跨策略幂等：【修改后接受】幂等窄化为"同 symbol 且同 posSide 才拒"；删除"每轮重设 1x"（会反向顶回同 posSide 方向仓）
- R1-7 maker 限价入场：【驳回】150U 净负 EV，推迟到 ≥1000U
- R1-8 波动率目标：【驳回】min(qty,150/price) 在 4190 净值下恒绑定=死代码；B 若改 notional=150*scale 再议
- R1-9 组合相关性：【驳回相关性分支】r_shrunk∈[0.35,0.85] 永不>0.85=纯装饰；总敞口并入 RES-3 账本方案
- R1-10/11/12：待 C 裁定 + B 修订版（另行补充）

【方案全文来源】
- docs/plans/optimization_plan_agentB_R1.md：R1-1~R1-9 完整方案（R1-10/11/12 在 B 的交付消息中）
- 实施前先读对应章节，严格按上方 C 的修改要求调整后再动手

# Agent B — 优化方案设计（R1 轮）

> 本轮立项输入：协调者预确认的缺口（① 幽灵止损单、② 套利平仓不喂阈值学习、③ 杠杆设置三处冲突），
> 加上本人读代码发现的高收益项。共 6 条，按收益/风险排序（R1-1 > R1-6 > R1-2 > R1-4 > R1-3 > R1-5）。
>
> 第二批（Agent A 第二轮 NEW-1~NEW-8 里挑收益/风险比最高的 3 条）：R1-7（NEW-3 执行）、R1-8（NEW-1 波动率目标）、R1-9（NEW-7 组合相关性过滤）。见文末。

---

## R1-1 幽灵止损单清理（fail-closed）

### 现状与证据
- `directional_trader.py` `open_position()`（198–208 行）开仓后挂交易所侧条件停损单：
  `create_order(sym, "market", stop_side, qty, None, params={"posSide": dir, "reduceOnly": True, "ordType": "conditional", "triggerPrice": stop})`。
  这条走 OKX `/trade/order-algo`（algo 单，返回 `algoId`，ccxt `parse_order` 把它放进 `order['id']`，见 `lib/ccxt/okx.py` 3978 行）。
- 全项目 grep 无任何 `cancel_order/cancel_orders` 调用（交易代码中仅 `directional_trader.py` 203–205 行出现 conditional，lib/ 下是 ccxt 源码）。
- 本地 `monitor()`（260–263 行）与 `_liquidate_all()`（309–312 行）平仓后，交易所侧条件单永不取消。
- `reduceOnly=True` 不能兜底：方向匹配时（同币同方向开新仓），旧单触发会照常减仓【新仓】，并占挂单额度。
- OKX `cancelAllOrders` 为 False（`lib/ccxt/okx.py` 59 行 `'cancelAllOrders': False`），不能直接 `cancel_all_orders`；条件单是 algo 单，必须走 `/trade/cancel-algos`（ccxt 里 `cancel_order(id, sym, params={"trigger": True})` 或 `cancel_orders([id], sym, params={"trigger": True})`）。

### 实施方案（函数级）
1. 在 `DirectionalTrader` 新增取消助手（幂等 + fail-closed）：

```python
def _cancel_stop_orders(self, base, reason=""):
    """取消某 instId 的全部挂起条件停损单。
    fail-closed：任何失败只告警飞书、不抛异常、不中断平仓流程。"""
    sym = f"{base}/USDT:USDT"
    try:
        opens = self.exchange.fetch_open_orders(sym, params={"ordType": "conditional"})
        algo_ids = [o["id"] for o in opens if o.get("id") and o.get("reduceOnly")]
    except Exception as e:
        notify(f"⚠️ 取消失败(查询) {base} {reason}: {e}")   # fail-closed
        return False
    if not algo_ids:
        return True
    try:
        self.exchange.cancel_orders(algo_ids, sym, params={"trigger": True})
        return True
    except Exception as e:
        notify(f"⚠️ 取消失败 {base} {reason} algoIds={algo_ids}: {e}")  # fail-closed
        return False
```

2. 三处挂接（全部在“平仓成功/开仓前”调用）：
   - `monitor()`：`create_order(... 平仓 ...)` 成功、`continue` 之前调用 `self._cancel_stop_orders(base, "止损/止盈平仓")`。
   - `_liquidate_all()`：`create_order(... 强平 ...)` 之后、`log_exit` 之前调用 `self._cancel_stop_orders(base, "熔断强平")`。
   - `open_position()`：在 `try: create_order(...)` 主仓单之前（即 193 行 try 之前）调用 `self._cancel_stop_orders(base, "开仓前清理残留")`，保证同 instId 无旧条件单残留再挂新单（幂等）。

3. （可选加固）开仓时把 algoId 持久化，供精确取消与对账：
   - `create_order` 条件单返回值 `algo_resp['id']` 存入 journal。给 `TradeJournal.log_entry()` 增加可选参 `stop_algo_id=None`，写入 trade dict；`_cancel_stop_orders` 优先用 `t['stop_algo_id']` 精确取消，取不到再走 fetch-and-cancel-all 兜底（兼容存量单与 journal 写入前崩溃的场景）。

### 验证方法（离线单测，纯风控修复，不涉及过拟合）
1. 单测（monkeypatch 掉 `self.exchange`，不触网）：
   - 断言 `monitor` 平仓分支调用 `fetch_open_orders(sym, {"ordType":"conditional"})` 且对返回的 `reduceOnly` 单调用 `cancel_orders(ids, sym, {"trigger":True})`。
   - 断言 `_liquidate_all` 对每个 instId 各调用一次取消。
   - 断言 `open_position` 在主仓单之前先取消。
2. 异常路径：让 `fetch_open_orders`/`cancel_orders` 抛异常 → 断言函数返回 False 且 `notify` 被调用一次（fail-closed 告警）、平仓流程不中断（不向上抛）。
3. 幂等：`fetch_open_orders` 返回空列表 → 取消函数直接返回 True，不报错。
4. 沙盘实测：`python3 directional_trader.py --once` 后调一次手动平仓，用 OKX 原生 `GET /api/v5/trade/orders-algo-pending?instId=...-USDT-SWAP` 确认该 instId 挂起条件单数量归 0。

### 预期收益与风险
- 收益：根除“旧停损单误平新仓”的资本事故；释放挂单额度；消除幽灵单长期残留。
- 风险：`fetch_open_orders`/`cancel_orders` 是新增 REST 调用，失败被吞只告警；极端情况下 `ordType=conditional` 过滤与 OKX 返回字段差异可能漏取消 → 用“开仓前清理 + fetch-and-cancel-all”双保险覆盖。最坏情况 = 现状（取消失败→告警），不会新增错误平仓。

### 回滚方式
- 三处 `self._cancel_stop_orders(...)` 调用点整体注释掉即可恢复原逻辑；`_cancel_stop_orders` 方法删除不影响其它代码。若第 3 步（algoId 持久化）单独回滚，仅去掉 `stop_algo_id` 参数，取消逻辑退回 fetch-and-cancel-all。

---

## R1-6 统一杠杆策略：跨进程 set_leverage 覆盖 + trading_daemon 对齐 1x

### 现状与证据
- 三处杠杆映射冲突：
  - `trading_main.py` 36 行 `LEVERAGE_MAP` 全部 1x（套利对冲，OP-3 已改）。
  - `directional_trader.py` 32 行 BTC/ETH 3x、SOL/XRP/DOGE 2x（方向性）。
  - `trading_daemon.py` 28 行仍 3x/2x（套利常驻进程，上轮“对冲降 1x”漏改此文件）。
  - `funding_arb.py` 37 行已 1x（无需改）。
- 四入口共用同一 OKX 模拟盘账户，均调用 `private_post_account_set_leverage({"instId","lever","mgnMode":"isolated","posSide"})`（`trading_main.py` 235–242、`directional_trader.py` 163–169、`trading_daemon.py` 122–128、`funding_arb.py` 40–55）。
- OKX isolated 的 set_leverage 会【重新保证金化该 instId+posSide 现有持仓】：同 instId+同 posSide 上，后设进程覆盖先设进程。
- 实际冲突路径：套利正费率开合约空腿(posSide=short)、方向性也开 BTC short(posSide=short) → 同 instId+short；或套利负费率开合约多腿(posSide=long)、方向性开 BTC long → 同 instId+long。方向性 3x 与套利 1x 互顶。

### 实施方案（函数级，最简且安全）
1. `trading_daemon.py` 28 行 → `LEVERAGE_MAP = {"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1}`（与 `funding_arb.py` 37 行一致）。套利入口全部 1x。
2. `directional_trader.py open_position()`：保留 2–3x，并确认【开仓前重设本方向杠杆】已在 163–169 行（create_order 之前）执行——满足“开仓前必须重设”。平仓后不改回、不记账“谁最后设的”（协调者判断为过度设计）。
3. **新增跨策略幂等（消除同 instId 同 posSide 并发持仓）**：`open_position()` 在 risk gate 之后、journal 幂等之前，查交易所持仓，同币种已有任意合约持仓则拒绝：

```python
# 跨策略幂等：套利仓/其它进程已持有该 instId 合约仓位时不重叠开仓
sym = f"{base}/USDT:USDT"
try:
    positions = self.exchange.fetch_positions()
    same_base = [p for p in positions
                 if p.get("symbol") == sym and p.get("contracts", 0) != 0]
    if same_base:
        print(f"⏭️ {base} 交易所已有 {len(same_base)} 个合约持仓（可能为套利对冲仓），跳过（跨策略幂等）")
        return None
except Exception:
    pass  # 查询失败退回 journal 幂等（不阻断）
```

   （`trading_main._risk_guard` 191–194 行已有同款检查；方向侧补上后即“双向幂等”。）
4. （防御纵深）`trading_main.manage_arb_positions()` 每轮对仍持有的套利合约腿重设 1x（每轮一次 REST，幂等，兜底被方向进程顶成 3x 的最坏情形）：

```python
pos = next((p for p in positions if p.get("symbol") == sym and p.get("contracts", 0) != 0), None)
if pos is None:
    ...  # 原逻辑：从台账移除
    continue
try:
    self.exchange.private_post_account_set_leverage({
        "instId": self.exchange.market(sym)["id"], "lever": "1",
        "mgnMode": "isolated", "posSide": pos["side"]})
except Exception:
    pass
```

### 验证方法（离线单测，纯风控修复）
1. `trading_daemon` 单测：断言 `LEVERAGE_MAP` 全部值为 1；`open_hedge` 调 set_leverage 时 `lever == "1"`。
2. `directional_trader` 幂等单测（monkeypatch `fetch_positions`）：返回含 `{"symbol":"BTC/USDT:USDT","contracts":1}` → 断言 `open_position` 返回 None 且不调 `create_order`；返回空 → 正常走开仓。
3. 顺序单测：断言 `open_position` 中 `set_leverage` 调用发生在 `create_order`（主仓单）之前。
4. 沙盘实测：先 `funding_arb.py open BTC/USDT:USDT 100`（合约空腿 1x），再跑 `directional_trader.py --once`，断言方向侧日志输出“交易所已有持仓，跳过”，且 BTC 合约仍只有 1 个仓位、杠杆未被改 3x。

### 预期收益与风险
- 收益：消除“套利 1x 被顶成 3x（爆仓距离缩短）/方向 3x 被顶成 1x（多占保证金）”两类覆盖事故；套利三入口杠杆语义统一为 1x。
- 残余风险（取舍说明）：
  1. TOCTOU 竞态：双向幂等是“查→开”两步，极小窗口内两进程可能同时查到空仓同时开仓。取舍：接受该极小概率，因为（a）两进程各自在开仓前重设自己那条腿的杠杆，最后一次 set 属于“后开仓方”自己的正确值；（b）第 4 步 arb 每轮重设 1x 兜底，把“套利腿被顶 3x”的最坏情形在下一轮（≤1 分钟）纠正。
  2. 手动干预/程序外开平仓无法用代码覆盖，取舍：依赖对账与飞书告警，不在本期引入“最后设杠杆者”记账。
  3. 方向侧若与套利腿同 instId 但【不同 posSide】（如方向 long + 套利 short）本就不会互相 re-margin，仍被第 3 步“同币种拒绝”一并挡掉——这是有意的保守：宁可错过一个方向性机会，也不让两套策略同币种叠加敞口。

### 回滚方式
- `trading_daemon` LEVERAGE_MAP 改回 3/2x 即恢复（但会重新暴露覆盖风险，不建议）。
- 第 3 步幂等检查整段注释掉即恢复“仅 journal 幂等”；第 4 步重设 1x 删除不影响平仓逻辑。

---

## R1-2 套利平仓喂阈值学习 + 综合分开仓快照

### 现状与证据
- `threshold_learner.record(score, pnl)` 只在 `directional_trader.py` 281–282 行调用；`trading_main.py` 套利仓平仓 `_close_hedge()`（359–367 行）只把净盈亏喂 `weight_learner`，从不喂 `threshold_learner`。
- 而套利决策正是用 `threshold_learner.threshold` 做闸门（`decide()` 180 行 `total >= self.threshold_learner.threshold`），却从不喂回样本 → 阈值校准只有方向性样本（且方向性样本 score 恒为 `SIGNAL_SCORE=80`），存在样本偏差。
- 台账 `arb_positions.json` 已存 `scores` 子分数（`execute()` 269–276 行），但未存综合分，也未存权重版本。
- 额外发现：`run_once()` 386 行 `self.execute(base, sig)` 连 `scores` 都没传（`decide` 得到的 `scores` 被丢弃），`--once` 模式整条学习链不喂数据。

### 实施方案（函数级）
1. 台账在开仓时存【综合分快照 + 权重版本快照】（选择：**开仓时快照，而非平仓时用当前权重重算**）：
   - 理由：`threshold_learner` 的语义是“决策时的分数 → 结果”。平仓标签是被“开仓那一刻的分数/权重”触发的那笔决策产生的；若用后来可能已进化的权重重算，得到的分数不再是当初真正触发开仓的分数，等于把标签挂在另一条决策规则上，是隐式未来函数（事后诸葛亮）。这与系统自身“无未来函数”的纪律一致。
   - 快照也保真：权重层进化后，历史样本的“分数→盈亏”映射不被事后改写。

```python
def execute(self, base, sig, scores=None, composite_score=None):
    ...
    self.arb_positions.append({
        "base": base, "amount": amount,
        "dir": "short" if rate > 0 else "long",
        "entry_sign": 1 if rate > 0 else -1,
        "entry_rate": rate,
        "scores": scores if scores else {},
        "composite_score": composite_score,           # 新增：开仓快照
        "weights_version": self.weight_learner.version,  # 新增：权重版本
        "opened_at": time.time(), "flip_since": None,
    })
```

2. 两个调用点都传 `total` 与 `scores`（修复 run_once 漏传）：
   - `run_once()`：`self.execute(base, sig, scores=scores, composite_score=total)`。
   - `run()`：`self.execute(base, sig, scores=scores, composite_score=total)`。

3. `_close_hedge()` 平仓成功后，在喂 `weight_learner` 的同时喂 `threshold_learner`（向后兼容旧台账无快照）：

```python
if ok:
    self.arb_positions.remove(rec)
    self._save_arb_positions()
    net_pnl = ...  # 见 R1-4（真实已实现盈亏）
    score = rec.get("composite_score")
    if score is None and rec.get("scores"):
        # 旧台账无快照：用当前权重重算并打“recomputed”标记（审计用，不默认信任）
        score = composite(rec["scores"], self.weight_learner.weights)
    if score is not None:
        self.threshold_learner.record(float(score), float(net_pnl))
    if rec.get("scores"):
        self.weight_learner.record(rec["scores"], net_pnl)
```

### 验证方法（含样本外）
1. 离线单测（monkeypatch 无网）：构造 `rec`（含 `composite_score`/`scores`/`weights_version`）与假 `threshold_learner`/`weight_learner`，断言 `_close_hedge` 成功后两者各 `record` 一次、`score` 取自快照而非重算。
2. 向后兼容单测：`rec` 无 `composite_score` 但有 `scores` → 断言用 `composite(rec["scores"], weights)` 重算且打 recomputed 标记；`scores` 也为空 → 两个 learner 都不 `record`（不喂垃圾样本）。
3. **样本外/防过拟合验证**（阈值校准）：
   - 用固定时间窗（如按 `opened_at` 排序）做**前 70% 训练 / 后 30% 样本外**的 walk-forward：训练段跑 `ThresholdLearner.calibrate()` 得到阈值 T，在样本外段用 T 判断“综合分≥T 的样本平均 pnl 是否显著 ≥ 0 且优于初始阈值 70 的决策收益”。
   - 断言：样本外加入套利样本后，分数桶分布从“仅 80 单点”变为覆盖 0–100；校准阈值变化量必须 ≤ `safety_margin` 夹逼（`min_threshold=60 / max_threshold=90` 已有硬约束）。
   - 记录 `weights_version` 用于事后审查：凡 `composite_score` 对应版本 ≠ 平仓时现役版本的样本，单独打标，不参与“权重层 IC”评估，避免版本混杂。

### 预期收益与风险
- 收益：补上“综合分→盈亏”的套利样本，消除幸存者偏差的反面（只有方向性样本）；阈值从此由两类真实决策共同校准；修复 `--once` 漏喂 scores。
- 风险：快照分数量纲与方向性样本（恒 80）仍在同一 `threshold_state.json` 混桶 → 见 R1-3 拆分。套利样本量少（`min_samples=30`）时不会触发校准，只累积不误改。

### 回滚方式
- 删除 `_close_hedge` 中新增的 `threshold_learner.record` 调用即回到“只喂 weight_learner”；`execute` 新增两个字段为增量写入，旧代码读台账 `rec.get("composite_score")` 返回 None 走重算分支，向后兼容。台账文件为 append 型，删除新字段后旧记录不受影响。

---

## R1-3 学习状态文件跨进程共享与分数尺度混杂

### 现状与证据
- `directional_trader.py` 85 行与 `trading_main.py` 69 行都用 `ThresholdLearner()`（默认 `path="threshold_state.json"`）——**两个独立进程读写同一状态文件**，无文件锁、无原子写（`threshold_learning.py` `_save()` 47–50 行直接 `open(path,"w")`），并发时丢失更新/写坏 JSON。
- 且两者分数语义不同：方向性样本 score 恒为 `SIGNAL_SCORE=80`，套利样本为 0–100 连续综合分。混在同一 `threshold_state.json` 的桶里，80 桶被方向性样本主导、其它桶只有套利样本，`calibrate()` 的“盈亏平衡桶”判断会被两种不同评分体系互相污染。
- 同样问题存在于 `weight_state.json`（`weight_learning.py` 41 行默认路径，`trading_main.py` 93 行使用；若未来多进程实例化即冲突）、`trade_journal.json`（`TradeJournal` 默认路径，`directional_trader.py` 与 `SelfEvolvingTrader` 都加载）。

### 实施方案（函数级）
1. **按策略拆分阈值状态文件**（推荐，语义正确）：
   - `directional_trader.py` 85 行 → `ThresholdLearner(path="threshold_state_dir.json")`。
   - `trading_main.py` 69 行 → `ThresholdLearner(path="threshold_state_arb.json")`。
   - 理由：方向性策略（信号分 80 常量、方向性盈亏）与套利策略（0–100 综合分、套利盈亏）本就是两条不同决策规则，各自校准各自阈值才无尺度污染。
2. **原子写 + 可选文件锁**（对仍共享的文件，如 `trade_journal.json`）：
   - `threshold_learning.py` `_save()` 与 `weight_learning.py` `_save()` 改为：先写 `path + ".tmp"`，再 `os.replace(tmp, path)`（原子替换，崩溃不留半截 JSON）。
   - 对跨进程共享文件加 `fcntl.flock(f.fileno(), LOCK_EX)`（Unix）包住“读-改-写”临界区；Windows 用 `msvcrt`。锁只做串行化，不改变逻辑。
3. `record()` 样本加 `"strategy"` 字段（`"dir"` / `"arb"`），便于事后审计与 profile 分桶排障。

### 验证方法（离线单测）
1. 并发写测试：起 N 个线程/进程各 `record()` M 次到同一 `threshold_state.json`，断言文件始终是可解析 JSON、`decisions` 长度单调不减、无 `JSONDecodeError`。
2. 原子写测试：`_save()` 后断言目录无残留 `.tmp`、`os.replace` 前后 inode 变化（验证是替换非原地写）。
3. 拆分验证：断言两个 learner 实例各自 `path` 不同、`record` 不互相可见。
4. 尺度验证：分别向 dir/arb 两个 learner 喂同批次样本，断言二者 `calibrate()` 结果可不同且互不影响（不共享桶）。

### 预期收益与风险
- 收益：消除并发写坏状态文件（最坏会导致阈值/权重被清空或变成随机数，等于风控参数被劫持）；消除方向性 80 常量与套利连续分混桶带来的阈值校准偏差。
- 风险：拆分后阈值不再“全局统一”，两个策略各一套阈值（本就是正确语义）。迁移首日旧 `threshold_state.json` 需要手动决定归属（建议归档重命名，两个 learner 从 70 重新累积，`min_samples=30` 未满前保持 70 不动，不误改）。

### 回滚方式
- 把两个 `path` 参数改回 `ThresholdLearner()` 默认值即恢复共享单文件；原子写/锁为纯实现层加固，改回 `open(path,"w")` 不影响语义。旧状态文件保留即可回读。

---

## R1-4 套利盈亏用真实已实现盈亏，替代估算

### 现状与证据
- `_close_hedge()`（362–365 行）：`funding_pnl = abs(entry_rate)*3*max(days_held,0)`，`net_pnl = funding_pnl - 0.003`。这是**估算**：只有费率收入、往返成本硬编码 0.003、**完全忽略基差盈亏**。
- 而 `manage_arb_positions()` 的平仓主因之一就是“基差向不利方向扩张”（317–320 行），此类平仓的基差亏损（空腿 perp 溢价扩大）正是这笔交易的真实主盈亏来源，却被估算值抹掉。这个被污染的标签既喂 `weight_learner`（R1-4 前置），也将喂 `threshold_learner`（R1-2）——样本标签错，两层学习都白搭。
- 台账只存 `entry_rate`/`sig["price"]`（参考价），不存**实际成交价**；`_close_hedge` 也不记录平仓成交价，无法算真实盈亏。

### 实施方案（函数级）
1. 台账开仓时记录**实际成交价**（来自下单返回的 `average` 填充价）：
```python
spot_order = self.exchange.create_market_buy_order(f"{base}/USDT", amount)   # 或 sell
perp_order  = self.exchange.create_market_sell_order(sym, amount, params={"posSide": "short"})
...
"spot_entry_px": (spot_order.get("average") or spot_order.get("price")),
"perp_entry_px": (perp_order.get("average") or perp_order.get("price")),
"entry_notional": amount * price,   # 名义本金（归一化分母）
```

2. `_close_hedge` 记录平仓成交价并计算真实盈亏（名义比例）：
```python
spot_close = self.exchange.create_market_sell_order(f"{base}/USDT", amount)  # 反向
perp_close = self.exchange.create_market_*_order(sym, amount, {"posSide": ...})
funding_received = self._fetch_funding_received(base, rec["opened_at"])  # OKX /api/v5/account/bills?type=funding 或 ccxt fetch_ledger
spot_pnl  = (spot_close.avg - rec["spot_entry_px"]) * amount * (1 if 现货多 else -1)
perp_pnl  = (rec["perp_entry_px"] - perp_close.avg) * amount   # 合约腿方向符号
fees      = 实际手续费（账单）或 config.ARB_ROUNDTRIP_COST 兜底
net_pnl   = (spot_pnl + perp_pnl + funding_received - fees) / rec["entry_notional"]
```
   - `_fetch_funding_received`：优先 `self.exchange.fetch_ledger(code="USDT", params={"type": "funding"})`（OKX `has['fetchLedger']=True`，见 `lib/ccxt/okx.py` 119 行）按 `since=rec["opened_at"]*1000` 过滤加总；失败时退回现有估算公式，但给样本打 `"pnl_estimated": True` 标记（审计用）。

3. 喂 learner 处改为优先真实 `net_pnl`，估算值仅作兜底并打标。

### 验证方法（含样本外）
1. 离线单测：构造假 spot/perp 订单（含 `average`）与假 ledger，断言 `net_pnl = (spot+perp+funding-fees)/notional` 与手算一致；含“基差扩张平仓”用例，断言此时 `net_pnl` 显著为负（而非旧估算法返回的近 0）。
2. 标签对比：对同一批历史套利记录，用旧估算 vs 新真实算法各算一遍，断言两者在“基差平仓”样本上分歧显著（证明旧标签确有系统性偏差）。
3. **样本外验证**：把带真实标签的样本喂 `weight_learner`，用 walk-forward（前 70% 训练 / 后 30% 样本外）评估候选权重在样本外段的分数-盈亏 IC，断言真实标签下 IC 的样本外衰减 ≤ 估算标签下 IC 的衰减（即真实标签更稳定、更少过拟合）。

### 预期收益与风险
- 收益：修复两层学习（权重/阈值）最底层的标签质量；基差亏损不再被抹掉，阈值/权重才能真正学会“基差扩张 = 亏损”这一关键风险模式。
- 风险：真实成交价/账单接口依赖 OKX 返回字段（`average`/账单 type=funding），沙盘/字段缺失时退回估算并打标，不会因字段异常而崩溃。计算复杂度上升，仅在平仓时发生，不影响热路径。

### 回滚方式
- 台账新增字段为增量；`net_pnl` 计算函数单独封装，回滚时把 `net_pnl` 恢复为 `abs(entry_rate)*3*max(days_held,0) - 0.003` 即可，台账与 learner 调用无需改动。

---

## R1-5 trading_daemon.py 遗留裸单腿 + 重复对冲逻辑

### 现状与证据
- `trading_daemon.py` 是与 `trading_main.py` 并存的另一套套利入口，逻辑陈旧且危险：
  - `open_hedge()` 74 行 `NOTIONAL = 700`、28 行 `LEVERAGE_MAP` 3x（`trading_main` 已改为 150 / 1x，见 OP-3 注释）。
  - **负费率分支（147–155 行）只开“合约多腿”，注释自称“单腿风险”**——这是裸方向性多头仓，无止损、无对冲，与“对冲套利”目标矛盾，资金费率转负即实亏。
  - `check_positions()`（163–179 行）只告警、从不平仓；不写 `arb_positions.json`、不喂任何 learner，与 `trading_main` 的自动平仓/台账/学习完全脱节。
  - 幂等检查用 `NOTIONAL*2` 余额、无净年化闸门之外的其它 `trading_main._risk_guard` 防护（持仓数上限、单币名义敞口上限均缺失）。
- 若该 daemon 仍被 cron/systemd 启动，会与 `trading_main` 在同一账户并发下单，冲突且绕过新风控。

### 实施方案（函数级）
- **方案 A（推荐）：下线该入口**。删除或重命名为 `trading_daemon.py.legacy`，并检查 cron/pm2/systemd 是否有对 `trading_daemon.py` 的调用，全部改指 `trading_main.py`。
- **方案 B（若需保留轮询式入口）**：把 `open_hedge()` 的负费率分支改为与 `trading_main.execute()` 一致的对冲（现货空腿需保证金账户则**整体拒绝**并告警，而非只开单腿）；`NOTIONAL` 对齐 150、`LEVERAGE_MAP` 对齐 1x（杠杆统一见 R1-6）；删除本文件本地幂等/余额逻辑，改为 import `trading_main` 的 `_risk_guard`/`execute`；`check_positions` 改为调用 `manage_arb_positions` 实现自动平仓与台账同步。

### 验证方法（离线单测 + 环境核查）
1. 环境核查：`grep -rn trading_daemon` 检查 cron/crontab、pm2、systemd、`*.sh`、`node_modules` 之外的启动脚本，确认是否仍在被调度。
2. 方案 B 单测：monkeypatch `exchange`，断言负费率分支**不再产生合约多头裸仓**（要么现货空+合约多对冲，要么整体拒绝返回 False）。
3. 幂等/闸门单测：断言 `open_hedge` 走 `_risk_guard` 后，持仓数>4、单币敞口>600 时返回 False。

### 预期收益与风险
- 收益：消除一个可被 cron 误触发的裸多头入口；避免双入口并发下单/绕过新风控；消除 700/3x 与 150/1x 两套参数并存的混乱。
- 风险：若 daemon 实际已停用，本条收益主要为“消除隐患”，改动量小；若仍被调度，下线前需先停调度（避免切换瞬间双跑）。

### 回滚方式
- 方案 A：重命名回 `.py` 并恢复调度即可。方案 B：git 层面保留 diff，回退到原 `open_hedge` 实现。

---

# 第二批设计（Agent A 第二轮 NEW-1~NEW-8 精选）

> 从 8 条里挑收益/风险比最高且契合"防过拟合/小仓位/宁可做对"哲学的 3 条：
> NEW-3（执行，零过拟合）> NEW-1（波动率目标，JF 级证据 + 已有数据复用）> NEW-7（组合相关性，自证回撤 52%→19.4%）。
> 未选理由：NEW-2 Kelly 在 journal 样本极少时极度不稳、负 EV 过滤会把门焊死（过拟合重灾区）；
> NEW-4/5/6/8 均需额外数据或模型且样本外证据不足，留待数据更全后再立项。

---

## R1-7 方向仓入场限价 maker + 超时市价兜底（NEW-3）

### 现状与证据
- `directional_trader.open_position`（195–197 行）`create_order(sym, "market", side, qty, params={"posSide": dir})`——纯 taker。
- `trading_main.execute`（254–262 行）、`funding_arb.open_hedge`（118–145 行）也是 `create_market_*` 全 taker。
- `config.FEE_RATE=0.001` 只在回测用，实盘无滑点/费率控制；OKX taker vs maker 差 0.05–0.15%/笔，150 USDT 小单成本占比最高。

### 实施方案（函数级）
1. 在 `execution.py` 新增 `maker_or_market()`（只用于【单腿方向仓入场】，不用于两腿对冲）：

```python
def maker_or_market(exchange, symbol, side, qty, params=None, timeout_s=60, poll_s=5, max_price_dev=0.002):
    """先挂 postOnly 限价 maker（零费/返佣），超时或被拒则撤单改市价兜底。
    仅限单腿方向仓；两腿对冲禁用（一腿部分成交=裸腿）。"""
    params = params or {}
    book = exchange.fetch_order_book(symbol, 5)
    top = book["bids"][0][0] if side == "buy" else book["asks"][0][0]
    if not top:
        return exchange.create_order(symbol, "market", side, qty, params=params)
    # 价偏离过大（信号价已远离盘口）→ 直接市价，不冒 60s 追价风险
    ref = exchange.fetch_ticker(symbol)["last"]
    if ref and abs(top - ref) / ref > max_price_dev:
        return exchange.create_order(symbol, "market", side, qty, params=params)
    order = exchange.create_order(symbol, "limit", side, qty, top,
                                  params={**params, "postOnly": True})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        o = exchange.fetch_order(order["id"], symbol)
        if o["status"] in ("closed", "filled"):
            return o
        if o["status"] in ("canceled", "rejected", "expired"):
            break                       # 跨价被拒 → 落市价
        time.sleep(poll_s)
    try:
        exchange.cancel_order(order["id"], symbol)   # 撤未成交残单
    except Exception:
        pass
    return exchange.create_order(symbol, "market", side, qty, params=params)
```

2. `directional_trader.open_position`：把主仓 `create_order(sym, "market", ...)`（195 行）替换为
   `order = maker_or_market(self.exchange, sym, side, qty, params={"posSide": sig["dir"]})`；
   后续条件停损单、journal 记录不变（journal 的 `entry_price` 建议改用 `order.get("average")`，见风险项，本期可不动）。
3. **明确不做 TWAP**：150 USDT 订单市场冲击≈0，拆单无收益反而引入最小下单量/部分成交/时序风险。仅当未来名义 ≥ 1000 USDT 再评估 TWAP，且每片须 ≥ `max(min_qty, min_notional/price)`（`execution.qty_for_notional` 已有 min/max 校验可复用）。
4. 两腿对冲（`trading_main`/`funding_arb`）与止损止盈出场**保持市价**——对冲要两腿同时成交、止损是风控必须即时成交，均不走限价。

### 验证方法（离线单测 + 沙盘，零参数拟合）
1. 单测（mock `exchange`）：断言（a）先发 postOnly 限价、成交则不市价；（b）超时路径 `cancel_order` + 市价；（c）`rejected/canceled` 路径市价；（d）`top` 缺失或 `max_price_dev` 超阈直接市价。
2. 沙盘 A/B：`--once` 开仓记录 maker 成交率、成交价 vs 同刻市价差、超时兜底次数；累计 N 笔对比 taker 与 maker 的实际成本差，确认省费 0.05–0.15% 落地。
3. 防过拟合：`timeout_s`/`max_price_dev` 是工程参数（非从历史拟合），无需样本外；但需在沙盘记录"超时兜底导致的价格滑点"是否吃掉省下的费（若某币频繁超时且滑点 > 费差，则该币退回纯市价）。

### 预期收益与风险
- 收益：每笔方向仓确定性省 0.05–0.15% 成本，零过拟合，直接落净收益。
- 风险：限价不成交→市价兜底有最多 `timeout_s` 延迟，期间价格不利会小亏；用 `max_price_dev` + 短超时（60s）限制追价风险。postOnly 跨价被拒自动落市价。成交价可能偏离 `sig["entry"]`，导致预设 stop/tp 距离偏移——标注为既有滑点问题（现市价单同样存在），本期不越界改动。

### 回滚方式
- `open_position` 把 `maker_or_market(...)` 一行改回 `create_order(sym, "market", ...)` 即完全恢复；`execution.maker_or_market` 保留不调用则无副作用。

---

## R1-8 波动率目标仓位缩放（NEW-1）

### 现状与证据
- `directional_trader.open_position`（170–175 行）固定 `RISK_PER_TRADE=0.01` + `min(qty, 150/price)`；`risk/risk_manager.position_size` 固定 `config.RISK_PER_TRADE=0.015`。
- `scan_signal`（108–139 行）已 fetch 100 根日线并算 ATR，可直接复用算【日线已实现波动率】；`realtime_okx.vol_15m` 只用于 `score_volatility` 打分、不用于仓位。
- 关键区分：方向仓是日线级持有（止损/止盈可能数日才触发），仓位缩放必须用**日线已实现波动率**，不是 15 分钟振幅（日内噪音大、量纲不匹配）。

### 实施方案（函数级）
1. `scan_signal` 返回值新增 `"ann_vol"`：`std(20 日 close 对数收益率) × sqrt(365)`（`import numpy as np` 或纯 Python；无 20 日数据返回 None）。
2. `open_position` 在 qty 计算前加缩放：

```python
VOL_TARGET = 0.20          # 年化目标波动率（文献常用 15–25%，取 20% 先验）
VOL_SCALE_MIN, VOL_SCALE_MAX = 0.5, 1.5   # 缩放上下限（安全边界，非拟合）

realized = sig.get("ann_vol") or VOL_TARGET
scale = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, VOL_TARGET / realized))
risk_budget = RISK_PER_TRADE * scale
qty = (balance * risk_budget) / (price * stop_dist)
qty = min(qty, 150 / price)   # 150 USDT 名义天花板【始终生效】
```

3. 语义：高波动 → 降仓（宁可少赚不冒大险）；低波动 → 升仓但受 `scale≤1.5` 与 `150/price` 双约束，不激进超配。

### 校准与样本外验证（参数类，必须防过拟合）
- 校准数据：项目自身日线（`data/` 缓存 + `backtest/engine.py` 的池子），walk-forward。
- **校准方式 = 不拟合**：`VOL_TARGET=0.20` 来自文献先验，**不**对项目 PnL 做网格寻优（寻优 target = 过拟合）。`[0.5,1.5]` 是安全夹逼，非拟合产物。
- 样本外验证：
  1. 在 `backtest/engine.py` 的 `_enter` 实现同样缩放（用截至 T 日的 20 日日线算 realized，T+1 开盘执行，无未来函数）。
  2. walk-forward 分年滚动：每段独立跑"有/无缩放"两版。
  3. **主判据 = 机制生效**：组合已实现年化波动率是否向 20% 收敛（而非单纯 Sharpe 变高——Sharpe 变高可能是运气）。
  4. 辅助判据：最大回撤、Sharpe。
  5. 敏感性网格（防过拟合体检）：`VOL_TARGET∈{0.15,0.20,0.25}`、窗口∈{10,20,30}、上限∈{1.2,1.5,2.0}，报告各指标随参数变化的稳定性；若结论对参数极度敏感（换个 target 就从盈利变亏损），判为过拟合，**不上线**。

### 预期收益与风险
- 收益：压平回撤、高波动自动降险（Moreira & Muir JF 证据），实现成本低（复用已有日线）。
- 风险：20 日 realized 是后视估计，极端行情跳变时缩放滞后；用 0.5–1.5 夹逼限制幅度。日线样本少（20 点）波动率估计噪声大，故只做温和缩放、不激进。

### 回滚方式
- `open_position` 删掉 `scale/risk_budget` 两行、恢复 `RISK_PER_TRADE` 即回滚；`scan_signal` 的 `ann_vol` 为增量字段，旧调用不受影响。

---

## R1-9 组合相关性过滤 + 总敞口上限（NEW-7，不做 HRP）

### 现状与证据
- `directional_trader.SYMBOLS = ["BTC","ETH","SOL","XRP","DOGE"]`（32 行）5 个高 beta 强相关币；`scan_signals`（319–339 行）逐个**独立**开仓，无相关性矩阵、无风险平价。
- `config.MAX_HOLDINGS=4` 只限数量；`trading_main._risk_guard`（203–209 行）有单币 600 敞口上限，但无【组合总敞口】约束。
- 回测 report 自证"组合分散化回撤 52%→19.4%"。
- 5 币样本下相关性估计不稳定（30 日窗口 ~30 点，相关系数标准误 ~0.19）。

### 实施方案（函数级，保守稳健，不做 HRP）
1. 新增 `portfolio_guard.py`（或 `risk/` 下）：

```python
def portfolio_guard(held, candidate_base, klines_map, corr_threshold=0.85,
                    window=60, shrink=0.5, total_cap=600):
    """held: 已持有且同方向 base 列表；klines_map: {base: 日线 close 列表}。
    返回 (放行?, 原因)。只对【极端相关】动作，5 币样本下噪声稳健。"""
    # 1. 组合总名义敞口上限（零相关估计，最稳）
    if sum(h["notional"] for h in held) >= total_cap:
        return False, f"组合总敞口已达 {total_cap} USDT 上限"
    # 2. 相关性拦截：Spearman 秩相关（对离群稳健）+ 60 日 + 缩水
    def spearman(a, b):
        ra = sorted(range(len(a)), key=lambda i: a[i])
        rb = sorted(range(len(b)), key=lambda i: b[i])
        return 1 - 6*sum((ai-bi)**2 for ai,bi in zip(ra,rb))/(len(a)**3-len(a)) if len(a)>3 else 0.0
    for h in held:
        ca, cb = klines_map[candidate_base][-window:], klines_map[h["base"]][-window:]
        if len(ca) < 30 or len(cb) < 30:
            continue
        r = spearman(ca, cb)
        mean_corr = 0.7          # 币圈高相关先验均值（缩水目标）
        r_shrunk = shrink * r + (1 - shrink) * mean_corr
        if r_shrunk > corr_threshold:
            return False, f"{candidate_base} 与已持 {h['base']} 相关 {r_shrunk:.2f} > {corr_threshold}，跳过"
    return True, ""
```

2. 落点：
   - `directional_trader.scan_signals`：在 `open_position` 之前调 `portfolio_guard`（held = journal 中 open 且同方向仓；klines_map 用 `scan_signal` 已缓存的日线或新增轻量缓存）。
   - `trading_main._risk_guard`：新增"组合总合约敞口 ≤ 600 USDT"（已有单币 600，补组合维度）。
3. **明确不做 HRP/风险平价**：5 资产 HRP 与协方差矩阵在样本量下过度且不稳定；等风险 = 每仓 150 名义（现有上限已实现），组合总敞口封顶即等价于朴素等风险。

### 验证方法（含 5 币稳定性，防过拟合）
1. 离线单测：mock 相关矩阵，断言 corr>0.85 且同方向拦截、<0.85 放行、不同方向放行、总敞口超限拒绝。
2. **稳定性验证（针对 5 币样本）**：
   - Bootstrap：从历史日收益重采样 200 次，比较 `corr_sample` 与 `corr_shrunk` 的估计分布宽度，断言缩水后区间显著更窄（证明 shrink 有效）。
   - 敏感性网格：`corr_threshold∈{0.7,0.8,0.85,0.9}`、`window∈{30,60,90}`、`shrink∈{0,0.3,0.5}`，报告拦截率与回撤随参数变化；若拦截率对阈值极度敏感（0.8 全拦 / 0.85 全放），则**只上线"总敞口上限"、不上线相关性拦截**。
3. 样本外回测：`backtest/engine.py` 加组合 guard，walk-forward（相关只用截至 T 日数据，无未来函数），比较有/无 filter 的最大回撤与 Sharpe；主判据 = 回撤降低 + 拦截率合理（5–30%），而非单纯收益提升。

### 预期收益与风险
- 收益：消除"4 个同向高相关仓 = 4 倍 beta"的隐藏集中风险；总敞口上限直接封顶下行。
- 风险：5 币相关性估计噪声大 → 用 Spearman + 60 日 + 缩水 + 仅 0.85 极端阈值四层稳健化，宁可少拦不误拦；若稳定性验证不过，退化为只上线"总敞口上限"（零相关估计、零过拟合）。

### 回滚方式
- `portfolio_guard` 调用点注释掉即恢复逐币独立开仓；`_risk_guard` 的组合总敞口判断删除即可。均为增量，不动现有单币/单仓逻辑。

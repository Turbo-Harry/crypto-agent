# Agent B — R2 批次优化方案设计

> 立项：RES-6 / RES-7 / RES-16 / R3-1 / R3-3（高优）+ RES-17（中）。
> 共 6 条，按收益/风险排序。每条含现状证据 / 函数级伪代码 / 防过拟合验证方法 / 收益风险 / 回滚方式。
> 明确不立项：RES-15（与批次2改同文件，批次2后下一轮）；RES-8/11/12/13/14/18/19/20（上轮已标放弃）。

---

## R2-1（RES-6）EvolutionGate 回滚回写 WeightLearner.weights

### 现状证据
- `evolution_gate.py` `_rollback()`（149–156 行）只改 gate 内部状态：`self.state["incumbent"] = {"label":"基线(回滚)","pnls":[]}`、`rollbacks += 1`、`_save()`——**从不回写 `WeightLearner.weights`**。
- `weight_learning.py` 已有 `rollback_to_base()`（140–143 行：`self.weights = dict(self.base_weights); self._save()`），但**无人调用**。
- `maybe_evolve()`（103–138 行）只处理 `promote`/`reject`/`shadow`，`_observe()` 触发的 `_rollback` 结果是"gate 显示已回滚、weights 却仍是退化后的候选"——进化层嘴上回滚、实际不回滚。

### 实施方案（函数级）
1. `evolution_gate.EvolutionGate.__init__` 增加可选回调：

```python
def __init__(self, name, path=..., ..., on_rollback=None):
    ...
    self.on_rollback = on_rollback

def _rollback(self):
    self.state["rollbacks"] += 1
    old_label = self.state["incumbent"]["label"]
    self.state["incumbent"] = {"label": "基线(回滚)", "pnls": []}
    self.state["live_batches"] = []
    self._log("rollback", f"{old_label} 退化，回滚至保守基线")
    self._save()
    if self.on_rollback:          # 新增：回写真实权重
        self.on_rollback()
```

2. `weight_learning.WeightLearner.__init__` 绑定回调：

```python
self.gate = EvolutionGate("评分权重层", "weight_gate.json",
                          min_shadow_samples=gate_min_shadow,
                          min_edge=gate_min_edge,
                          on_rollback=self.rollback_to_base)
```

3. `rollback_to_base()` 补一条 `print`/`notify`（回滚是重要事件，可审计）：

```python
def rollback_to_base(self):
    self.weights = dict(self.base_weights)
    self.version = 0          # 回滚即回到基线版本
    self._save()
    print("⛔ 权重层验证门触发回滚 → 已回写 weights=base_weights 并持久化")
```

### 防过拟合验证（纯工程修复，离线单测）
1. 单测 A：构造 gate 使 `_observe` 触发 `_rollback`（喂连续退化 pnl），断言 `wl.weights == wl.base_weights` 且 `weight_state.json` 里 `weights` 已写回基线、`version == 0`。
2. 单测 B：`rollback_to_base()` 被调后，下一次 `maybe_evolve` 用回滚后 weights 重算 `ic_inc`（断言现役已是基线，不再是退化候选）。
3. 幂等：连续多次 rollback 后 weights 仍等于 base_weights（不越滚越偏）。

### 预期收益与风险
- 收益：修复"上线退化不回滚"的致命缺口——否则权重层会被噪声进化劫持、风控参数越改越坏且无兜底。
- 风险：回调在 `_save` 后执行，若 `rollback_to_base` 内部 `_save` 抛异常会向上冒（`weight_learning._save` 已有 try/except 兜底，不会崩）；`version=0` 回写可能与"未进化"状态混淆——已在状态打印里注明回滚。

### 回滚方式
- 去掉 `on_rollback` 绑定即恢复"gate 内部回滚、weights 不回写"原状（不建议）；`rollback_to_base` 保留不调用无副作用。

---

## R2-2（RES-16）WeightLearner 候选生成与 IC 评估按时间切分（消除样本内自证）

### 现状证据
- `weight_learning.py` `maybe_evolve()`（103–138 行）：`_factor_contribution` 生成候选、`spearman` 算 `ic_inc/ic_cand` **全部用同一批 `self.records`**——候选"在哪些样本上生成"就在"哪些样本上证明自己好"，是样本内自证（过拟合）。
- `records` 无时间戳，无法切分。
- `min_samples=40` 全局门槛，未区分训练/验证段。

### 实施方案（函数级）
1. `record()` 加时间戳：

```python
def record(self, scores, pnl):
    self.records.append({"scores": dict(scores), "pnl": float(pnl),
                         "ts": time.time()})
    ...
```

2. `maybe_evolve()` 按时间切分（训练段生成候选、验证段算 IC）：

```python
def maybe_evolve(self):
    recs = sorted(self.records, key=lambda r: r.get("ts", 0))
    n = len(recs)
    n_train = int(n * 0.7)
    train, valid = recs[:n_train], recs[n_train:]
    # 候选生成需要 min_samples，验证段需要 gate_min_shadow
    if len(train) < self.min_samples or len(valid) < self.gate_min_shadow:
        return {"action": "wait", "n": n, "need": self.min_samples,
                "need_valid": self.gate_min_shadow}

    # 贡献评估只用训练段
    contrib = {k: self._factor_contribution(k, train) for k in self.weights}
    pos = {k: w for k, w in self.weights.items() if contrib[k] > self.min_positive_contrib}
    if not pos or set(pos.keys()) == set(self.weights.keys()):
        return {"action": "no_change", "contrib": {k: round(v,4) for k,v in contrib.items()}}
    cand = {k: pos.get(k, 0.0) for k in self.weights}
    tot = sum(cand.values())
    cand = {k: v/tot for k, v in cand.items()}

    # IC 只在验证段算（候选在训练段生成，验证段是样本外）
    vpnls = [r["pnl"] for r in valid]
    v_inc = [self._composite(r["scores"], self.weights) for r in valid]
    v_cand = [self._composite(r["scores"], cand) for r in valid]
    ic_inc = spearman(v_inc, vpnls)
    ic_cand = spearman(v_cand, vpnls)
    # 后续 gate propose/record_incumbent/record_shadow 不变
    ...
```

3. `_factor_contribution(key, records=None)`：默认 `records or self.records`，接受训练段切片。

### 防过拟合验证
1. 单测：mock 一批 records（含 ts），断言候选只用前 70% 生成、IC 只用后 30% 计算（可通过给训练/验证段人工构造相反贡献来验证：训练段因子 A 正贡献、验证段因子 A 无贡献 → 候选 IC 不显著 → 不 promote）。
2. 样本外自检：真实数据上，promote 后记录"验证段 IC"与"后续新增样本 IC"的衰减；断言 promote 后的现役权重在**尚未参与生成的更新样本**上 IC 不再显著下降（对比改造前样本内 IC 虚高）。
3. 门槛回归：断言样本不足（train<40 或 valid<20）时返回 wait、不进化（"不进化也是进化"）。

### 预期收益与风险
- 收益：候选必须通过样本外验证段才上线，消除"自证回声"，权重进化从"拟合噪声"变"真实贡献"。
- 风险：切分后需要更多样本才进化（约 ≥60 条），进化变慢——符合"宁缺毋滥"；旧 records 无 ts（默认 0）排序退化为原顺序，向后兼容。

### 回滚方式
- 去掉切分、恢复 `recs = self.records` 全量即回退；`ts` 字段为增量。

---

## R2-3（RES-7）经验验证只追踪"本笔实际采纳"（消除全量 validate 回声）

### 现状证据
- `directional_trader.py` `monitor()`（277–279 行）：`trusted = self.exp_bank.trusted(symbol=base); for tl in trusted: self.exp_bank.validate(tl["id"], closed["pnl"])`——**全量 validate 该币所有 trusted 经验**，与本笔是否采纳无关（回声室：经验多→被"验证"多→更容易 trusted）。
- `_ExpAdapter.relevant()`（57–70 行）返回 dict **不含 `id`**，决策时无法记录采纳了哪条。
- `self_evolving_trader.decide()`（23–68 行）按 category 计数触发 stop_adj/限价/拒绝，但未返回触发决策的经验 id。

### 实施方案（函数级）
1. `_ExpAdapter.relevant()` 返回 id：

```python
def relevant(self, symbol=None, category=None):
    out = [{"id": l["id"], "symbol": l["symbol"], "category": l["category"],
            "lesson": l["content"]}
           for l in (self.bank.trusted(symbol) + self.bank.discarded(symbol))]
    if category:
        out = [l for l in out if l["category"] == category]
    return out
```

2. `SelfEvolvingTrader.decide()` 收集实际采纳的 trusted 经验 id：

```python
decision = {"trade": True, "reason": [], "stop_adj": 0, "size_factor": 1.0,
            "adopted_lesson_ids": []}
relevant = self.bank.relevant(symbol=symbol)
trusted_ids_by_cat = {}
for l in relevant:
    if l.get("id"):
        trusted_ids_by_cat.setdefault(l["category"], []).append(l["id"])
...
if cats.get("止损", 0) >= 2:
    decision["stop_adj"] = 0.2
    decision["adopted_lesson_ids"] += trusted_ids_by_cat.get("止损", [])
if cats.get("入场时机", 0) >= 1:
    decision["adopted_lesson_ids"] += trusted_ids_by_cat.get("入场时机", [])
if cats.get("信号", 0) >= 3:
    decision["adopted_lesson_ids"] += trusted_ids_by_cat.get("信号", [])
    decision["trade"] = False
    return decision
```

3. `trade_journal.log_entry()` 增加 `adopted_lesson_ids=None` 字段并写入 trade dict。

4. `directional_trader.open_position()` 传采纳 id：`tid = self.journal.log_entry(..., adopted_lesson_ids=dec.get("adopted_lesson_ids", []))`（`dec` 已在 scan_signals 里从 `self.evolver.decide` 取得并传入 open_position）。

5. `monitor()` 平仓后**只 validate 本笔采纳的**：

```python
# 替换原来的"全量 trusted validate"
for lid in t.get("adopted_lesson_ids") or []:
    self.exp_bank.validate(lid, closed["pnl"])
```

### 防过拟合验证
1. 单测：构造"该币 5 条 trusted，本笔只采纳 1 条"→ 断言平仓只 validate 那 1 条、其余 4 条 adoptions 计数不变（防回声）。
2. 单测：决策触发 stop_adj/限价/拒绝分支 → 断言 `adopted_lesson_ids` 精确等于对应 category 的 trusted id，不含 discarded（discarded 不参与 validate）。
3. 长期对账：跑 N 笔后，断言"每条经验 adoptions 增长速率 ∝ 实际被采纳频率"，而非"∝ 该币 trusted 总数"（消除规模效应）。

### 预期收益与风险
- 收益：经验验证从"规模回声"变"因果采纳"，真正用"本笔实际用了这条教训"的结果来奖惩它，打破经验库回声室。
- 风险：采纳 id 追踪依赖 `_ExpAdapter` 返回 id 与 `decide` 分支收集的一致性；若某分支漏收集，只是"少验证"（保守方向），不误杀。`decide` 被 `trading_main` 也实例化但未调用，改动不影响套利路径。

### 回滚方式
- `monitor()` 改回全量 `trusted` validate 即回退；`adopted_lesson_ids` 字段为增量，旧 journal 记录无此字段 → 平仓跳过 validate（保守）。

---

## R2-4（R3-3）进程 watchdog + heartbeat 文件 + 崩溃自动重启

### 现状证据
- `directional_trader.run()`（345–380 行）与 `trading_main.run()`（472–540 行）都是 `while True` 主循环，无心跳文件、无守护；`realtime_okx.py` 已有"监督线程"模式（内部自愈，仅限 WebSocket）。
- 环境为 macOS（存在 `~/Library/LaunchAgents`、`data/com.okx.collect.plist`），已有 launchd 使用先例。
- 进程"卡死但不退出"（如 fetch 挂起）时，`KeepAlive` 不会触发重启——需要独立心跳超时判定。

### 实施方案（函数级，选 launchd）
1. 心跳文件（两进程主循环每 tick 写 epoch）：

```python
# directional_trader.run() 循环内（2s/tick）
def _heartbeat(self):
    with open("heartbeat_directional.txt", "w") as f:
        f.write(str(time.time()))

# trading_main.run() 循环内（60s/tick）写 heartbeat_arb.txt
```

2. `watchdog.py`（独立脚本，launchd 每分钟跑）：

```python
TIMEOUT = {"heartbeat_directional.txt": 30,   # 方向仓 2s 循环 → 30s 无心跳=卡死
           "heartbeat_arb.txt": 300}          # 套利 60s 循环 → 300s 无心跳=卡死
PROCS = {"heartbeat_directional.txt": "directional_trader.py",
         "heartbeat_arb.txt": "trading_main.py"}

def check():
    now = time.time()
    for hb, proc in PROCS.items():
        try:
            ts = float(open(hb).read().strip())
        except Exception:
            ts = 0
        if now - ts > TIMEOUT[hb] and _is_running(proc):
            notify(f"⚠️ {proc} 心跳超时 {now-ts:.0f}s，判定卡死，kill 重启")
            os.system(f"pkill -f {proc}")      # 触发 launchd KeepAlive 自动重启

def _is_running(proc):
    return os.system(f"pgrep -f {proc} >/dev/null 2>&1") == 0
```

3. launchd plist（`~/Library/LaunchAgents/com.crypto.<proc>.plist`）各进程：

```xml
<key>KeepAlive</key><true/>     <!-- 进程退出/被杀自动重启 -->
<key>ProgramArguments</key><array>
  <string>/usr/bin/python3</string>
  <string>/path/to/directional_trader.py</string>
</array>
```

4. watchdog 自身用第二个 launchd `StartInterval=60` 定时触发；告警复用 `lark` CLI（`notify`）。

### 防过拟合验证（纯运维，无过拟合）
1. 单测：mock 心跳文件写旧时间戳 + `pgrep` 返回 0 → 断言 watchdog 调 `pkill` 且 `notify` 一次；时间戳新鲜或进程已退 → 不动作。
2. 沙盘：手动 `kill -STOP`（挂起不退出）方向仓进程，60s 后断言 watchdog 告警并 kill、launchd 重启恢复；`kill -9`（退出）断言 KeepAlive 直接重启。
3. 阈值敏感性：TIMEOUT 取值为主循环周期 ×10（方向 20s→取 30s 留余量、套利 600s→取 300s 更灵敏），非从数据拟合，无需样本外。

### 预期收益与风险
- 收益：僵尸进程（fetch 挂起/死锁）能被心跳超时识别并重启，弥补 KeepAlive 只看退出的盲区；崩溃自动拉起 + 飞书告警。
- 风险：pkill -f 可能误杀同名进程——用完整脚本名匹配 + 单实例锁（R1-12 临界区锁）兜底；心跳写文件失败（磁盘满）会误判卡死 → watchdog 告警后重启，最坏多一次重启。

### 回滚方式
- 卸载 launchd plist + 删 watchdog.py 即回退；主循环 `_heartbeat()` 调用删除不影响交易逻辑。

---

## R2-5（R3-1）止盈挂交易所侧（与 R1-1 畸形单验证联动，含降级路径）

### 现状证据
- `directional_trader.open_position()`（198–208 行）只挂**止损**条件单；止盈只靠本地 `monitor()` tick 检查（253–254 行 `hit_exit`），进程崩溃/断网则止盈失效（止损有交易所兜底、止盈没有）。
- R1-1 已标注"现有 conditional 停损单写法可能产出畸形单，需沙盘先验证"——TP 单同写法有同风险，须与 R1-1 联动验证后再上。

### 实施方案（函数级，两档 + 降级）
**方案 A（独立 TP 条件单，与止损对称，首选）**：

```python
# open_position() 挂止损后对称挂止盈（long 仓平=卖、short 仓平=买）
tp_side = "sell" if sig["dir"] == "long" else "buy"
self.exchange.create_order(
    sym, "market", tp_side, qty, None,
    params={"posSide": sig["dir"], "reduceOnly": True,
            "ordType": "conditional", "triggerPrice": sig["tp"]})
```

- 撤单：R1-1 修订后的 `_cancel_stop_orders` 取"该 instId 全部 pending algo 单"，天然覆盖 TP+SL，无需新增撤单逻辑。

**方案 B（OKX attachAlgoOrds 附着 TP/SL，更原子，备选）**：开主仓单时 `params={"attachAlgoOrds": {"tp": {"triggerPx": sig["tp"], "ordPx": "-1", "triggerPxType": "last"}, "sl": {...}}}`。**未完成**：需核实 ccxt OKX 对 `attachAlgoOrds` 的参数透传，沙盘验证优先 A。

**降级路径（沙盘验证不通过时）**：
1. A 方案 conditional TP 若产出畸形单 → 改 OKX 原生 `private_post_trade_order_algo` 显式构造 `{ordType:"conditional", side:tp_side, posSide, sz, reduceOnly:"true", triggerPx:tp, triggerPxType:"last", ordPx:"-1"}`（与 R1-1 止损同款原生构造）。
2. 若原生构造也不稳 → **保留现状**（只交易所侧止损 + 本地 monitor 止盈），并明确标注"止盈依赖进程存活"为已知残余风险。

### 防过拟合验证（执行类，沙盘为主）
1. 沙盘验证（与 R1-1 联动，两者都通过才上正式方案）：
   - `orders-algo-pending` 能查到 TP 单、`triggerPrice/posSide/reduceOnly/ordType` 正确。
   - 手动触发：TP 单按 reduceOnly 成交、方向正确（long 平=卖）、成交后 TP 单消失。
   - 平仓后（本地止盈或止损触发）：`_cancel_stop_orders` 把 SL+TP 都撤净，无残留。
2. 单测：`open_position` 断言挂 TP 单参数正确；`_cancel_stop_orders` 断言覆盖 TP 单。
3. 零过拟合：TP 触发价直接用 `sig["tp"]`（2:1 盈亏比策略参数，非从历史拟合），无参数需校准。

### 预期收益与风险
- 收益：止盈也获得交易所侧硬兜底（进程崩溃仍能按 2:1 目标价止盈），与止损对称，消除"止损有兜底、止盈靠存活"的不对称。
- 风险：TP 单同样有畸形单风险 → 必须沙盘验证通过才上（与 R1-1 联动，有明确降级路径）；TP 触发后本地 `hit_exit` 与交易所 TP 可能双触发 → 依赖 reduceOnly 兜底（最多减到 0，不反手）。

### 回滚方式
- 删掉 TP 挂单代码即回到"只挂止损 + 本地止盈"；`_cancel_stop_orders` 无需改动（本就取全部 algo 单）。

---

## R2-6（RES-17）deep_review 补 atr_value / signal_price（复盘退化修复）

### 现状证据
- `review_engine.deep_review()`（20–111 行）支持 `atr_value` / `signal_price` / `post_exit_reverse` 参数，用于"止损是否太紧被插针""是否追高"等复盘维度。
- `directional_trader.monitor()`（271 行）`report = deep_review(closed)` **三个参数都没传** → 止损质量/入场时机两个复盘维度退化（只按 pnl 产出泛化教训）。
- `trade_journal.log_entry()` 未存 `atr_value` / `signal_price`；`open_position` 有 `sig["atr"]`、`sig["entry"]` 可用。

### 实施方案（函数级）
1. `trade_journal.log_entry()` 增加字段（`atr_value=None, signal_price=None`，写入 trade dict）。
2. `directional_trader.open_position()` 传入：`signal_price=sig["entry"]`（信号触发参考价）、`atr_value=sig["atr"]`。
3. `monitor()` 平仓后传参：

```python
report = deep_review(closed,
                     atr_value=t.get("atr_value"),
                     signal_price=t.get("signal_price"))
```

   （可选补 `post_exit_reverse`：平仓后取当前价判断是否反转被插针——**未完成**：需在平仓后再取一次价格，改动略大，本期只补必传两项。）

### 防过拟合验证（纯复盘，离线单测）
1. 单测：构造一笔"止损距离 < 1×ATR 且亏损"的交易 → 断言 `deep_review` 产出"止损太紧"类别教训（传参后不再缺失）；不传参对照断言无该维度教训。
2. 单测：构造"入场价 > 信号价 3%" → 断言产出"追高"教训。
3. 回归：旧 journal 记录无 `atr_value/signal_price` → `deep_review` 收到 None，行为与现状一致（不崩）。

### 预期收益与风险
- 收益：复盘引擎恢复"止损质量/入场时机"两个维度，教训更具体（带 ATR/偏差数值），经验库质量提升，间接喂好 R2-3 的采纳验证。
- 风险：几乎零风险（一行补参 + 字段增量）；`signal_price=sig["entry"]` 是信号价近似（真实信号价是日线收盘），已标注。

### 回滚方式
- `monitor()` 改回 `deep_review(closed)` 即回退；`log_entry` 新字段为增量。

---

## 排序小结

| 排序 | 编号 | 立项 | 类型 | 一句话 |
|---|---|---|---|---|
| 1 | R2-1 | RES-6 | 风控/进化安全 | gate 回滚回写 weights=base_weights + 单测 |
| 2 | R2-2 | RES-16 | 防过拟合 | 候选生成(train)/IC 验证(valid)按时间切分 |
| 3 | R2-3 | RES-7 | 防回声 | 只 validate 本笔实际采纳的 trusted 经验 id |
| 4 | R2-4 | R3-3 | 运维可靠性 | launchd KeepAlive + 心跳文件 + watchdog 杀僵尸进程 |
| 5 | R2-5 | R3-1 | 执行对称 | 止盈挂交易所侧，与 R1-1 联动沙盘验证 + 降级路径 |
| 6 | R2-6 | RES-17 | 复盘质量 | deep_review 补 atr_value/signal_price |

未立项：RES-15（批次2改同文件，延后）；RES-8/11/12/13/14/18/19/20（上轮已放弃）。

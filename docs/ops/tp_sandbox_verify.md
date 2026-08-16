# R2-5 止盈挂交易所侧 — 沙盘验证步骤（开启 FLAG_ENABLE_EXCHANGE_TP 前必经）

> 背景：R1-1 的止损条件单存在"畸形单"风险（ccxt 的 type=market+ordType=conditional+triggerPrice
> 组合可能产出缺 tpTriggerPx/slTriggerPx 结构的单）。TP 单同款风险，必须验证通过才开启。
> 验证目标：TP/SL 单在 OKX 模拟盘真实挂上、字段正确、按 reduceOnly 成交、平仓后撤净。

## 验证清单（逐项打勾）

### 1. 静态挂单验证
- [ ] 临时开启 FLAG_ENABLE_EXCHANGE_TP=True，模拟盘跑一次 `directional_trader.py --once`（或手动调 open_position）
- [ ] `GET /api/v5/trade/orders-algo-pending?instId=BTC-USDT-SWAP` 能查到 TP 单
- [ ] 字段核对：`ordType=conditional`、`triggerPx`（非 triggerPrice）、`posSide` 与主仓一致、
      `reduceOnly=true`、`sz` 正确、`side` 为平仓方向（long→sell）

### 2. 触发成交验证
- [ ] 手动把价格推到 TP 位（或用小仓实测），确认 TP 按 reduceOnly 成交、方向正确
- [ ] 成交后该 algo 单从 pending 列表消失
- [ ] 本地 monitor 与交易所 TP 双触发时：reduceOnly 兜底（最多减到 0，不反手）

### 3. 撤单验证（与 R1-1 联动）
- [ ] 本地平仓（止损/止盈/强平）后，`_cancel_stop_orders` 把 TP+SL 全部撤净（pending 列表为空）
- [ ] 开新仓前，旧 instId 无残留 algo 单

### 4. 降级路径验证（当 attachAlgoOrds 不可用时）
- [ ] attachAlgoOrds 透传测试：主仓单带 attachAlgoOrds 参数下单，观察 ccxt 是否透传、
      交易所是否接受、orders-algo-pending 是否出现 TP+SL
- [ ] 若 attach 不可用 → 原生 private_post_trade_order_algo 显式构造是否成功（字段见 _place_tp 降级代码）

## 判定
- 全部通过 → 把 FLAG_ENABLE_EXCHANGE_TP 置 True 并记录验证日期。
- 任一失败 → 保持 False（现状：只交易所侧止损 + 本地 monitor 止盈），
  在 docs/reports/optimization_notes.md 标注"止盈依赖进程存活"为已知残余风险。

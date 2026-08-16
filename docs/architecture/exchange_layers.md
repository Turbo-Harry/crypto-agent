# 交易所访问分层架构（EXCHANGE LAYERS）

> 目标：策略代码与具体交易所彻底解耦，去掉 ccxt 依赖，OKX 原生 REST 直连。

## 分层

```
┌─────────────────────────────────────────────────────────┐
│ 应用/策略层  directional_trader.py · trading_main.py     │
│             trading_agent.py · funding_arb.py           │
│             ── 只 import exchange.base.ExchangeAdapter   │
├─────────────────────────────────────────────────────────┤
│ 接口层       exchange/base.py                           │
│             ExchangeAdapter（ABC，统一交易语义）          │
│             + 异常归一：ExchangeError                    │
├─────────────────────────────────────────────────────────┤
│ 适配层       exchange/okx_adapter.py                    │
│             OKXAdapter —— 单位换算（ctVal/lotSz/minSz）、 │
│             场所探测（swap/spot）、响应翻译               │
├─────────────────────────────────────────────────────────┤
│ 传输层       exchange/transport.py                      │
│             OKXTransport —— HTTP + HMAC-SHA256 签名、     │
│             模拟盘 header、限速、429 退避、错误归一        │
├─────────────────────────────────────────────────────────┤
│ 领域模型     exchange/models.py                         │
│             Instrument/Candle/BalanceInfo/PositionInfo/  │
│             OrderResult + floor_to_lot/lot_decimals      │
├─────────────────────────────────────────────────────────┤
│ 测试替身     exchange/fake_adapter.py                   │
│             FakeAdapter —— 内存交易所，单测注入           │
└─────────────────────────────────────────────────────────┘
```

依赖方向单向向下：策略 → 接口 → 适配 → 传输。任何一层都不反向 import。

## 关键设计决策

1. **单位统一在适配层**：策略层永远说"基础币数量、USDT 价格"。
   张数换算（qty ÷ ct_val）、lotSz 对齐（向下取整，绝不超发）、minSz 校验
   全部在 OKXAdapter 内完成。策略层不出现 `ctVal`、`lotSz` 字样。

2. **场所探测**：`venue_for(base)` 优先合约、回退现货。ANTHROPIC 这类
   "只有永续"的代币自动走合约路径（杠杆+交易所侧止损）；XNVDA 这类
   "只有现货"的代币自动走现货路径（仅做多、本地止损）。

3. **失败语义两级**：
   - 网络/签名/限频 → 抛 `ExchangeError`（fail-closed，调用方 try/except）
   - 业务拒绝（余额不足/最小下单量）→ 返回 `OrderResult(ok=False, message)`
   业务层据此回滚 claim、跳过，不误判为系统故障。

4. **沙盘实测沉淀的端点要点**（见 okx_adapter.py 注释）：
   - 条件止损必须 `slTriggerPx` 系列（`triggerPx` 报 50015）
   - 止盈必须 `tpTriggerPx` 系列
   - `orders-algo-pending` 必须带 `ordType`（否则 51000）
   - funding bills 金额在 `balChg` 字段

5. **可测试性**：`DirectionalTrader(exchange=FakeAdapter())` 离线跑通
   信号→开仓→止损→平仓全链路（`test_exchange_layers.py`，18 断言）。

## 换交易所的成本

新增 BinanceAdapter / BybitAdapter，实现 `ExchangeAdapter` 接口即可；
策略层零改动。mock/回测可直接用 FakeAdapter。

## 验证记录

- 2026-08-16 沙盘实测：开多 0.01 ANTHROPIC → 挂 slTriggerPx 止损 →
  pending 查询 → 撤单 → reduceOnly 平仓 → 持仓归零，全链路 ✅
- `test_exchange_layers.py`：18 通过 0 失败（含最小下单量拒绝、账本释放）
- 活体 directional_trader 重启后心跳正常、3 笔 ETH 持仓衔接 ✅

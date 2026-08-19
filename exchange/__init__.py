"""
exchange 包 — 交易所访问分层架构
================================

分层（自上而下，依赖单向向下）：

  策略/应用层   directional_trader.py / trading_main.py
      │  只依赖抽象接口 ExchangeAdapter（不 import 任何交易所 SDK）
      ▼
  接口层        exchange.base.ExchangeAdapter（ABC，定义统一交易语义）
      ▲
      │  实现
  适配层        exchange.okx_adapter.OKXAdapter（合约换算/lotSize/场所探测）
      │  只做 HTTP 与签名
      ▼
  传输层        exchange.transport.OKXTransport（OKX 原生 REST，HMAC 签名、
                模拟盘 header、限速、错误归一）
      │
  领域模型      exchange.models（Instrument/Candle/PositionInfo/…dataclass）

收益：
  1. 策略代码与具体交易所解耦 —— 换 Binance/Bybit 只新增适配器。
  2. 单测可注入 exchange.fake_adapter.FakeAdapter（内存假交易所），无需网络。
  3. 签名/限速/错误处理集中在传输层，业务层不出现 HTTP 细节。
"""
from exchange.base import ExchangeAdapter
from exchange.okx_adapter import OKXAdapter
from exchange.models import (Instrument, Candle, TickerInfo, BalanceInfo,
                             PositionInfo, OrderResult)

__all__ = ["ExchangeAdapter", "OKXAdapter",
           "Instrument", "Candle", "TickerInfo", "BalanceInfo",
           "PositionInfo", "OrderResult"]

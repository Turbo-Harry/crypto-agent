"""
资金费率套利 — OKX 模拟盘（demo trading）。
策略：现货多头 + 永续空头 1:1 对冲，赚资金费率（delta 中性，不赌方向）。

用法：
  python3 funding_arb.py status     # 查看账户 + 资金费率 + 套利机会
  python3 funding_arb.py open BTC/USDT:USDT 100   # 对冲开仓（现货多100U + 合约空100U）
  python3 funding_arb.py monitor    # 持续监控净值 + 资金费
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.okx_adapter import OKXAdapter

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "okx_config.json")


def connect():
    """连接 OKX 模拟盘（原生 REST，无 ccxt）。"""
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"], sandbox=True)


# 对冲不需要杠杆：统一 1x 逐仓（高杠杆只降低保证金率、抬高爆仓风险 — OP-3）
LEVERAGE_MAP = {"BTC": 1, "ETH": 1, "SOL": 1, "XRP": 1, "DOGE": 1, "ADA": 1}


def setup_position(exchange, symbol):
    """开仓前配置：逐仓模式 + 合理杠杆（long/short 双向）。"""
    base = symbol.split("/")[0]
    leverage = LEVERAGE_MAP.get(base, 2)
    for side in ["long", "short"]:
        try:
            exchange.set_leverage(f"{base}-USDT-SWAP", leverage, side)
        except Exception as e:
            print(f"  {base} {side} 杠杆设置失败: {e}")
    print(f"  已配置 {base}: 逐仓 + {leverage}x 杠杆（long/short）")
    return leverage


def show_status(exchange):
    """展示账户 + 资金费率 + 套利年化。"""
    print("=" * 60)
    print("OKX 模拟盘 — 资金费率套利状态")
    print("=" * 60)

    # 账户余额
    bal = exchange.fetch_balance()
    print(f"\n账户 USDT: 可用 {bal.usdt_free:.2f} | 总 {bal.usdt_total:.2f}（总权益 {bal.total_eq:.2f}）")

    # 资金费率
    print("\n主流币资金费率 + 套利年化:")
    symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
               "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT"]
    print(f"  {'币':<8} {'费率/8h':>10} {'年化':>10} {'方向':>12}")
    for sym in symbols:
        base = sym.split("/")[0]
        try:
            rate = exchange.fetch_funding_rate(f"{base}-USDT-SWAP")
            annual = rate * 3 * 365 * 100
            direction = "现货多+合约空" if rate > 0 else "现货空+合约多"
            print(f"  {base:<8} {rate*100:>9.4f}% {annual:>9.1f}% {direction:>12}")
        except Exception:
            print(f"  {base}: 查询失败")

    # 当前持仓
    try:
        positions = [p for p in exchange.fetch_positions() if p.base_qty > 0]
        if positions:
            print(f"\n当前合约持仓 {len(positions)} 个:")
            for p in positions:
                print(f"  {p.inst_id} {p.side} {p.contracts} 张")
        else:
            print("\n当前无合约持仓")
    except Exception:
        pass


def open_hedge(exchange, symbol, notional_usdt):
    """对冲开仓：方向由实时资金费率决定（正费率=现货多+合约空）。
    开仓前先配置逐仓+合理杠杆。"""
    base = symbol.split("/")[0]
    # 0. 配置逐仓 + 合理杠杆
    setup_position(exchange, symbol)
    # 方向：读实时费率（修复：此前硬编码"现货多+合约空"，负费率时反向付费）
    try:
        rate = exchange.fetch_funding_rate(f"{base}-USDT-SWAP")
    except Exception:
        rate = 0
    # 查现货价格
    price = exchange.fetch_ticker_last(f"{base}-USDT")
    amount = notional_usdt / price
    annual = rate * 3 * 365
    print(f"\n对冲开仓 {base}: 名义 {notional_usdt} USDT ≈ {amount:.6f} {base} @ {price:.2f} "
          f"(费率 {rate*100:+.4f}%/8h, 年化 {annual*100:+.1f}%)")

    if rate >= 0:
        # 1. 现货买入
        try:
            res = exchange.place_market_order(f"{base}-USDT", "buy", amount, venue="spot")
            if not res.ok:
                raise RuntimeError(res.message)
            print(f"  现货买入: {amount:.6f} {base} 成交 @ {price}")
        except Exception as e:
            print(f"  现货买入失败: {e}")
            return
        # 2. 合约做空（等额，posSide=short）
        try:
            res = exchange.place_market_order(f"{base}-USDT-SWAP", "sell", amount,
                                              venue="swap", pos_side="short")
            if not res.ok:
                raise RuntimeError(res.message)
            print(f"  合约做空: {amount:.6f} {base} 成交 @ {price}")
        except Exception as e:
            print(f"  合约做空失败: {e}")
    else:
        # 负费率：现货空腿需保证金/借币账户（当前未配置）→ 整体拒绝，不开单腿（R1-11）
        print(f"⛔ 负费率 {base}: 现货空腿需保证金账户（未配置），整体拒绝开仓，不开裸单腿")
        return


def monitor(exchange, interval=3600):
    """持续监控净值 + 资金费。"""
    print("开始监控（Ctrl+C 停止）")
    while True:
        show_status(exchange)
        print(f"\n下次更新: {interval} 秒后")
        time.sleep(interval)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    ex = connect()
    if cmd == "status":
        show_status(ex)
    elif cmd == "open":
        symbol = sys.argv[2] if len(sys.argv) > 2 else "BTC/USDT:USDT"
        notional = float(sys.argv[3]) if len(sys.argv) > 3 else 100
        open_hedge(ex, symbol, notional)
    elif cmd == "monitor":
        monitor(ex)

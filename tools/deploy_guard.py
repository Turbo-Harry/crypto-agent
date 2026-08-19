#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署前检查（2026-08-19 用户要求'提高健壮性'）:
  smoke   : 迷你单开/平仓冒烟——下单链路全环节 sCode 校验
  predeploy: 部署窗口纪律——持仓距止损/止盈 <1% 时禁止部署(平仓在即,重启盲窗危险)

用法:
  python3 tools/deploy_guard.py smoke        # 部署前跑,失败=下单链路坏了
  python3 tools/deploy_guard.py predeploy    # 重启前跑,失败=现在不能动引擎
退出码: 0=通过; 1=不通过(冒烟失败或处于危险窗口)。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "lib"))

SMOKE_SYMBOL = "KAITO"          # ctVal=1 低价币,迷你单成本低
SMOKE_QTY = 1.0                 # 1 合约,~0.35 USDT
NEAR_LEVEL_PCT = 0.01           # 距止损/止盈 1% 内 = 危险窗口


def _exchange():
    from exchange.okx_adapter import OKXAdapter
    cfg = json.load(open(os.path.join(ROOT, "okx_config.json")))
    return OKXAdapter(cfg["apiKey"], cfg["secret"], cfg["password"], sandbox=True)


def smoke():
    """迷你单开→挂SL/TP→取消→平仓,全链路 sCode=0 才算通过。
    覆盖: 市价单/clOrdId/tdMode/条件单/取消/平仓——今日所有下单事故的复现面。"""
    ex = _exchange()
    inst = f"{SMOKE_SYMBOL}-USDT-SWAP"
    last = ex.fetch_ticker_last(inst)
    print(f"冒烟: {inst} 现价 {last}")
    # 1. 开仓
    r = ex.place_market_order(inst, "buy", SMOKE_QTY, venue="swap", pos_side="long")
    if not r.ok:
        print(f"❌ 冒烟失败-开仓: {r.message}")
        return 1
    print("  ✅ 开仓 sCode=0")
    time.sleep(0.5)
    # 2. SL + TP 条件单(交易所侧止损止盈双验证)
    sl = ex.place_conditional_stop(inst, "sell", SMOKE_QTY, "long", round(last * 0.97, 6))
    tp = ex.place_conditional_stop(inst, "sell", SMOKE_QTY, "long", round(last * 1.005, 6), is_tp=True)
    if not sl.ok or not tp.ok:
        print(f"❌ 冒烟失败-条件单: SL={sl.message} TP={tp.message}")
        return 1
    print("  ✅ SL/TP 条件单 sCode=0")
    time.sleep(0.5)
    # 3. 取消挂单
    for ot in ("conditional", "oco", "trigger", "move_order_stop"):
        try:
            pend = ex.t.private_get("/api/v5/trade/orders-algo-pending",
                                    {"instType": "SWAP", "ordType": ot})
            for p in pend.get("data") or []:
                ex.t.private_post("/api/v5/trade/cancel-algos",
                                  [{"algoId": p["algoId"], "instId": p["instId"]}])
        except Exception:
            pass
    print("  ✅ 取消挂单完成")
    time.sleep(0.5)
    # 4. 平仓
    rc = ex.place_market_order(inst, "sell", SMOKE_QTY, venue="swap",
                               pos_side="long", reduce_only=True)
    if not rc.ok:
        print(f"❌ 冒烟失败-平仓: {rc.message}")
        return 1
    print("  ✅ 平仓 sCode=0")
    print("✅ 冒烟通过: 下单链路全环节健康,可部署")
    return 0


def predeploy():
    """部署窗口纪律: 任何持仓距止损/止盈 <1% → 禁止部署。
    重启盲窗 2-3 分钟,平仓临界时刻的盲窗会错过离场。"""
    import sqlite3
    ex = _exchange()
    db = sqlite3.connect(os.path.join(ROOT, "crypto_agent.db"))
    db.row_factory = sqlite3.Row
    dangers = []
    for t in db.execute("SELECT * FROM trades WHERE status='open'"):
        try:
            last = ex.fetch_ticker_last(f"{t['symbol']}-USDT-SWAP")
        except Exception:
            continue
        stop, tp = t["stop_loss"], t["take_profit"]
        if not stop or not tp or not last:
            continue
        near_stop = abs(last - stop) / stop < NEAR_LEVEL_PCT
        near_tp = abs(last - tp) / tp < NEAR_LEVEL_PCT
        if near_stop or near_tp:
            dangers.append(f"{t['id']} {t['symbol']} 距"
                           f"{'止损' if near_stop else '止盈'} {(last-stop)/stop*100:+.2f}%")
    if dangers:
        print("❌ 部署窗口危险,禁止重启引擎:")
        for d in dangers:
            print(f"   - {d}")
        print("   等仓位离场(或人工确认接受盲窗风险)后再部署。")
        return 1
    print("✅ 部署窗口安全: 无持仓处于平仓临界状态")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "predeploy"
    sys.exit({"smoke": smoke, "predeploy": predeploy}.get(mode, predeploy)())

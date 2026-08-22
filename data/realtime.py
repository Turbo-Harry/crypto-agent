# -*- coding: utf-8 -*-
"""实时行情工厂(2026-08-23): 按 config.REALTIME_BACKEND 选择后端,
两个模块导出同名 OKXRealtime、同接口,切换只需改 config 一处。"""


def make_realtime(symbols=None, fetch_candles=None):
    import config
    backend = getattr(config, "REALTIME_BACKEND", "okx")
    if backend == "ccxtpro":
        from data.realtime_ccxtpro import OKXRealtime
    else:
        from data.realtime_okx import OKXRealtime
    return OKXRealtime(symbols, fetch_candles=fetch_candles)

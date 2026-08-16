"""
实验4 — 回踩确认的成交价模型测试（诚实评估）。
真实场景：突破后在箱体上沿上方挂限价单，回踩触及才成交，不抢跑。
扫描不同挂单溢价 premium，找出诚实且有效的配置。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from data.fetch import build_observe_pool, fetch_klines, fetch_btc_klines
from backtest.engine import Backtest
import backtest.engine as eng


def load_data():
    pool_tickers = build_observe_pool(config.OBSERVE_POOL_SIZE)
    btc = fetch_btc_klines()
    pool = {}
    for t in pool_tickers:
        try:
            k = fetch_klines(t["symbol"])
            if len(k) >= config.EMA_SLOW + config.BOX_MIN_DAYS + 10:
                pool[t["symbol"]] = k
        except Exception:
            pass
    return btc, pool


def main():
    print("加载数据...")
    btc, pool = load_data()
    print(f"观察池 {len(pool)} 币\n")
    print("=" * 104)
    print("挂单溢价 premium：限价单挂在 box_high×(1+premium)，日内触及(low<=挂单价)才成交")
    print("-" * 104)

    # 通过临时修改 _process_pending 里的入场溢价来扫描
    # 用一个环境变量方式传递 premium
    for premium in [0.005, 0.01, 0.015, 0.02, 0.03]:
        # monkey-patch：直接改 engine 模块里用的溢价常量
        # 简单做法：改 config.PULLBACK_ZONE 并让 engine 用它
        saved = config.PULLBACK_ZONE
        config.PULLBACK_ZONE = premium
        # 但 engine 里入场价硬编码 1.01，需要改。这里改用子类覆盖
        try:
            # 动态修改 engine 模块中的 _process_pending
            import types
            orig = eng.Backtest._process_pending

            def _patched(self, t, premium=premium):
                still = []
                for p in self.pending:
                    ck = self._coin_at(p["symbol"], t)
                    if ck is None:
                        still.append(p); continue
                    close, low = ck["close"], ck["low"]
                    bh = p["box_high"]
                    days = t - p["signal_idx"]
                    entry_raw = bh * (1 + premium)
                    if close < bh * config.PULLBACK_BREAK:
                        continue
                    if days > config.PULLBACK_WINDOW:
                        continue
                    if low <= entry_raw and close > bh:
                        if len(self.holdings) < config.MAX_HOLDINGS:
                            ep = entry_raw * (1 + eng.ENTRY_COST)
                            self._enter(p["symbol"], ep, bh, ck)
                        continue
                    still.append(p)
                self.pending = still

            eng.Backtest._process_pending = _patched
            stats = Backtest(btc, pool, initial_equity=100_000).run()
            print(f"溢价{premium*100:>4.1f}% | 交易{stats['交易次数']:>3} | "
                  f"胜率{stats['胜率']*100:>5.1f}% | 盈亏比{stats['盈亏比']:>5.2f} | "
                  f"收益{stats['总收益率']*100:>+7.2f}% | 回撤{stats['最大回撤']*100:>5.1f}%")
        finally:
            eng.Backtest._process_pending = orig
            config.PULLBACK_ZONE = saved

    print("=" * 104)


if __name__ == "__main__":
    main()

"""
自驱动进化交易系统 — 完整闭环演示。

展示"信号 → 决策 → 执行 → 复盘 → 经验库 → 更优决策"的进化循环。
核心：策略/模型只是触发信号，真正的进化靠复盘驱动的经验累积。
"""
import sys
import os
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from execution.trade_journal import TradeJournal
from decision.review_engine import deep_review, ExperienceBank


class SelfEvolvingTrader:
    """自驱动进化交易员：决策时参考经验库，交易后复盘更新经验库。"""

    def __init__(self):
        self.journal = TradeJournal()
        self.bank = ExperienceBank()

    def decide(self, symbol, signal_score, signal_name, signal_price, entry_price,
               stop_dist, tp_dist, atr_value, journal=None):
        """
        决策层：综合信号 + 历史经验 → 是否交易、仓位、止损修正。
        这是"进化"的关键：不是机械执行信号，而是参考经验库调整。
        journal: 调用方显式传入(2026-08-17)——本类自建 TradeJournal 是启动
        时快照,不随交易更新,连亏检查读陈旧数据;且测试换掉 trader.journal
        后这里仍读生产库(隔离泄漏)。单一事实源 = 调用方的 journal。
        """
        journal = journal or self.journal
        decision = {"trade": True, "reason": [], "stop_adj": 0, "size_factor": 1.0,
                    "adopted_lesson_ids": []}   # R2-3：恒初始化，无采纳也返回空列表

        # 1. 信号门槛（统一维护于 config.DECIDE_MIN_SCORE,与引擎 SIGNAL_SCORE 联动）
        if signal_score < config.DECIDE_MIN_SCORE:
            decision["trade"] = False
            decision["reason"].append(
                f"信号分 {signal_score} < {config.DECIDE_MIN_SCORE}")
            return decision

        # 2. 查经验库：该币种历史教训
        relevant = self.bank.relevant(symbol=symbol)
        if relevant:
            # 统计主要错误类别 + 收集采纳经验 id（R2-3）
            from collections import Counter
            cats = Counter(l["category"] for l in relevant)
            ids_by_cat = {}
            for l in relevant:
                if l.get("id") is not None:
                    ids_by_cat.setdefault(l["category"], []).append(l["id"])
            if cats.get("止损", 0) >= 2:
                # 历史多次止损问题 → 自动放宽止损 0.2 ATR
                decision["stop_adj"] = 0.2
                decision["adopted_lesson_ids"] += ids_by_cat.get("止损", [])
                decision["reason"].append(
                    f"历史 {cats['止损']} 次止损问题，自动放宽止损 +0.2 ATR")
            if cats.get("入场时机", 0) >= 1:
                decision["adopted_lesson_ids"] += ids_by_cat.get("入场时机", [])
                decision["reason"].append("历史有追高记录，本次用限价单，不追市价")
        # 信号失效检查：读【被证伪】的经验（discarded），不是 trusted——
        # 失效教训经亏损验证后只会进 discarded，此前读 trusted 使该分支恒不可达
        discarded = getattr(self.bank, "discarded", lambda s, c=None: [])(symbol=symbol)
        if discarded:
            cats_d = Counter(l["category"] for l in discarded)
            if cats_d.get("信号", 0) >= 3:
                decision["adopted_lesson_ids"] += [l["id"] for l in discarded
                                                   if l["category"] == "信号" and l.get("id")]
                decision["trade"] = False
                decision["reason"].append(f"该信号模式历史 {cats_d['信号']} 次失效，拒绝")
                return decision

        # 3. 连亏检查（journal 用调用方传入的实时台账，2026-08-17）
        closed = [t for t in journal.trades if t["status"] == "closed"]
        recent_losses = [t for t in closed[-5:] if t["pnl"] is not None and t["pnl"] < 0]
        if len(recent_losses) >= 3:
            decision["trade"] = False
            decision["reason"].append(f"连亏 {len(recent_losses)} 笔，冷却")
            return decision
        if len(recent_losses) == 2:
            decision["size_factor"] = 0.5
            decision["reason"].append("近期连亏 2 笔，半仓")

        if not decision["reason"]:
            decision["reason"].append("信号达标，无历史警示，正常交易")
        # Phase0 T0.2：候选经验（一致性初筛通过、待独立验证）低权重参考——
        # 只写入决策理由并纳入采纳追踪（供后续交易验证），不改变任何参数。
        cands = getattr(self.bank, "candidates", lambda s: [])(symbol=symbol)
        if cands:
            decision["adopted_lesson_ids"] += [l["id"] for l in cands if l.get("id")]
            decision["reason"].append(f"参考 {len(cands)} 条待验证候选经验（不影响参数）")
        return decision

    def execute_and_review(self, symbol, signal_name, signal_score, signal_price,
                           entry_price, stop_loss, take_profit, size, atr_value,
                           exit_price):
        """执行 + 复盘（一笔交易的完整生命周期）。"""
        # 1. 记录开仓
        tid = self.journal.log_entry(
            symbol=symbol, signal=signal_name,
            reason=f"信号分 {signal_score}", entry_price=entry_price,
            stop_loss=stop_loss, take_profit=take_profit, size=size)

        # 2. 平仓
        t = self.journal.log_exit(tid, exit_price, "止损/止盈")

        # 3. 深度复盘
        report = deep_review(
            t, atr_value=atr_value,
            post_exit_reverse=(exit_price < stop_loss),  # 跳空触发 = 可能插针
            signal_price=signal_price)
        for l in report["lessons"]:
            self.bank.add(symbol, l)

        # 4. 返回复盘教训
        return t["pnl"], report["lessons"]


def demo():
    """演示进化：模拟 6 笔交易，观察经验库累积如何影响后续决策。"""
    print("=" * 70)
    print("自驱动进化交易系统 — 演示（模拟 6 笔交易）")
    print("=" * 70)
    trader = SelfEvolvingTrader()

    # 模拟交易序列（每笔有不同的错误模式）
    trades = [
        # (symbol, signal_name, score, signal_price, entry, stop, tp, size, atr, exit)
        ("BTC/USDT", "回踩确认", 78, 62500, 63000, 61500, 66000, 100, 300, 60500),  # 止损太宽+跳空
        ("BTC/USDT", "回踩确认", 76, 61800, 62400, 60800, 65400, 100, 280, 63500),  # 盈利
        ("ETH/USDT", "突破回抽", 80, 1850, 1890, 1830, 2010, 100, 25, 1820),        # 追高+止损
        ("BTC/USDT", "回踩确认", 74, 62000, 62100, 61000, 65000, 100, 260, 60800),  # 又止损
        ("BTC/USDT", "回踩确认", 77, 60000, 60100, 59300, 62500, 100, 240, 61200),  # 盈利
        ("SOL/USDT", "突破回抽", 79, 75, 76, 73, 82, 100, 1.5, 72),                 # 止损
    ]

    for i, (sym, sig, score, sp, ep, sl, tp, sz, atr, ex) in enumerate(trades, 1):
        # 决策层
        dec = trader.decide(sym, score, sig, sp, ep, (ep-sl)/ep, (tp-ep)/ep, atr)
        if not dec["trade"]:
            print(f"\n[{i}] {sym} {sig}: ❌ 拒绝交易 — {'; '.join(dec['reason'])}")
            continue
        # 执行 + 复盘
        pnl, lessons = trader.execute_and_review(
            sym, sig, score, sp, ep, sl, tp, sz, atr, ex)
        pnl_pct = pnl * 100
        print(f"\n[{i}] {sym} {sig} (分{score}): {'盈利' if pnl>0 else '亏损'} {pnl_pct:+.1f}%")
        print(f"    决策: {'; '.join(dec['reason'])}")
        for l in lessons:
            print(f"    复盘[{l['category']}]: {l['lesson'][:45]}...")

    # 最终经验库统计
    print("\n" + "=" * 70)
    print("进化结果 — 经验库错误类别统计:")
    stats = trader.bank.stats()
    for cat, cnt in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {cnt} 条教训")
    print("\n这 6 笔交易后，系统'学到'了自己的主要问题模式，")
    print("后续同类信号会自动调整止损、限价入场、连亏冷却。")


if __name__ == "__main__":
    demo()

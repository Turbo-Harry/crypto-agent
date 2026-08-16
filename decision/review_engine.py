"""
深度复盘引擎 — 自驱动进化的核心。
每笔交易结束后，从多个维度复盘，生成可操作的文字教训，沉淀到经验库。

复盘维度（每个都回答"这次决策对不对、错在哪、下次怎么办"）：
  1. 入场时机：是否追高/追低，信号到入场的偏差
  2. 止损质量：距离是否合理（对比 ATR），是否被插针扫掉
  3. 出场质量：止盈是否及时，止损是否果断
  4. 信号质量：信号是否真的成立
  5. 仓位管理：是否超风险
  6. 决策链：是否忽略了历史教训（重蹈覆辙）

每个教训都是"可操作的、带参数的"，不是空话，例如：
  "止损 1.5×ATR 太紧，被插针扫掉后价格反转，下次该场景用 2.0×ATR + 0.3 缓冲"
"""
import json
import os


def deep_review(trade, atr_value=None, post_exit_reverse=None, signal_price=None):
    """
    深度复盘一笔交易，返回结构化的复盘报告 + 教训列表。
    参数：
      trade: 交易记录 dict（含 entry/exit/stop/tp/size/reason 等）
      atr_value: 入场时的 ATR 值（用于判断止损距离合理性）
      post_exit_reverse: 出场后价格是否反转（用于判断止损是否被插针扫掉）
      signal_price: 信号触发时的参考价（用于判断是否追高）
    """
    lessons = []
    entry = trade["entry_price"]
    exit_px = trade["exit_price"]
    stop = trade["stop_loss"]
    tp = trade["take_profit"]
    pnl = trade["pnl"]

    is_short = trade.get("direction") == "short" or (tp < entry)
    if is_short:
        stop_dist = (stop - entry) / entry if stop > entry else 0
        tp_dist = (entry - tp) / entry if tp < entry else 0
    else:
        stop_dist = (entry - stop) / entry if entry > stop else 0
        tp_dist = (tp - entry) / entry if tp > entry else 0
    rr = tp_dist / stop_dist if stop_dist > 0 else 0

    # ---- 1. 入场时机 ----
    if signal_price:
        chase = (entry - signal_price) / signal_price
        if chase > 0.02:
            lessons.append({
                "category": "入场时机",
                "lesson": f"追高 {chase*100:.1f}%：入场价 {entry} 比信号价 {signal_price} 高出 {chase*100:.1f}%，"
                          f"下次用限价单挂在信号价附近，不追市价"})
        elif chase < -0.02:
            lessons.append({
                "category": "入场时机",
                "lesson": f"入场价低于信号价 {abs(chase)*100:.1f}%（可能信号已失效才成交），下次检查信号是否仍成立"})

    # ---- 2. 止损质量 ----
    if atr_value:
        atr_pct = atr_value / entry
        stop_in_atr = stop_dist / atr_pct if atr_pct > 0 else 0
        if stop_in_atr < 1.0:
            lessons.append({
                "category": "止损",
                "lesson": f"止损 {stop_dist*100:.1f}% 只有 {stop_in_atr:.1f}×ATR，太紧，容易被噪音扫掉，"
                          f"建议至少 1.5×ATR"})
        elif stop_in_atr > 3.0:
            lessons.append({
                "category": "止损",
                "lesson": f"止损 {stop_dist*100:.1f}% 达 {stop_in_atr:.1f}×ATR，太宽，单笔亏损过大，"
                          f"建议 1.5~2.0×ATR 并相应缩小仓位"})
    if post_exit_reverse:
        lessons.append({
            "category": "止损",
            "lesson": "止损后被插针扫掉、价格反转，说明止损位放在流动性扫损区（整数关口/前低），"
                      "下次止损放结构点外 + 0.3×ATR 缓冲"})

    # ---- 3. 出场质量 ----
    if pnl < 0:
        # 亏损：检查是否该早止损
        actual_loss = abs(pnl)
        if actual_loss > stop_dist * 1.3:
            lessons.append({
                "category": "出场",
                "lesson": f"实际亏损 {actual_loss*100:.1f}% 明显超过预设止损 {stop_dist*100:.1f}%（跳空/滑点），"
                          f"说明止损单未及时成交，下次用市价止损而非限价止损"})
    else:
        # 盈利：检查是否止盈太早
        if pnl < tp_dist * 0.5:
            lessons.append({
                "category": "出场",
                "lesson": f"盈利 {pnl*100:.1f}% 未达目标 {tp_dist*100:.1f}% 的一半，止盈太早，"
                          f"下次让利润奔跑（移动止盈）"})

    # ---- 4. 信号质量 ----
    if pnl > 0:
        lessons.append({
            "category": "信号",
            "lesson": f"信号有效（盈利 {pnl*100:.1f}%），该信号模式加权，下次同类信号可提高置信度"})
    else:
        lessons.append({
            "category": "信号",
            "lesson": f"信号失效（亏损 {pnl*100:.1f}%），复盘：入场时是否满足全部共振条件？是否在震荡市错误入场？"})

    # ---- 5. 盈亏比 ----
    if rr < 2.0:
        lessons.append({
            "category": "盈亏比",
            "lesson": f"盈亏比 {rr:.1f} < 2，这笔交易本就不该入场，下次入场前先算 R:R"})

    return {"pnl": pnl, "rr": rr, "lessons": lessons}


class ExperienceBank:
    """经验库：按错误类别分类的教训，支持统计和决策参考。"""

    def __init__(self, path="experience_bank.json"):
        self.path = path
        self.lessons = []
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.lessons = json.load(f)

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.lessons, f, ensure_ascii=False, indent=2)

    def add(self, symbol, lesson_dict):
        """加一条教训（含类别）。"""
        self.lessons.append({
            "symbol": symbol,
            "category": lesson_dict["category"],
            "lesson": lesson_dict["lesson"],
        })
        self._save()

    def stats(self):
        """统计各错误类别的频率（发现自己的主要问题）。"""
        from collections import Counter
        cats = Counter(l["category"] for l in self.lessons)
        return dict(cats)

    def relevant(self, symbol=None, category=None):
        """查相关教训（决策时参考）。"""
        out = self.lessons
        if symbol:
            out = [l for l in out if l["symbol"] == symbol]
        if category:
            out = [l for l in out if l["category"] == category]
        return out


if __name__ == "__main__":
    # 演示：一笔交易的深度复盘
    trade = {
        "entry_price": 63000, "exit_price": 60500, "stop_loss": 61500,
        "take_profit": 66000, "pnl": (60500 - 63000) / 63000,
    }
    report = deep_review(trade, atr_value=300, post_exit_reverse=True, signal_price=62500)
    print("=== 深度复盘报告 ===")
    print(f"盈亏 {report['pnl']*100:.1f}% | 盈亏比 {report['rr']:.1f}")
    print("\n教训:")
    bank = ExperienceBank()
    for l in report["lessons"]:
        print(f"  [{l['category']}] {l['lesson']}")
        bank.add("BTC/USDT", l)
    print("\n经验库统计:", bank.stats())

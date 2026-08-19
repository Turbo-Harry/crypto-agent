"""
风控模块 — 仓位计算 + 回撤熔断 + 每日限额。
风控层独立于信号层，拥有否决权。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def position_size(account_equity, stop_distance):
    """
    仓位计算（核心公式）：
      仓位 = (账户 × 单笔风险%) / 止损距离%
    再受单币最大仓位上限约束。
    """
    risk_amount = account_equity * config.RISK_PER_TRADE
    pos = risk_amount / stop_distance
    pos = min(pos, account_equity * config.MAX_POSITION_PER_COIN)
    return pos


def actual_risk(account_equity, stop_distance):
    """实际单笔风险（仓位受上限约束后的真实风险比例）"""
    pos = position_size(account_equity, stop_distance)
    return (pos * stop_distance) / account_equity


class RiskManager:
    """账户级风控状态机：跟踪净值、回撤、每日亏损，触发熔断。"""

    def __init__(self, initial_equity):
        self.initial_equity = initial_equity
        self.peak_equity = initial_equity
        self.equity = initial_equity
        self.day_start_equity = initial_equity
        self.current_day = None
        self.halted = False
        self.halt_reason = ""

    def update_equity(self, equity, day_key):
        """更新净值并检查熔断。day_key 用于识别交易日（如日期字符串）。"""
        self.equity = equity
        if self.current_day != day_key:
            # 停手语义是"当日"（DAILY_LOSS_LIMIT），跨日必须复位——
            # 否则一次真实熔断会把引擎永久锁死到进程重启（审计发现 2026-08-17）。
            self.current_day = day_key
            self.day_start_equity = equity
            self.halted = False
            self.halt_reason = ""
        self.peak_equity = max(self.peak_equity, equity)

        drawdown = (self.peak_equity - equity) / self.peak_equity
        daily_loss = (self.day_start_equity - equity) / self.day_start_equity

        if drawdown >= config.MAX_DRAWDOWN_HARD:
            self.halted = True
            self.halt_reason = f"回撤 {drawdown:.1%} 触发硬熔断（{config.MAX_DRAWDOWN_HARD:.0%}）"
        elif daily_loss >= config.DAILY_LOSS_LIMIT:
            self.halted = True
            self.halt_reason = f"单日亏损 {daily_loss:.1%} 触发停手（{config.DAILY_LOSS_LIMIT:.1%}）"

        return drawdown

    def can_trade(self):
        return not self.halted

    def reduce_exposure_flag(self):
        """回撤达软线时返回 True（应减仓）"""
        dd = (self.peak_equity - self.equity) / self.peak_equity
        return dd >= config.MAX_DRAWDOWN_SOFT


if __name__ == "__main__":
    # 自测仓位计算
    eq = 100_000
    for sd in [0.03, 0.05]:
        pos = position_size(eq, sd)
        print(f"账户 {eq:,.0f}，止损 {sd:.0%} → 仓位 {pos:,.0f}，实际风险 {actual_risk(eq, sd):.2%}")

"""
统一评分体系 — 把 9 类决策来源量化成 0-100 分，加权融合成综合决策分。

解决当前框架的最大短板："门槛式 if-else" → "量化评分"。
决策逻辑从 "年化>8% AND 情绪<80" 变成 "综合分 75 → 开仓"。

两部分：
  1. 信号评分：每个决策来源 → 0-100 分（可解释、单调）
  2. 框架自评：对系统本身的合理性打分（诚实量化）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============ 第一部分：信号评分（每个来源 → 0-100） ============

def score_funding_rate(annual):
    """① 资金费率：年化绝对值 → 分。非单调（OP-3）：
    8%→53分, 12%→80分, 15%→100分；但 |年化|≥80% 是挤压陷阱(squeeze trap)特征，
    反而是被逼空/逼多收割的前兆 → 30 分（此前单调递增会把陷阱当机会）。"""
    a = abs(annual)
    if a >= 0.80:
        return 30
    return min(100, max(0, a / 0.15 * 100))


def score_fear_greed(fng, direction="long"):
    """⑤ 恐惧贪婪 → 分。做多：恐惧(0)→100分，贪婪(100)→0分（别人恐惧我贪婪）。"""
    if direction == "long":
        return 100 - min(100, max(0, fng))
    return min(100, max(0, fng))


def score_orderflow(taker_buy, direction="long"):
    """③ 订单流：主动买占比 → 分。做多时买占比越高分越高。"""
    if direction == "long":
        return min(100, max(0, taker_buy * 200 - 50))
    return min(100, max(0, 50 - (taker_buy - 0.5) * 200))


def score_oi(lsr_account, direction="long"):
    """⑧ OI 多空比 → 分。
    做多：散户做多拥挤(>2.5)危险低分；做空：散户做空拥挤(<0.4)逼空风险低分。
    （审计 CR-7：v1 对空头恒返回 70，空头方向的 OI 因子形同虚设）"""
    if direction == "long":
        if lsr_account > 2.5:
            return 20   # 散户做多拥挤，反转风险
        if lsr_account < 0.8:
            return 60   # 散户做空极端，逆势
        return 80       # 正常区间
    # 做空方向（对称）
    if lsr_account < 0.4:
        return 20   # 散户做空拥挤，逼空风险
    if lsr_account > 1.25:
        return 60   # 散户极端做多，逆势
    return 80


def score_volatility(vol_15m):
    """② 波动率：15 分钟振幅 → 分（日内短线用分钟级，不用 24h）。
    0.5-1.5% 正常，1.5-3% 偏高，>3% 极端（插针风险），<0.3% 无动能。
    None = 数据未就绪（WebSocket 预热中）→ 45 分，低于中性，保守观望。"""
    if vol_15m is None:
        return 45
    if vol_15m <= 0:
        return 50
    if 0.005 <= vol_15m <= 0.015:
        return 80
    if 0.015 < vol_15m <= 0.03:
        return 50
    if vol_15m > 0.03:
        return 20
    return 40


def net_funding_annual(annual):
    """资金费率毛年化 → 扣费后净年化。
    开平仓往返成本按 ARB_MIN_HOLD_DAYS 持有期摊销（审计 CR-4：毛年化高估）。"""
    import config
    cost_annual = config.ARB_ROUNDTRIP_COST * (365 / config.ARB_MIN_HOLD_DAYS)
    return abs(annual) - cost_annual


def score_calendar():
    """⑦ 经济日历：事件窗口内 0 分；正常 100 分。
    RES-13：事件清单已全部过期 → 60 分（fail-safe，不再伪装"安全"）+
    30 天冷却的飞书告警一次。"""
    from data.economic_calendar import is_high_impact_now, calendar_expired
    in_win, ev = is_high_impact_now()
    if in_win:
        return 0
    expired, last = calendar_expired()
    if expired:
        _calendar_expiry_alert(last)
        return 60   # 数据过期：中性以下，不再恒 100
    return 100


def _calendar_expiry_alert(last):
    """RES-13：日历过期告警（30 天冷却，防重复轰炸）。"""
    import time
    try:
        state_file = "calendar_alert_state.json"
        import json
        try:
            with open(state_file) as f:
                st = json.load(f)
        except Exception:
            st = {}
        if time.time() - st.get("last_alert", 0) < 30 * 86400:
            return
        st["last_alert"] = time.time()
        with open(state_file, "w") as f:
            json.dump(st, f)
        msg = (f"⚠️ 经济日历事件已全部过期（最近 {last}），日历风控门失效——"
               f"请更新 DEFAULT_EVENTS 或接入日历数据源")
        try:
            import subprocess
            subprocess.run([os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".lark"), "im",
                            "+messages-send", "--as", "bot",
                            "--user-id", "ou_3c597d18937078f2587b56adb8b960d2",
                            "--text", msg], capture_output=True, timeout=20)
        except Exception:
            pass
    except Exception:
        pass


def score_experience(bank, symbol):
    """⑥ 经验库（带评分）：只按可信经验给分，弃用经验不参考。
    历史经验不一定对——只有被交易结果验证过的经验才有发言权。"""
    try:
        from decision.experience_scoring import experience_score_for_decision
        return experience_score_for_decision(bank, symbol)
    except Exception:
        return 70


def score_factor(factor_ic):
    """⑩ 因子：进化因子的 IC → 分（IC 0.15 → 100 分）。"""
    return min(100, max(0, abs(factor_ic) / 0.15 * 100))


# ============ 第二部分：加权融合 ============

# 资金费率套利的权重（费率是核心，波动率防插针）
# 审计 CR-7：calendar 0.20 权重但 99.99% 时间为常数 100，等于稀释其余因子分辨力
# → 降为 0.10，分摊给 funding/volatility（因子相关性去冗余的 IC 化校准待 R3 数据回测）
ARB_WEIGHTS = {
    "funding": 0.35,     # 费率年化（核心）
    "volatility": 0.20,  # 波动率（防插针/无动能）
    "fear_greed": 0.10,  # 情绪
    "oi": 0.10,          # 多空拥挤度
    "calendar": 0.10,    # 事件避险（降权：多数时间常数，勿稀释其余因子）
    "experience": 0.15,  # 经验库
}

# 方向性交易的权重（因子+情绪是核心）
DIR_WEIGHTS = {
    "factor": 0.30,      # 进化因子（alpha 核心）
    "fear_greed": 0.20,  # 情绪
    "orderflow": 0.15,   # 订单流
    "calendar": 0.20,    # 事件避险
    "experience": 0.15,  # 经验库
}

DECISION_THRESHOLD = 70   # 综合分 >= 70 开仓
DECISION_CAUTION = 50     # 50-70 半仓或观望


def composite(scores, weights):
    """加权融合 → 综合决策分（0-100）。"""
    total = sum(w * scores.get(k, 50) for k, w in weights.items())
    wsum = sum(weights.values())
    return total / wsum


def decide(scores, weights):
    """综合分 → 决策。"""
    s = composite(scores, weights)
    if s >= DECISION_THRESHOLD:
        return {"action": "开仓", "score": s}
    if s >= DECISION_CAUTION:
        return {"action": "半仓/观望", "score": s}
    return {"action": "不开仓", "score": s}


# ============ 第三部分：框架合理性自评 ============

def framework_self_assessment():
    """对系统本身打分（诚实量化框架合理性）。"""
    dims = {
        "验证严谨性": 85,
        "风控体系": 80,
        "数据完整性": 75,
        "执行可靠性": 75,
        "自进化能力": 70,
        "策略有效性": 65,
        "信号融合": 55,
    }
    total = sum(dims.values()) / len(dims)
    return dims, total


if __name__ == "__main__":
    # 演示：当前市场的综合评分
    print("=" * 60)
    print("统一评分体系 — 演示")
    print("=" * 60)

    # 套利评分（当前 BTC）
    arb_scores = {
        "funding": score_funding_rate(0.093),   # 年化 9.3%
        "fear_greed": score_fear_greed(34),      # 恐惧贪婪 34（恐惧）
        "oi": score_oi(2.07),                    # 多空比 2.07
        "calendar": score_calendar(),            # 经济日历
        "experience": 100,                       # 经验库（干净）
    }
    arb_total = composite(arb_scores, ARB_WEIGHTS)
    print(f"\n套利决策评分: {arb_total:.1f} 分")
    for k, v in arb_scores.items():
        print(f"  {k}: {v:.0f} 分")
    print(f"  → 决策: {decide(arb_scores, ARB_WEIGHTS)['action']}")

    # 框架自评
    dims, total = framework_self_assessment()
    print(f"\n框架合理性自评: {total:.1f}/100")
    for k, v in dims.items():
        bar = "█" * (v // 5)
        print(f"  {k:<8} {v:>3} {bar}")

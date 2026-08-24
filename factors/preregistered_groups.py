"""预注册机制因子组的 research-only 挑战评估；绝不直接生成运行制品。"""
from dataclasses import asdict, dataclass

import config
from factors.entry_model_training import (evaluate_rows, load_entry_rows,
                                          select_model_family)


@dataclass(frozen=True)
class FactorGroup:
    name: str
    rationale: str
    features: tuple[str, ...]


# 2026-08-25 在查看本轮组合结果前冻结。只包含当时行情可复算的五个机制族；
# 资金费/OI/连续盘口组因当前历史源缺失而不注册，避免用缺失代理制造组合。
PREREGISTERED_GROUPS = (
    FactorGroup(
        "trend_quality",
        "多周期趋势只有在成交参与同步时才可能具有延续性",
        ("trend_band_atr", "momentum_1h", "momentum_4h", "volume_ratio")),
    FactorGroup(
        "pullback_confirmation",
        "回踩位置、拒绝影线与异常成交共同刻画承接质量",
        ("pullback_depth_atr", "wick_ratio", "volume_zscore")),
    FactorGroup(
        "breakout_confirmation",
        "挤压释放、趋势强度与异常成交共同确认突破",
        ("bb_squeeze_release", "adx", "volume_zscore")),
    FactorGroup(
        "market_resonance",
        "相对强弱、市场宽度与剔除 BTC 后动量区分共振与孤立异动",
        ("cross_sectional_rank", "market_breadth", "btc_residual_momentum")),
    FactorGroup(
        "execution_cost",
        "价差、150 USDT 预期滑点与 ATR 风险尺度共同约束交易摩擦",
        ("spread_bps", "expected_slippage_bps", "atr_pct")),
)


def evaluate_preregistered_groups(db_path=None, strategy_id=None):
    """同折比较 Logistic/CatBoost；结果只给证伪权，不写模型生命周期。"""
    output = []
    for group in PREREGISTERED_GROUPS:
        directions = {}
        for direction in ("long", "short"):
            rows = load_entry_rows(
                direction, list(group.features), db_path, strategy_id)
            complete = [row for row in rows if all(
                row["features"].get(name) is not None
                for name in group.features)]
            missing_rate = 1 - len(complete) / len(rows) if rows else 1.0
            if (len(complete) < config.ENTRY_MODEL_MIN_SAMPLES or
                    missing_rate > config.FACTOR_MAX_MISSING_RATE):
                directions[direction] = {
                    "status": "insufficient_group_data",
                    "n": len(rows), "complete_n": len(complete),
                    "missing_rate": missing_rate,
                    "eligible_for_shadow": False,
                }
                continue
            logistic = evaluate_rows(
                complete, list(group.features), "logistic_ovr")
            catboost = evaluate_rows(
                complete, list(group.features), "catboost_multiclass")
            family, relative_gain = select_model_family(logistic, catboost)
            selected = catboost if family == "catboost_multiclass" else logistic
            directions[direction] = {
                "status": ("eligible_for_shadow_review"
                           if selected["eligible_for_shadow"]
                           else "stop_no_promotion"),
                "n": len(rows), "complete_n": len(complete),
                "missing_rate": missing_rate, "selected_family": family,
                "relative_multiclass_brier_gain": relative_gain,
                "eligible_for_shadow": selected["eligible_for_shadow"],
                "selected": selected, "logistic": logistic,
                "catboost": catboost,
            }
        output.append({**asdict(group), "directions": directions,
                       "research_only": True})
    return output

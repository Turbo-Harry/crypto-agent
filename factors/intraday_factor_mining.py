"""只消费 signal_samples/signal_outcomes 的日内自动因子挖掘入口。"""
import json
import time
from typing import List, Optional

import config
from factors.feature_registry import REGISTRY, extract_features
from factors.intraday_factor_gate import evaluate_factor


def load_observations(feature_name: str, db_path=None,
                      strategy_id: Optional[str] = None) -> List[dict]:
    import storage.db as sdb
    sdb.init_db(db_path)
    rows = sdb.q(
        "SELECT s.*,o.pnl_r,o.tp_first,o.sl_first,o.timeout "
        "FROM signal_samples_canonical s JOIN signal_outcomes o ON o.signal_id=s.signal_id "
        "WHERE s.strategy_id=? AND s.timeframe=? AND s.horizon_hours=? "
        "ORDER BY s.event_ts",
        [strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID,
         config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS],
        db_path=db_path)
    spec = REGISTRY[feature_name]
    out = []
    for row in rows:
        features = extract_features(row)
        value = features.get(feature_name)
        if value is not None and spec.expected_direction == "directional" \
                and row["direction"] == "short":
            value = -value
        try:
            snapshot = json.loads(row.get("features") or "{}")
            regime = snapshot.get("regime") or {}
            regime_tag = (regime.get("tag") if isinstance(regime, dict)
                          else str(regime))
        except (TypeError, ValueError, json.JSONDecodeError):
            regime_tag = None
        month = time.strftime("%Y-%m", time.gmtime(float(row["event_ts"])))
        out.append({"signal_id": row["signal_id"], "event_ts": row["event_ts"],
                    "label_end_ts": row["event_ts"] + row["horizon_hours"] * 3600,
                    "symbol": row["symbol"], "direction": row["direction"],
                    "regime": regime_tag or "unknown", "month": month,
                    "entry": row["entry"], "stop": row["stop"],
                    "horizon_hours": row["horizon_hours"],
                    "funding_rate": features.get("funding_rate"),
                    "value": value, "pnl_r": row["pnl_r"],
                    "tp_first": row["tp_first"], "sl_first": row["sl_first"],
                    "timeout": row["timeout"]})
    return out


def run_mining(db_path=None, strategy_id: Optional[str] = None):
    """验证预注册且有理论依据的候选；不直接改权重或交易规则。"""
    if len(REGISTRY) > config.FACTOR_MAX_AUTO_CANDIDATES:
        raise ValueError(
            f"因子候选 {len(REGISTRY)} 超过上限 "
            f"{config.FACTOR_MAX_AUTO_CANDIDATES}")
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    observations = {
        name: load_observations(name, db_path, strategy_id) for name in REGISTRY}
    total_candidates = len(REGISTRY)
    accepted_values = {}
    results = []
    for name, spec in REGISTRY.items():
        result = evaluate_factor(
            name, spec.rationale, observations[name],
            total_candidates=total_candidates, accepted=accepted_values,
            expression=name, db_path=db_path, strategy_id=strategy_id)
        results.append(result)
        if result["status"] == "validated":
            accepted_values[name] = {
                row["signal_id"]: row["value"] for row in observations[name]
                if row["value"] is not None}
    return results

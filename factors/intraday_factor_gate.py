"""日内候选因子的 purged walk-forward 样本外验证门。"""
import json
import hashlib
import math
import time
from collections import defaultdict
from typing import Dict, List, Optional

import config
from decision.entry_probability import execution_cost_r
from factors.overfit_guard import deflated_sharpe, pbo_cscv


EVALUATION_VERSION = "intraday-factor-oos-v5"


def _dataset_hash(rows):
    evidence = [{"signal_id": row.get("signal_id"),
                 "event_ts": row.get("event_ts"),
                 "value": row.get("value"), "pnl_r": row.get("pnl_r"),
                 "entry": row.get("entry"), "stop": row.get("stop"),
                 "direction": row.get("direction"),
                 "horizon_hours": row.get("horizon_hours"),
                 "funding_rate": row.get("funding_rate")}
                for row in sorted(rows, key=lambda item: (
                    float(item.get("event_ts") or 0), str(item.get("signal_id") or "")))]
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pearson(x, y):
    if len(x) < 3 or len(x) != len(y):
        return 0.0
    mx, my = sum(x) / len(x), sum(y) / len(y)
    xy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    xx = sum((a - mx) ** 2 for a in x)
    yy = sum((b - my) ** 2 for b in y)
    return xy / math.sqrt(xx * yy) if xx > 0 and yy > 0 else 0.0


def spearman(x, y):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda idx: values[idx])
        out = [0.0] * len(values)
        for rank, idx in enumerate(order):
            out[idx] = rank
        return out
    return pearson(ranks(x), ranks(y)) if len(x) >= 3 else 0.0


def purged_walk_forward_splits(rows: List[dict], folds: int = None):
    """扩展训练窗 + 连续测试窗；标签窗 purge，测试后 embargo 由下折自然隔离。"""
    folds = int(folds or config.FACTOR_WALK_FORWARD_FOLDS)
    ordered = sorted(range(len(rows)), key=lambda idx: rows[idx]["event_ts"])
    block = len(ordered) // (folds + 1)
    if block < 1:
        return []
    splits = []
    embargo = config.FACTOR_EMBARGO_HOURS * 3600
    for fold in range(folds):
        test_lo = block * (fold + 1)
        test_hi = block * (fold + 2) if fold < folds - 1 else len(ordered)
        test = ordered[test_lo:test_hi]
        if not test:
            continue
        test_start = rows[test[0]]["event_ts"]
        train = [idx for idx in ordered[:test_lo]
                 if rows[idx]["label_end_ts"] <= test_start - embargo]
        if len(train) >= 30:
            splits.append((train, test))
    return splits


def _tstat(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance == 0:
        return 99.0 if mean > 0 else 0.0
    return mean / math.sqrt(variance / len(values))


def _cost_r(row):
    return float(execution_cost_r(row) or 0.0)


def _log_trial(result, db_path=None):
    import storage.db as sdb
    sdb.init_db(db_path)
    sdb.x(
        "INSERT OR IGNORE INTO factor_trials (ts,name,strategy_id,rationale,n_samples,n_folds,"
        "mean_ic,icir,ic_tstat,gross_spread,turnover,net_spread,status,"
        "expression,dsr,pbo,missing_rate,fold_consistency,"
        "symbol_concentration,redundant_with,details,timeframe,horizon_hours,"
        "trial_key,data_hash,evaluation_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [time.time(), result["name"], result["strategy_id"],
         result.get("rationale", ""),
         result.get("n_samples", 0), result.get("n_folds", 0),
         result.get("mean_ic", 0), result.get("icir", 0),
         result.get("ic_tstat", 0), result.get("gross_spread", 0), 0,
         result.get("net_ev_increment", 0), result["status"],
         result.get("expression"), result.get("dsr"), result.get("pbo"),
         result.get("missing_rate"), result.get("fold_consistency"),
         result.get("symbol_concentration"), result.get("redundant_with"),
         json.dumps({"folds": result.get("folds", []),
                     "stability": result.get("stability", {}),
                     "candidate_universe": result.get("candidate_universe")},
                    ensure_ascii=False, sort_keys=True),
         config.SIGNAL_SAMPLE_TIMEFRAME,
         config.SIGNAL_OUTCOME_HORIZON_HOURS, result["trial_key"],
         result["data_hash"], result["evaluation_version"]], db_path=db_path)


def _stability_keys(row):
    """固定维度的 OOS 分组；缺失值显式记 unknown，不静默丢行。"""
    return (("direction", row.get("direction") or "unknown"),
            ("symbol", row.get("symbol") or "unknown"),
            ("regime", row.get("regime") or "unknown"),
            ("month", row.get("month") or "unknown"))


def evaluate_factor(name: str, rationale: str, rows: List[dict],
                    total_candidates: Optional[int] = None,
                    accepted: Optional[Dict[str, Dict[str, float]]] = None,
                    expression: Optional[str] = None, db_path=None,
                    strategy_id: Optional[str] = None):
    """评估 rows（每行 event_ts/label_end_ts/value/pnl_r/symbol/entry/stop）。"""
    total = len(rows)
    trials = int(total_candidates or 0)
    if not trials and db_path:
        try:
            import storage.db as sdb
            trials = int(sdb.q1("SELECT COUNT(*) n FROM factor_trials",
                                db_path=db_path)["n"]) + 1
        except Exception:
            trials = 1
    usable = [row for row in rows if row.get("value") is not None]
    missing_rate = 1 - len(usable) / total if total else 1.0
    strategy_id = str(strategy_id or config.ENTRY_SIGNAL_STRATEGY_ID)
    result = {"name": name, "strategy_id": strategy_id,
              "rationale": rationale or "",
              "expression": expression, "n_samples": len(usable),
              "n_folds": 0, "mean_ic": 0.0, "icir": 0.0,
              "ic_tstat": 0.0, "gross_spread": 0.0,
              "net_ev_increment": 0.0, "dsr": None, "pbo": None,
              "missing_rate": missing_rate, "fold_consistency": 0,
              "symbol_concentration": 1.0, "redundant_with": None,
              "status": "reject", "folds": [], "stability": {},
              "candidate_universe": max(1, trials)}
    result["data_hash"] = _dataset_hash(rows)
    result["evaluation_version"] = EVALUATION_VERSION
    result["trial_key"] = hashlib.sha256(json.dumps({
        "name": name, "expression": expression,
        "strategy_id": strategy_id,
        "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME,
        "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
        "data_hash": result["data_hash"],
        "evaluation_version": EVALUATION_VERSION,
        "candidate_universe": result["candidate_universe"],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if not rationale:
        result["status"] = "hypothesis_only"
        _log_trial(result, db_path)
        return result
    if len(usable) < config.FACTOR_MIN_SAMPLES:
        result["status"] = "insufficient_data"
        _log_trial(result, db_path)
        return result
    if missing_rate > config.FACTOR_MAX_MISSING_RATE:
        result["status"] = "reject_missing"
        _log_trial(result, db_path)
        return result

    if accepted:
        current = {row["signal_id"]: float(row["value"]) for row in usable}
        for accepted_name, accepted_values in accepted.items():
            common = sorted(set(current) & set(accepted_values))
            corr = pearson([current[key] for key in common],
                           [accepted_values[key] for key in common])
            if len(common) >= 30 and abs(corr) > config.FACTOR_REDUNDANT_CORR:
                result["status"] = "redundant"
                result["redundant_with"] = accepted_name
                _log_trial(result, db_path)
                return result

    splits = purged_walk_forward_splits(usable)
    factor_returns = []
    baseline_returns = []
    inverse_returns = []
    fold_spreads = []
    symbol_profit = {}
    segment_stats = defaultdict(lambda: {
        "n": 0, "selected_n": 0, "policy_sum": 0.0,
        "baseline_sum": 0.0})
    for fold, (train_idx, test_idx) in enumerate(splits):
        train_x = [float(usable[idx]["value"]) for idx in train_idx]
        train_y = [float(usable[idx]["pnl_r"]) for idx in train_idx]
        orientation = 1.0 if spearman(train_x, train_y) >= 0 else -1.0
        threshold = sorted(orientation * value for value in train_x)[len(train_x) // 2]
        test_x = [orientation * float(usable[idx]["value"]) for idx in test_idx]
        test_y = [float(usable[idx]["pnl_r"]) for idx in test_idx]
        ic = spearman(test_x, test_y)
        selected = [idx for idx, value in zip(test_idx, test_x) if value >= threshold]
        rejected = [idx for idx, value in zip(test_idx, test_x) if value < threshold]
        selected_net = [float(usable[idx]["pnl_r"]) - _cost_r(usable[idx])
                        for idx in selected]
        baseline_net = [float(usable[idx]["pnl_r"]) - _cost_r(usable[idx])
                        for idx in test_idx]
        selected_ev = sum(selected_net) / len(selected_net) if selected_net else -99.0
        baseline_ev = sum(baseline_net) / len(baseline_net) if baseline_net else 0.0
        increment = selected_ev - baseline_ev
        high_mean = (sum(float(usable[idx]["pnl_r"]) for idx in selected) /
                     len(selected)) if selected else 0.0
        low_mean = (sum(float(usable[idx]["pnl_r"]) for idx in rejected) /
                    len(rejected)) if rejected else 0.0
        spread = high_mean - low_mean
        fold_spreads.append(spread)
        result["folds"].append({"fold": fold, "train_n": len(train_idx),
                                "test_n": len(test_idx), "ic": ic,
                                "net_ev_increment": increment})
        for idx, value in zip(test_idx, test_x):
            net = float(usable[idx]["pnl_r"]) - _cost_r(usable[idx])
            chosen = net if value >= threshold else 0.0
            factor_returns.append(chosen)
            inverse_returns.append(net if value < threshold else 0.0)
            baseline_returns.append(net)
            if chosen > 0:
                symbol = usable[idx]["symbol"]
                symbol_profit[symbol] = symbol_profit.get(symbol, 0.0) + chosen
            for dimension, segment in _stability_keys(usable[idx]):
                stat = segment_stats[(dimension, str(segment))]
                stat["n"] += 1
                stat["selected_n"] += int(value >= threshold)
                stat["policy_sum"] += chosen
                stat["baseline_sum"] += net

    result["n_folds"] = len(result["folds"])
    if result["folds"]:
        ics = [fold["ic"] for fold in result["folds"]]
        result["mean_ic"] = sum(ics) / len(ics)
        sd = math.sqrt(sum((value - result["mean_ic"]) ** 2 for value in ics) /
                       max(1, len(ics) - 1))
        result["icir"] = result["mean_ic"] / sd if sd else 0.0
        result["ic_tstat"] = _tstat(fold_spreads)
        result["gross_spread"] = sum(fold_spreads) / len(fold_spreads)
        result["net_ev_increment"] = sum(
            fold["net_ev_increment"] for fold in result["folds"]) / len(result["folds"])
        result["fold_consistency"] = sum(
            1 for fold in result["folds"]
            if fold["ic"] > 0 and fold["net_ev_increment"] > 0)
    result["dsr"] = deflated_sharpe(factor_returns, max(1, trials))
    try:
        matrix = [[factor_returns[i], baseline_returns[i], inverse_returns[i]]
                  for i in range(len(factor_returns))]
        result["pbo"] = pbo_cscv(matrix, n_blocks=config.FACTOR_PBO_BLOCKS)
    except Exception:
        result["pbo"] = None
    total_positive = sum(symbol_profit.values())
    result["symbol_concentration"] = (
        max(symbol_profit.values()) / total_positive if total_positive > 0 else 1.0)
    stability = defaultdict(dict)
    for (dimension, segment), stat in sorted(segment_stats.items()):
        n = stat["n"]
        policy_ev = stat["policy_sum"] / n
        baseline_ev = stat["baseline_sum"] / n
        stability[dimension][segment] = {
            "n": n, "selected_n": stat["selected_n"],
            "coverage": round(stat["selected_n"] / n, 6),
            "policy_ev_r": round(policy_ev, 8),
            "baseline_ev_r": round(baseline_ev, 8),
            "net_ev_increment": round(policy_ev - baseline_ev, 8)}
    result["stability"] = dict(stability)

    hard_pass = (
        result["n_folds"] >= config.FACTOR_WALK_FORWARD_FOLDS and
        result["ic_tstat"] >= config.FACTOR_MIN_TSTAT and
        result["fold_consistency"] >= config.FACTOR_MIN_CONSISTENT_FOLDS and
        result["net_ev_increment"] > 0 and
        result["dsr"] is not None and result["dsr"] >= config.DSR_ACCEPT and
        result["pbo"] is not None and result["pbo"] < config.PBO_ACCEPT and
        result["symbol_concentration"] <= config.FACTOR_MAX_SYMBOL_CONCENTRATION)
    result["status"] = "validated" if hard_pass else "reject"
    _log_trial(result, db_path)
    return result

"""预注册因子组的二层非负权重训练；research-only，无制品/订单权限。"""
import math

import config
from decision.entry_probability import fit_logistic, raw_probability, sigmoid
from factors.intraday_factor_gate import purged_walk_forward_splits


def _lower(values):
    if not values:
        return None
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return mean - config.ENTRY_MODEL_EV_Z * math.sqrt(variance / len(values))


def _matrix(rows, features, medians=None):
    if medians is None:
        medians = {}
        for name in features:
            values = sorted(row["features"].get(name) for row in rows
                            if row["features"].get(name) is not None)
            if not values:
                return None, None
            medians[name] = values[len(values) // 2]
    return ([[float(row["features"].get(name)
                    if row["features"].get(name) is not None else medians[name])
              for name in features] for row in rows], medians)


def _fit_nonnegative_logistic(features, labels):
    """投影梯度 Logistic：组权重非负，L2/学习率/轮数复用集中参数。"""
    if not features or len(features) != len(labels) or not features[0]:
        return None
    width = len(features[0])
    means = [sum(row[j] for row in features) / len(features)
             for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2
                       for row in features) / len(features)
        scales.append(math.sqrt(variance) or 1.0)
    normalized = [[(row[j] - means[j]) / scales[j] for j in range(width)]
                  for row in features]
    base = min(1 - 1e-6, max(1e-6, sum(labels) / len(labels)))
    bias = math.log(base / (1 - base))
    weights = [0.0] * width
    for _ in range(config.ENTRY_MODEL_EPOCHS):
        grad_b = 0.0
        grad_w = [0.0] * width
        for row, label in zip(normalized, labels):
            probability = sigmoid(bias + sum(
                weight * value for weight, value in zip(weights, row)))
            error = probability - int(label)
            grad_b += error
            for j, value in enumerate(row):
                grad_w[j] += error * value
        n = len(normalized)
        bias -= config.ENTRY_MODEL_LEARNING_RATE * grad_b / n
        for j in range(width):
            update = (grad_w[j] / n + config.ENTRY_MODEL_L2 * weights[j])
            weights[j] = max(
                0.0, weights[j] - config.ENTRY_MODEL_LEARNING_RATE * update)
    return {"means": means, "scales": scales, "weights": weights,
            "bias": bias, "n_train": len(features), "base_rate": base}


def _four_way_split(rows):
    """基础组拟合→stack拟合→policy校准；三段均早于外层测试。"""
    n = len(rows)
    minimum = config.ENTRY_CALIBRATION_MIN_SAMPLES
    if n < minimum * 3:
        return None
    base_end = max(minimum, int(n * 0.60))
    stack_end = max(base_end + minimum, int(n * 0.80))
    if n - stack_end < minimum:
        stack_end = n - minimum
    stack_start = rows[base_end]["event_ts"]
    policy_start = rows[stack_end]["event_ts"]
    embargo = config.FACTOR_EMBARGO_HOURS * 3600
    base = [row for row in rows[:base_end]
            if row["label_end_ts"] <= stack_start - embargo]
    stack = [row for row in rows[base_end:stack_end]
             if row["label_end_ts"] <= policy_start - embargo]
    policy = rows[stack_end:]
    if min(len(base), len(stack), len(policy)) < minimum:
        return None
    return base, stack, policy


def evaluate_group_weight_model(rows, groups):
    """四段时序 stacking；groups 为 (name, features) 且必须预先冻结。"""
    group_names = [name for name, _ in groups]
    folds, selected_all, equal_all, signal_all = [], [], [], []
    all_truth, all_probability, all_base = [], [], []
    for fold, (train_idx, test_idx) in enumerate(
            purged_walk_forward_splits(rows)):
        train = [rows[idx] for idx in train_idx]
        test = [rows[idx] for idx in test_idx]
        split = _four_way_split(train)
        if split is None:
            continue
        base_rows, stack_rows, policy_rows = split
        group_models, group_medians = [], []
        valid = True
        for _, features in groups:
            base_x, medians = _matrix(base_rows, features)
            stack_x, _ = _matrix(stack_rows, features, medians)
            labels = [int(row["net_pnl_r"] > 0) for row in base_rows]
            model = fit_logistic(base_x, labels)
            if model is None:
                valid = False
                break
            group_models.append(model)
            group_medians.append(medians)
        if not valid:
            continue

        def group_scores(target_rows):
            columns = []
            for (_, features), model, medians in zip(
                    groups, group_models, group_medians):
                values, _ = _matrix(target_rows, features, medians)
                probabilities = [raw_probability(model, row) for row in values]
                if any(value is None for value in probabilities):
                    return None
                columns.append(probabilities)
            return [list(values) for values in zip(*columns)]

        stack_scores = group_scores(stack_rows)
        policy_scores = group_scores(policy_rows)
        test_scores = group_scores(test)
        if stack_scores is None or policy_scores is None or test_scores is None:
            continue
        meta = _fit_nonnegative_logistic(
            stack_scores, [int(row["net_pnl_r"] > 0) for row in stack_rows])
        if meta is None:
            continue
        policy_probability = [raw_probability(meta, values)
                              for values in policy_scores]
        candidates = []
        for threshold in sorted(set(policy_probability)):
            returns = [row["net_pnl_r"] for row, probability in zip(
                policy_rows, policy_probability) if probability >= threshold]
            if len(returns) >= config.MODEL_MIN_SELECTED_EVALUATIONS:
                lower = _lower(returns)
                if lower is not None and lower > 0:
                    candidates.append((lower, len(returns), threshold))
        policy = max(candidates, key=lambda item: (item[0], item[1])) \
            if candidates else None
        threshold = policy[2] if policy else None
        probabilities = [raw_probability(meta, values) for values in test_scores]
        selected = [row for row, probability in zip(test, probabilities)
                    if threshold is not None and probability >= threshold]
        selected_returns = [row["net_pnl_r"] for row in selected]
        equal_probability = [sum(values) / len(values) for values in test_scores]
        equal_ranked = [row for _, row in sorted(
            zip(equal_probability, test), key=lambda item: item[0], reverse=True)
                        [:len(selected)]]
        signal_ranked = sorted(
            test, key=lambda row: (
                float(row.get("baseline_score"))
                if row.get("baseline_score") is not None else float("-inf"),
                str(row.get("signal_id") or "")), reverse=True)[:len(selected)]
        equal_returns = [row["net_pnl_r"] for row in equal_ranked]
        signal_returns = [row["net_pnl_r"] for row in signal_ranked]
        truth = [int(row["net_pnl_r"] > 0) for row in test]
        base_rate = sum(int(row["net_pnl_r"] > 0) for row in base_rows) / len(base_rows)
        shares_total = sum(meta["weights"])
        shares = {name: (weight / shares_total if shares_total else 0.0)
                  for name, weight in zip(group_names, meta["weights"])}
        folds.append({
            "fold": fold, "n": len(test), "threshold": threshold,
            "selected_n": len(selected), "weights": shares,
            "net_ev": (sum(selected_returns) / len(selected_returns)
                       if selected_returns else None),
            "net_ev_lower_bound": _lower(selected_returns),
            "equal_weight_ev": (sum(equal_returns) / len(equal_returns)
                                if equal_returns else None),
            "signal_score_ev": (sum(signal_returns) / len(signal_returns)
                                if signal_returns else None),
            "brier": sum((p - y) ** 2 for p, y in zip(probabilities, truth)) /
            len(truth),
            "baseline_brier": sum((base_rate - y) ** 2 for y in truth) /
            len(truth),
        })
        selected_all.extend(selected_returns)
        equal_all.extend(equal_returns)
        signal_all.extend(signal_returns)
        all_truth.extend(truth)
        all_probability.extend(probabilities)
        all_base.extend([base_rate] * len(test))
    if not all_truth:
        return {"status": "insufficient_data", "folds": folds,
                "selected_n": 0, "eligible_for_shadow": False,
                "research_only": True}
    brier_value = (sum((p - y) ** 2
                       for p, y in zip(all_probability, all_truth)) /
                   len(all_truth))
    base_brier = (sum((p - y) ** 2 for p, y in zip(all_base, all_truth)) /
                  len(all_truth))
    skill = 1 - brier_value / base_brier if base_brier else 0.0
    good_brier = sum(item["brier"] <= item["baseline_brier"] for item in folds)
    good_ev = sum(item["net_ev"] is not None and item["net_ev"] > 0 and
                  item["equal_weight_ev"] is not None and
                  item["signal_score_ev"] is not None and
                  item["net_ev"] >= max(item["equal_weight_ev"],
                                        item["signal_score_ev"])
                  for item in folds)
    lower = _lower(selected_all)
    active_weight_folds = [fold for fold in folds
                           if sum(fold["weights"].values()) > 0]
    zero_weight_folds = len(folds) - len(active_weight_folds)
    weight_values = {
        name: [fold["weights"].get(name, 0.0) for fold in active_weight_folds]
        for name in group_names}
    mean_weights = {
        name: sum(values) / len(values) if values else 0.0
        for name, values in weight_values.items()}
    weight_ranges = {
        name: max(values) - min(values) if values else 0.0
        for name, values in weight_values.items()}
    max_weight_range = max(weight_ranges.values()) if weight_ranges else None
    weights_stable = (zero_weight_folds == 0 and
                      max_weight_range is not None and
                      max_weight_range <= config.GROUP_WEIGHT_MAX_FOLD_RANGE)
    # 二层 stacking 多占一个严格早期 warm-up 折；仍要求四个完整 OOS 折
    # 全部满足原来的 4 折稳定门，不能用同折预测补齐第五折。
    eligible = (len(folds) >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and skill > 0 and
                good_brier >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                good_ev >= config.ENTRY_MODEL_MIN_GOOD_FOLDS and
                len(selected_all) >= config.MODEL_MIN_SELECTED_EVALUATIONS and
                lower is not None and lower > 0 and weights_stable)
    return {
        "status": ("eligible_for_shadow_review" if eligible
                   else "stop_no_promotion"),
        "groups": group_names, "folds": folds, "brier_skill": skill,
        "good_brier_folds": good_brier, "good_ev_folds": good_ev,
        "selected_n": len(selected_all),
        "oos_net_ev": (sum(selected_all) / len(selected_all)
                       if selected_all else None),
        "oos_net_ev_lower_bound": lower,
        "equal_weight_oos_ev": (sum(equal_all) / len(equal_all)
                                if equal_all else None),
        "signal_score_oos_ev": (sum(signal_all) / len(signal_all)
                                if signal_all else None),
        "mean_weights": mean_weights, "weight_ranges": weight_ranges,
        "max_weight_range": max_weight_range,
        "active_weight_folds": len(active_weight_folds),
        "zero_weight_folds": zero_weight_folds,
        "weights_stable": weights_stable,
        "eligible_for_shadow": eligible, "research_only": True,
    }

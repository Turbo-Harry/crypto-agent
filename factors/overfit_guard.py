"""
防过拟合守卫（overfit_guard）—— Deflated Sharpe + CSCV-PBO。

依据（López de Prado）:
  - Deflated Sharpe Ratio (DSR): 考虑非正态(偏度/峰度)与多重试验次数 N 后的
    Sharpe 显著性; DSR ≥ 1 才可接受。公式见 Bailey & López de Prado (2014),
    "The Deflated Sharpe Ratio" (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)。
  - Probability of Backtest Overfitting (PBO): CSCV(组合对称交叉验证)估计
    "样本内最优配置在样本外跑输中位数"的概率; PBO < 0.3 才可接受。
    (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)

零依赖(仅 numpy 与标准库);逆正态 CDF 用 Acklam 近似。
用法: 因子验证门(factors/factor_gate.py)与进化门(decision/evolution_gate.py)的钩子。
"""
import math
from itertools import combinations

try:
    import numpy as np
except ImportError:
    np = None

EULER_GAMMA = 0.5772156649015329


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _inv_norm_cdf(p):
    """标准正态逆 CDF（Acklam 近似，误差 < 1.15e-9）。"""
    if p <= 0 or p >= 1:
        raise ValueError("p 必须 ∈ (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(returns, n_trials=1):
    """DSR(≥1 才可接受)。returns: 周期收益序列(list/np)。n_trials: 多重试验次数。"""
    r = np.asarray(returns, dtype=float) if np is not None else None
    if r is None or len(r) < 3:
        return None
    n = len(r)
    mu = float(np.mean(r))
    sd = float(np.std(r, ddof=1))
    if sd == 0:
        return 0.0
    sr = mu / sd
    skew = float(((r - mu) ** 3).mean() / (sd ** 3)) if sd > 0 else 0.0
    kurt = float(((r - mu) ** 4).mean() / (sd ** 4))
    # 期望最大 SR 基准(多重试验校正, Bailey & LdP)
    var_sr = (1 - skew * sr + (kurt - 1) / 4 * sr * sr) / (n - 1)
    if n_trials <= 1:
        sr0 = 0.0   # 单次试验无多重检验负担
    else:
        z1 = _inv_norm_cdf(1 - 1 / n_trials)
        z2 = _inv_norm_cdf(1 - 1 / (n_trials * math.e))
        sr0 = math.sqrt(var_sr) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)
    dsr = _norm_cdf((sr - sr0) * math.sqrt(n - 1)
                    / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    return round(float(dsr), 4)


def pbo_cscv(returns_matrix, n_blocks=16, max_combos=100):
    """CSCV 估计 PBO。returns_matrix: (T 观测 × S 配置) 二维数组。
    返回 PBO ∈ [0,1]; <0.3 才可接受。数据不足返回 None。"""
    mat = np.asarray(returns_matrix, dtype=float) if np is not None else None
    if mat is None or mat.ndim != 2:
        return None
    t, s = mat.shape
    if t < n_blocks * 2 or s < 2:
        return None
    block_len = t // n_blocks
    mat = mat[:block_len * n_blocks]
    blocks = [mat[i * block_len:(i + 1) * block_len] for i in range(n_blocks)]
    idx = list(range(n_blocks))
    combos = list(combinations(idx, n_blocks // 2))
    if len(combos) > max_combos:
        combos = combos[:max_combos]
    underperf = 0
    for is_set in combos:
        is_mask = np.array([i in is_set for i in idx])
        is_data = np.concatenate([blocks[i] for i in idx if is_mask[i]], axis=0)
        oos_data = np.concatenate([blocks[i] for i in idx if not is_mask[i]], axis=0)
        # 样本内最优配置(按 Sharpe)
        is_sharpe = is_data.mean(axis=0) / (is_data.std(axis=0) + 1e-12)
        best = int(np.argmax(is_sharpe))
        oos_returns = oos_data[:, best]
        oos_sharpe = oos_returns.mean() / (oos_returns.std() + 1e-12)
        all_oos = oos_data.mean(axis=0) / (oos_data.std(axis=0) + 1e-12)
        # 样本内最优在样本外的相对秩
        rank = float((all_oos < oos_sharpe).mean())
        if rank < 0.5:
            underperf += 1
    pbo = underperf / len(combos)
    return round(pbo, 4)

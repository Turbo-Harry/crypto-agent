"""结构候选留样：在任何规则、AI 或额度门之前建立无选择偏差样本。

本模块只构建快照并写 SQLite，不调用交易所、不下单。唯一键把 5 分钟扫描
对同一根 15m K 的重复命中压成一次真实机会；参数变化通过 config_hash 进入
strategy_version，避免把不同规则版本混在一起。
"""
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

import config
from decision.signal_identity import config_identity, jsonable as _jsonable


_TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                 "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}


def _kline_ms(value: Any) -> int:
    ts = int(float(value))
    return ts * 1000 if abs(ts) < 100_000_000_000 else ts


def build_sample(base: str, sig: Dict[str, Any], venue: str,
                 event_ts: Optional[float] = None) -> Dict[str, Any]:
    """把结构信号冻结成可复算快照；缺关键障碍时显式报错，禁止脏样本。"""
    event_ts = float(event_ts if event_ts is not None else time.time())
    direction = str(sig.get("dir") or "")
    if direction not in ("long", "short"):
        raise ValueError("signal direction must be long or short")
    kline_ts = _kline_ms(sig.get("kline_ts"))
    entry = float(sig.get("entry") or 0)
    stop = float(sig.get("stop") or 0)
    tp = float(sig.get("tp") or 0)
    atr = float(sig.get("atr") or 0)
    if min(entry, stop, tp, atr) <= 0:
        raise ValueError("signal entry/stop/tp/atr must be positive")

    strategy_id = str(sig.get("strategy_id") or
                      config.ENTRY_SIGNAL_STRATEGY_ID)
    version, config_hash = config_identity(strategy_id)
    identity = (f"{base}|{direction}|{config.SIGNAL_SAMPLE_TIMEFRAME}|"
                f"{kline_ts}|{version}")
    signal_id = "sig_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    dims = dict(sig.get("shadow_dims") or {})
    factor_features = dict(sig.get("factor_features") or {})
    missing = [name for name in config.SHADOW_DIMS if dims.get(name) is None]
    missing.extend(name for name, value in factor_features.items()
                   if value is None and name not in missing)
    features = {
        "strategy_id": strategy_id,
        "shadow_score": sig.get("shadow_score"),
        "shadow_dims": dims,
        "regime": sig.get("regime"),
        "market_regime": sig.get("market_regime"),
        "strategy_route": sig.get("strategy_route"),
        "targets": sig.get("targets"),
        "forecast": sig.get("forecast"),
        "factor_features": factor_features,
    }
    frame_ms = _TIMEFRAME_MS.get(config.SIGNAL_SAMPLE_TIMEFRAME, 0)
    latency = max(0.0, event_ts * 1000 - (kline_ts + frame_ms))
    return {
        "signal_id": signal_id, "symbol": base, "direction": direction,
        "event_ts": event_ts, "kline_ts": kline_ts,
        "timeframe": config.SIGNAL_SAMPLE_TIMEFRAME, "venue": venue,
        "strategy_id": strategy_id,
        "strategy_version": version, "config_hash": config_hash,
        "feature_schema_version": config.SIGNAL_FEATURE_SCHEMA_VERSION,
        "entry": entry, "stop": stop, "tp": tp, "atr": atr,
        "horizon_hours": config.SIGNAL_OUTCOME_HORIZON_HOURS,
        **{name: dims.get(name) for name in config.SHADOW_DIMS},
        "features": json.dumps(_jsonable(features), sort_keys=True,
                               ensure_ascii=False),
        "missing_features": json.dumps(missing, ensure_ascii=False),
        "source_latency_ms": round(latency, 3),
        "created_at": event_ts, "updated_at": event_ts,
    }


def record_signal_sample(base: str, sig: Dict[str, Any], venue: str,
                         db_path: Optional[str] = None,
                         event_ts: Optional[float] = None) -> Tuple[str, bool]:
    """幂等写入候选，返回 (signal_id, 是否首次写入)。"""
    import storage.db as sdb
    sample = build_sample(base, sig, venue, event_ts=event_ts)
    sdb.init_db(db_path)
    columns = list(sample)
    with sdb.tx(db_path=db_path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO signal_samples ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [sample[name] for name in columns])
        created = cur.rowcount == 1
    return sample["signal_id"], created


def update_signal_decision(signal_id: str, db_path: Optional[str] = None,
                           rule_decision: Optional[str] = None,
                           ai_verdict: Optional[str] = None,
                           final_decision: Optional[str] = None,
                           reject_reason: Optional[str] = None,
                           trade_id: Optional[str] = None) -> None:
    """按决策链进度更新首次候选；调用方不传的字段保持原值。"""
    import storage.db as sdb
    values = {
        "rule_decision": rule_decision, "ai_verdict": ai_verdict,
        "final_decision": final_decision, "reject_reason": reject_reason,
        "trade_id": trade_id,
    }
    updates = [(name, value) for name, value in values.items() if value is not None]
    if not updates:
        return
    sql = ", ".join(f"{name}=?" for name, _ in updates) + ", updated_at=?"
    params = [value for _, value in updates] + [time.time(), signal_id]
    sdb.x(f"UPDATE signal_samples SET {sql} WHERE signal_id=?",
          params, db_path=db_path)


def merge_sample_features(signal_id: str, patch: Dict[str, Any],
                          db_path: Optional[str] = None) -> None:
    """给同一候选追加影子模型输出；不覆盖原始信号时点字段。"""
    import storage.db as sdb
    row = sdb.q1("SELECT features FROM signal_samples WHERE signal_id=?",
                 [signal_id], db_path=db_path)
    if not row:
        return
    try:
        current = json.loads(row.get("features") or "{}")
    except Exception:
        current = {}
    current.update(_jsonable(patch))
    sdb.x("UPDATE signal_samples SET features=?,updated_at=? WHERE signal_id=?",
          [json.dumps(current, sort_keys=True, ensure_ascii=False), time.time(),
           signal_id], db_path=db_path)


def record_agent_proposal_sample(*, proposal, snapshot, geometry, run_id: str,
                                 event_ts: float, db_path=None) -> Tuple[str, dict]:
    """把已验证几何的 AI 提案接入共同标签链；始终标 shadow/rejected。"""
    from decision.entry_probability import preopen_2to1_decision

    sig = {
        "dir": proposal.direction,
        "entry": geometry["entry"], "stop": geometry["stop"],
        "tp": geometry["tp"], "atr": geometry["atr"],
        "kline_ts": snapshot.kline_ts,
        "strategy_id": config.AGENT_PROPOSAL_STRATEGY_ID,
        "shadow_score": None,
        "shadow_dims": {name: None for name in config.SHADOW_DIMS},
        "factor_features": {
            "atr_pct": snapshot.atr / snapshot.reference_entry,
            "trend_band_atr": ((snapshot.ema20_15m - snapshot.ema50_15m) /
                               snapshot.atr),
            "volume_ratio": snapshot.volume_ratio,
            "momentum_1h": snapshot.momentum_1h,
            "momentum_4h": snapshot.momentum_4h,
            **dict(snapshot.market_features),
        },
        "regime": {"source": "agent_proposal_shadow"},
    }
    signal_id, _ = record_signal_sample(
        proposal.base, sig, "swap", db_path=db_path, event_ts=event_ts)
    rr_decision = preopen_2to1_decision(sig, db_path=db_path)
    merge_sample_features(signal_id, {
        "agent_proposal": {
            "run_id": run_id, "confidence": proposal.confidence,
            "thesis": proposal.thesis,
            "evidence_ids": proposal.evidence_ids,
            "execution_authority": False,
        },
        "preopen_2to1": rr_decision,
    }, db_path=db_path)
    update_signal_decision(
        signal_id, db_path=db_path, rule_decision="shadow",
        ai_verdict="proposal", final_decision="rejected",
        reject_reason=("agent_proposal_shadow:prediction_passed"
                       if rr_decision.get("passed") else
                       "agent_proposal_shadow:" + str(
                           rr_decision.get("reason") or "prediction_missing")))
    return signal_id, rr_decision

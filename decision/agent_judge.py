# -*- coding: utf-8 -*-
"""
AI 把关 + AI 记忆（2026-08-23 用户问"agent也会加入判断吗"、"AI会学习历史经验吗"）——
下单前把信号快照交给 DeepSeek 判断,返回 approve/reject/abstain。

设计红线（宁可做对,也不做错）:
  - 只否决、不放行: 只有 AI 明确说 reject 才拦单;
    approve/abstain/超时/解析失败/API 不可用 → 一律放行。
    交易链路绝不被 AI 可用性绑架。
  - RAG 式学习历史经验: 每次判断落 ai_judgments(含后续结果);
    下次判断把【带结果的旧案例】+【该币 trusted/discarded 教训】回喂 AI。
    不用微调,用检索——可控、可解释、可回滚。
"""
import json
import os
import re
import time
import urllib.request

import config

SYSTEM_PROMPT = (
    "你是加密货币短线交易系统的下单前把关人。系统已按严格规则选出信号,"
    "你只负责拦下【有明显风险】的单,不寻找理由拒绝。输出纯 JSON:"
    '{"verdict":"approve"|"reject"|"abstain",'
    '"risk_probability":0到1,"reason_code":"news_conflict"|'
    '"extreme_market"|"data_conflict"|"none"|"uncertain",'
    '"reason":"一句话理由"}。'
    "否决标准(满足任一): ①消息面与方向明显冲突(刚爆重大利空却开多、"
    "重大利好却开空) ②市场处于极端状态(闪崩/插针/流动性枯竭) ③信号数据"
    "自相矛盾。会提供你过去的判断案例(带结果)与本币历史教训,参考但不要"
    "被旧案例带偏——旧案例只能佐证,最终以当前信号本身为准。"
    "不确定就 approve;没把握就给 abstain。绝不输出 JSON 以外的内容。"
)

HARNESS_PROMPT_VERSION = config.AGENT_HARNESS_PROMPT_VERSION
HARNESS_CONTEXT_VERSION = config.AGENT_HARNESS_CONTEXT_VERSION
HARNESS_RETRIEVAL_VERSION = config.AGENT_HARNESS_RETRIEVAL_VERSION
HARNESS_SYSTEM_PROMPT = config.AGENT_HARNESS_SYSTEM_PROMPT


def _read_key():
    try:
        with open(os.path.expanduser("~/.dsh/.credentials.yaml")) as f:
            m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", f.read())
            return m.group(1) if m else None
    except Exception:
        return None


def _request_llm(user_prompt, key=None, url=None, model=None, timeout=None,
                 system_prompt=None, json_mode=False, max_tokens=None,
                 temperature=None):
    key = key or _read_key()
    if not key:
        return None
    url = url or getattr(config, "AGENT_JUDGE_API_URL", "")
    model = model or getattr(config, "AGENT_JUDGE_MODEL", "deepseek-chat")
    timeout = timeout or getattr(config, "AGENT_JUDGE_TIMEOUT_SECONDS", 20)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt or SYSTEM_PROMPT},
                     {"role": "user", "content": user_prompt}],
        "max_tokens": int(
            config.AGENT_JUDGE_MAX_OUTPUT_TOKENS
            if max_tokens is None else max_tokens),
        "temperature": float(
            config.AGENT_JUDGE_TEMPERATURE
            if temperature is None else temperature),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": "Mozilla/5.0 (crypto-agent trade-judge)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _call_llm(user_prompt, key=None, url=None, model=None, timeout=None,
              system_prompt=None):
    data = _request_llm(
        user_prompt, key=key, url=url, model=model, timeout=timeout,
        system_prompt=system_prompt)
    if not data:
        return None
    return data["choices"][0]["message"]["content"].strip()


def harness_model_available():
    """只报告 provider 是否可用，不暴露或缓存密钥。"""
    return bool(_read_key())


def production_harness_model_call(prompt, *, timeout_seconds=None):
    """Paper Harness 的受限 provider 回调；超时由 Harness 参数统一约束。"""
    from interfaces.agent import ModelCallResult
    timeout_seconds = (config.AGENT_HARNESS_TIMEOUT_MS / 1000.0
                       if timeout_seconds is None else float(timeout_seconds))
    data = _request_llm(
        prompt,
        timeout=max(0.001, min(
            config.AGENT_HARNESS_TIMEOUT_MS / 1000.0, timeout_seconds)),
        model=config.AGENT_HARNESS_MODEL,
        system_prompt=HARNESS_SYSTEM_PROMPT,
        json_mode=config.AGENT_HARNESS_JSON_MODE,
    )
    if not data:
        return None
    usage = data.get("usage") or {}
    usage_available = bool(usage)
    input_tokens = int(usage.get("prompt_tokens") or 0) if usage_available else None
    output_tokens = (int(usage.get("completion_tokens") or 0)
                     if usage_available else None)
    hit = (int(usage.get("prompt_cache_hit_tokens") or 0)
           if usage_available else None)
    miss_raw = usage.get("prompt_cache_miss_tokens")
    # 旧 provider 响应没有 cache 明细时，全部输入按 cache miss 保守计费。
    miss = (int(miss_raw) if miss_raw is not None else
            max(0, input_tokens - (hit or 0)) if input_tokens is not None else None)
    estimated_cost = ((
        (hit or 0) * config.AGENT_HARNESS_INPUT_CACHE_HIT_USD_PER_M +
        (miss or 0) * config.AGENT_HARNESS_INPUT_CACHE_MISS_USD_PER_M +
        (output_tokens or 0) * config.AGENT_HARNESS_OUTPUT_USD_PER_M
    ) / 1_000_000 if usage_available else None)
    return ModelCallResult(
        content=data["choices"][0]["message"]["content"].strip(),
        input_tokens=input_tokens, output_tokens=output_tokens,
        prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss,
        estimated_cost=estimated_cost,
        pricing_version=config.AGENT_HARNESS_PRICING_VERSION)


production_harness_model_call.model_version = config.AGENT_HARNESS_MODEL
production_harness_model_call.supports_timeout_budget = True


def parse_judgment(text):
    """标准化 AI 输出；解析失败与有效 abstain 分开记录。"""
    if not text:
        return {"verdict": "approve", "reason": "", "risk_probability": None,
                "reason_code": "none", "call_status": "no_output"}
    try:
        m = re.search(r"\{[^{}]*\}", text, re.S)
        obj = json.loads(m.group(0)) if m else {}
    except Exception:
        return {"verdict": "approve", "reason": str(text)[:80],
                "risk_probability": None, "reason_code": "none",
                "call_status": "parse_error"}
    if not obj:
        return {"verdict": "approve", "reason": str(text)[:80],
                "risk_probability": None, "reason_code": "none",
                "call_status": "parse_error"}
    verdict = str(obj.get("verdict", "abstain")).lower()
    reason = str(obj.get("reason", ""))[:120]
    if verdict not in ("approve", "reject", "abstain"):
        verdict = "abstain"
    try:
        risk = float(obj.get("risk_probability"))
        risk = max(0.0, min(1.0, risk))
    except (TypeError, ValueError):
        risk = None
    code = str(obj.get("reason_code") or
               ("uncertain" if verdict == "abstain" else "none"))[:40]
    return {"verdict": verdict, "reason": reason, "risk_probability": risk,
            "reason_code": code, "call_status": "valid"}


def parse_verdict(text):
    """兼容旧调用的二元组；详细状态由 parse_judgment 提供。"""
    parsed = parse_judgment(text)
    return parsed["verdict"], parsed["reason"]


def _memory_block(db_path, base, direction):
    """RAG 记忆块(2026-08-23): 带结果的旧判断案例 + 该币教训。"""
    if not getattr(config, "AGENT_JUDGE_MEMORY_ENABLED", False):
        return ""
    parts = []
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        # 旧案例: ≥24h 且已有结果,同方向优先,近期优先
        rows = sdb.q("SELECT * FROM ai_judgments WHERE "
                     "(outcome_r IS NOT NULL OR outcome_pnl IS NOT NULL) "
                     "AND direction=? AND ts < ? ORDER BY ts DESC LIMIT ?",
                     [direction,
                      time.time() - config.AGENT_JUDGE_MEMORY_MIN_HOURS * 3600,
                      config.AGENT_JUDGE_MEMORY_EXAMPLES], db_path=db_path)
        if rows:
            lines = []
            for r in rows:
                has_r = r.get("outcome_r") is not None
                outcome = (r.get("outcome_r") if has_r else
                           r.get("outcome_pnl"))
                ok = (float(outcome or 0) > 0) == (r["verdict"] == "approve")
                _tag = "对" if ok else "错"
                _dir_cn = "开多" if r["direction"] == "long" else "开空"
                _outcome_text = (f"{float(outcome or 0):+.2f}R" if has_r else
                                 f"{float(outcome or 0) * 100:+.1f}%")
                lines.append(
                    f"- {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} "
                    f"{r['base']} {_dir_cn} 分{int(r['score'] or 0)} → "
                    f"{r['verdict']} → 结果 {_outcome_text} "
                    f"(判断{_tag})")
            parts.append("你过去的判断案例(带结果):\n" + "\n".join(lines))
        # 该币教训
        lessons = sdb.q("SELECT status, content, good, bad FROM lessons "
                        "WHERE symbol=? AND status IN ('trusted','discarded') "
                        "ORDER BY (good-bad) DESC LIMIT ?",
                        [base, config.AGENT_JUDGE_LESSONS_TOP], db_path=db_path)
        if lessons:
            lines = [f"- [{l['status']}] {l['content'][:60]} "
                     f"(净验证 {int(l['good'] or 0) - int(l['bad'] or 0)})"
                     for l in lessons]
            parts.append(f"{base} 历史教训:\n" + "\n".join(lines))
    except Exception:
        pass
    return "\n".join(parts)


def judge(sig, base, score, price, sentiment, analyzer=None, db_path=None,
          signal_id=None):
    """对一笔即将开仓的信号做 AI 把关。返回 (verdict, reason, judgment_id)。
    analyzer: 测试注入(签名 user_prompt→str|None);默认走 DeepSeek。
    任何异常/超时/无钥匙仍 fail-open，但必须落 call_status，不能混入有效判断。"""
    if not getattr(config, "AGENT_JUDGE_ENABLED", False):
        return "approve", "", None
    dims = sig.get("shadow_dims") or {}
    sent = sentiment or {}
    tg = sig.get("targets") or {}
    mem = _memory_block(db_path, base, sig.get("dir"))
    user = (f"标的: {base} 永续合约\n"
            f"方向: {'开多' if sig.get('dir') == 'long' else '开空'}\n"
            f"信号分: {score}\n"
            f"6维子分: {json.dumps(dims, ensure_ascii=False)}\n"
            f"现价: {price}  止损: {sig.get('stop')}  止盈: {sig.get('tp')}\n"
            f"目标价位: {json.dumps(tg, ensure_ascii=False)}\n"
            f"预测: {json.dumps(sig.get('forecast') or {}, ensure_ascii=False)}\n"
            f"消息面: F&G={sent.get('fng_value')} 新闻情感={sent.get('news')} "
            f"合成={sent.get('composite')}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if mem:
        user += f"\n=== 历史经验(参考) ===\n{mem}"
    parsed = None
    try:
        text = analyzer(user) if analyzer else _call_llm(user)
        parsed = parse_judgment(text)
        if analyzer is None and text is None:
            parsed["call_status"] = "no_key"
    except TimeoutError:
        parsed = {"verdict": "approve", "reason": "", "risk_probability": None,
                  "reason_code": "none", "call_status": "timeout"}
    except Exception:
        parsed = {"verdict": "approve", "reason": "", "risk_probability": None,
                  "reason_code": "none", "call_status": "api_error"}
    verdict, reason = parsed["verdict"], parsed["reason"]
    jid = None
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        jid = sdb.x("INSERT INTO ai_judgments (ts, base, direction, score, "
                    "entry_price, verdict, reason,signal_id,call_status,"
                    "risk_probability,reason_code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [time.time(), base, sig.get("dir"), score, price,
                     verdict, reason, signal_id, parsed["call_status"],
                     parsed["risk_probability"], parsed["reason_code"]], db_path=db_path)
    except Exception:
        jid = None
    return verdict, reason, jid


def bind_trade(judgment_id, trade_id, db_path=None):
    """开仓成交后把判断行与交易绑定(平仓时回填结果)。"""
    if not judgment_id:
        return
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("UPDATE ai_judgments SET trade_id=? WHERE id=?",
              [trade_id, judgment_id], db_path=db_path)
    except Exception:
        pass


def record_trade_outcome(trade_id, pnl, db_path=None):
    """仅兼容没有 signal_id 的旧判断；新判断统一等当前 4h 路径结果。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("UPDATE ai_judgments SET outcome_pnl=?, outcome_ts=? "
              "WHERE trade_id=? AND outcome_pnl IS NULL AND signal_id IS NULL",
              [round(float(pnl or 0), 6), time.time(), trade_id],
              db_path=db_path)
    except Exception:
        pass


def harness_judge(sig, base, score, price, sentiment, *, model_call=None,
                  db_path=None, signal_id=None, account=None, health=None,
                  allow_veto=False):
    """Run the new Harness through an explicitly injected model callback.

    ``model_call`` is intentionally required for any model execution.  The
    compatibility module never opens a network client or chooses an endpoint;
    existing legacy ``judge`` remains the sole legacy provider path.
    """
    from decision.agent_contracts import AgentInput, HarnessConfig, stable_hash
    from decision.agent_harness import run_harness
    from decision.agent_policy import PolicyKernel

    sample = {}
    if signal_id:
        try:
            import storage.db as sdb
            sdb.init_db(db_path)
            sample = sdb.q1(
                "SELECT event_ts,kline_ts,strategy_id,strategy_version,timeframe,"
                "feature_schema_version,features,missing_features,source_latency_ms,"
                "rule_decision,final_decision FROM signal_samples WHERE signal_id=?",
                [signal_id], db_path=db_path) or {}
        except Exception:
            sample = {}
    timeframe = (sample.get("timeframe") or sig.get("timeframe") or
                 config.SIGNAL_SAMPLE_TIMEFRAME)
    strategy_id = str(sample.get("strategy_id") or sig.get("strategy_id") or
                      config.ENTRY_SIGNAL_STRATEGY_ID)
    if sample.get("strategy_version"):
        strategy_version = str(sample["strategy_version"])
    else:
        from decision.signal_identity import config_identity
        strategy_version = config_identity(strategy_id)[0]
    schema_version = (sample.get("feature_schema_version") or
                      config.SIGNAL_FEATURE_SCHEMA_VERSION)
    event_ts = sample.get("event_ts") or time.time()
    kline_ts = sample.get("kline_ts") or sig.get("kline_ts") or event_ts
    resolved_signal_id = signal_id or "signal-" + stable_hash({
        "base": base, "direction": sig.get("dir"), "timeframe": timeframe,
        "kline_ts": kline_ts, "strategy_version": strategy_version,
    })[:24]
    # run_id 与 Harness 的持久化幂等键使用相同稳定身份。否则跨五分钟桶重试
    # 会命中旧 agent_runs，却把 pending evaluation 写到新的孤立 run_id。
    model_version = str(getattr(model_call, "model_version", None) or
                        getattr(config, "AGENT_HARNESS_MODEL", "unknown"))
    tool_policy_version = config.AGENT_HARNESS_TOOL_POLICY_VERSION
    pricing_version = config.AGENT_HARNESS_PRICING_VERSION
    identity = {
        "signal_id": resolved_signal_id,
        "prompt_version": HARNESS_PROMPT_VERSION,
        "model_version": model_version,
        "context_version": HARNESS_CONTEXT_VERSION,
        "schema_version": schema_version,
        "retrieval_version": HARNESS_RETRIEVAL_VERSION,
        "tool_policy_version": tool_policy_version,
    }
    from decision.agent_lifecycle import version_for_identity, veto_effective
    lifecycle_version = version_for_identity(
        strategy_id=strategy_id, strategy_version=strategy_version,
        model_version=model_version,
        prompt_version=HARNESS_PROMPT_VERSION,
        context_version=HARNESS_CONTEXT_VERSION,
        schema_version=str(schema_version),
        retrieval_version=HARNESS_RETRIEVAL_VERSION,
        tool_policy_version=tool_policy_version,
        pricing_version=pricing_version)
    effective_veto = bool(allow_veto) and veto_effective(
        lifecycle_version, strategy_id=strategy_id, db_path=db_path)
    digest = stable_hash(identity)[:24]
    try:
        sample_features = json.loads(sample.get("features") or "{}")
        if not isinstance(sample_features, dict):
            sample_features = {}
    except (TypeError, ValueError, json.JSONDecodeError):
        sample_features = {}
    try:
        sample_missing = json.loads(sample.get("missing_features") or "[]")
        if not isinstance(sample_missing, list):
            sample_missing = []
    except (TypeError, ValueError, json.JSONDecodeError):
        sample_missing = []
    account_snapshot = dict(account or {})
    health_snapshot = {
        "missing_features": sample_missing,
        "source_latency_ms": sample.get("source_latency_ms"),
        "rule_decision": sample.get("rule_decision"),
        "final_decision_at_snapshot": sample.get("final_decision"),
        **dict(health or {}),
    }
    inp = AgentInput(
        run_id=f"agent-{digest}", signal_id=resolved_signal_id,
        event_ts=str(event_ts), kline_ts=str(kline_ts),
        strategy_version=strategy_version, prompt_version=HARNESS_PROMPT_VERSION,
        model_version=model_version, context_version=HARNESS_CONTEXT_VERSION,
        schema_version=schema_version,
        retrieval_version=HARNESS_RETRIEVAL_VERSION,
        tool_policy_version=tool_policy_version,
        pricing_version=pricing_version,
        signal={"base": base, "direction": sig.get("dir"), "score": score,
                "entry": price, "stop": sig.get("stop"), "tp": sig.get("tp"),
                "timeframe": timeframe,
                "shadow_dims": sig.get("shadow_dims") or {},
                "forecast": sig.get("forecast") or {},
                "preopen_2to1": sample_features.get("preopen_2to1") or {}},
        market={"regime": sig.get("regime"), "timeframe": timeframe,
                "frozen_features": sample_features},
        news=sentiment or {},
        account=account_snapshot,
        health=health_snapshot,
        field_provenance={
            "signal": f"signal:{resolved_signal_id}",
            "market": f"signal:{resolved_signal_id}:market",
            "news": f"signal:{resolved_signal_id}:news",
            "account": f"signal:{resolved_signal_id}:account",
            "health": f"signal:{resolved_signal_id}:health",
        })
    return run_harness(
        inp, baseline_passed=True, model_call=model_call,
        enabled=model_call is not None,
        config=HarnessConfig(
            max_steps=config.AGENT_HARNESS_MAX_STEPS,
            max_tools=config.AGENT_HARNESS_MAX_TOOL_CALLS,
            timeout_ms=config.AGENT_HARNESS_TIMEOUT_MS,
            max_semantic_retries=
                config.AGENT_HARNESS_MAX_SEMANTIC_RETRIES,
            max_context_chars=config.AGENT_HARNESS_CONTEXT_MAX_CHARS,
        ),
        # 用户已授权自动接入，但只有已通过生命周期验证的同一完整版本才有效。
        policy_kernel=PolicyKernel(
            veto_enabled=effective_veto, shadow=True,
            min_reject_risk=config.AGENT_HARNESS_REJECT_MIN_RISK,
            min_reject_confidence=config.AGENT_HARNESS_REJECT_MIN_CONFIDENCE),
        db_path=db_path)
def sweep_outcomes(exchange=None, db_path=None, horizon_hours=None):
    """从完整候选路径回填所有有效判断；不再用到期现价伪造结果。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        rows = sdb.q(
            "SELECT a.id,o.pnl_r,o.tp_first,o.sl_first,o.timeout "
            "FROM ai_judgments a JOIN signal_outcomes o "
            "ON o.signal_id=a.signal_id WHERE a.outcome_r IS NULL "
            "AND a.call_status='valid'", db_path=db_path)
        updated = 0
        for r in rows:
            try:
                sdb.x("UPDATE ai_judgments SET outcome_r=?,outcome_ts=?,"
                      "tp_first=?,sl_first=?,timeout=? WHERE id=?",
                      [r["pnl_r"], time.time(), r["tp_first"], r["sl_first"],
                       r["timeout"], r["id"]], db_path=db_path)
                updated += 1
            except Exception:
                continue
        return updated
    except Exception:
        return 0

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
    '{"verdict": "approve"|"reject"|"abstain", "reason": "一句话理由"}。'
    "否决标准(满足任一): ①消息面与方向明显冲突(刚爆重大利空却开多、"
    "重大利好却开空) ②市场处于极端状态(闪崩/插针/流动性枯竭) ③信号数据"
    "自相矛盾。会提供你过去的判断案例(带结果)与本币历史教训,参考但不要"
    "被旧案例带偏——旧案例只能佐证,最终以当前信号本身为准。"
    "不确定就 approve;没把握就给 abstain。绝不输出 JSON 以外的内容。"
)


def _read_key():
    try:
        with open(os.path.expanduser("~/.dsh/.credentials.yaml")) as f:
            m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", f.read())
            return m.group(1) if m else None
    except Exception:
        return None


def _call_llm(user_prompt, key=None, url=None, model=None, timeout=None):
    key = key or _read_key()
    if not key:
        return None
    url = url or getattr(config, "AGENT_JUDGE_API_URL", "")
    model = model or getattr(config, "AGENT_JUDGE_MODEL", "deepseek-chat")
    timeout = timeout or getattr(config, "AGENT_JUDGE_TIMEOUT_SECONDS", 20)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_prompt}],
        "max_tokens": 200,
        "temperature": getattr(config, "AGENT_JUDGE_TEMPERATURE", 0.2),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": "Mozilla/5.0 (crypto-agent trade-judge)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def parse_verdict(text):
    """从 LLM 输出里剥 JSON 取 verdict。解析失败/非 reject → 放行语义。"""
    if not text:
        return "approve", ""
    try:
        m = re.search(r"\{[^{}]*\}", text, re.S)
        obj = json.loads(m.group(0)) if m else {}
    except Exception:
        return "approve", text[:80]
    verdict = str(obj.get("verdict", "abstain")).lower()
    reason = str(obj.get("reason", ""))[:120]
    if verdict not in ("approve", "reject", "abstain"):
        verdict = "abstain"
    return verdict, reason


def _memory_block(db_path, base, direction):
    """RAG 记忆块(2026-08-23): 带结果的旧判断案例 + 该币教训。"""
    if not getattr(config, "AGENT_JUDGE_MEMORY_ENABLED", False):
        return ""
    parts = []
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        # 旧案例: ≥24h 且已有结果,同方向优先,近期优先
        rows = sdb.q("SELECT * FROM ai_judgments WHERE outcome_pnl IS NOT NULL "
                     "AND direction=? AND ts < ? ORDER BY ts DESC LIMIT ?",
                     [direction,
                      time.time() - config.AGENT_JUDGE_MEMORY_MIN_HOURS * 3600,
                      config.AGENT_JUDGE_MEMORY_EXAMPLES], db_path=db_path)
        if rows:
            lines = []
            for r in rows:
                ok = (float(r["outcome_pnl"] or 0) > 0) == (r["verdict"] == "approve")
                _tag = "对" if ok else "错"
                _dir_cn = "开多" if r["direction"] == "long" else "开空"
                lines.append(
                    f"- {time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} "
                    f"{r['base']} {_dir_cn} 分{int(r['score'] or 0)} → "
                    f"{r['verdict']} → 结果 {float(r['outcome_pnl'] or 0)*100:+.1f}% "
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


def judge(sig, base, score, price, sentiment, analyzer=None, db_path=None):
    """对一笔即将开仓的信号做 AI 把关。返回 (verdict, reason, judgment_id)。
    analyzer: 测试注入(签名 user_prompt→str|None);默认走 DeepSeek。
    任何异常/超时/无钥匙 → ("approve", "", None) 放行(不落判断表)。"""
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
    try:
        text = analyzer(user) if analyzer else _call_llm(user)
        verdict, reason = parse_verdict(text)
    except Exception:
        return "approve", "", None
    jid = None
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        jid = sdb.x("INSERT INTO ai_judgments (ts, base, direction, score, "
                    "entry_price, verdict, reason) VALUES (?,?,?,?,?,?,?)",
                    [time.time(), base, sig.get("dir"), score, price,
                     verdict, reason], db_path=db_path)
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
    """平仓回填 AI 判断的实际结果(复盘链调用)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        sdb.x("UPDATE ai_judgments SET outcome_pnl=?, outcome_ts=? "
              "WHERE trade_id=? AND outcome_pnl IS NULL",
              [round(float(pnl or 0), 6), time.time(), trade_id],
              db_path=db_path)
    except Exception:
        pass


def sweep_outcomes(exchange, db_path=None, horizon_hours=24):
    """被否决信号的'假如开了会怎样': 判断满 horizon 小时后按现价回填结果,
    AI 由此学习自己拦得对不对(拦下的单后来涨/跌了多少)。"""
    try:
        import storage.db as sdb
        sdb.init_db(db_path)
        rows = sdb.q("SELECT id, base, direction, entry_price, ts FROM "
                     "ai_judgments WHERE outcome_pnl IS NULL AND trade_id IS NULL "
                     "AND entry_price > 0 AND ts < ?",
                     [time.time() - horizon_hours * 3600], db_path=db_path)
        for r in rows:
            try:
                px = exchange.fetch_ticker_last(f"{r['base']}-USDT-SWAP")
                if not px:
                    continue
                pnl = (px / float(r["entry_price"]) - 1) \
                    if r["direction"] == "long" else \
                    (1 - px / float(r["entry_price"]))
                sdb.x("UPDATE ai_judgments SET outcome_pnl=?, outcome_ts=? "
                      "WHERE id=?", [round(float(pnl), 6), time.time(), r["id"]],
                      db_path=db_path)
            except Exception:
                continue
        return len(rows)
    except Exception:
        return 0

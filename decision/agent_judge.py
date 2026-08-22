# -*- coding: utf-8 -*-
"""
AI 把关（2026-08-23 用户问"agent也会加入判断吗"）——
下单前把信号快照交给 DeepSeek 判断,返回 approve/reject/abstain。

设计红线（宁可做对,也不做错）:
  - 只否决、不放行: 只有 AI 明确说 reject 才拦单;
    approve/abstain/超时/解析失败/API 不可用 → 一律放行。
    交易链路绝不被 AI 可用性绑架(AI 挂了只是少一道把关,不是少一次交易)。
  - 单信号一次调用、AGENT_JUDGE_TIMEOUT_SECONDS 超时;信号稀疏,成本可控。
  - 判断结果落 scan_decisions(ai_reject),留痕可复盘。
  - 判断人设: 只拦有明显风险的信号,不鸡蛋里挑骨头(阈值已很严)。
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
    "自相矛盾。不确定就 approve;没把握就给 abstain。绝不输出 JSON 以外的内容。"
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


def judge(sig, base, score, price, sentiment, analyzer=None):
    """对一笔即将开仓的信号做 AI 把关。返回 (verdict, reason)。
    analyzer: 测试注入(签名 user_prompt→str|None);默认走 DeepSeek。
    任何异常/超时/无钥匙 → ("approve", "") 放行。"""
    if not getattr(config, "AGENT_JUDGE_ENABLED", False):
        return "approve", ""
    dims = sig.get("shadow_dims") or {}
    sent = sentiment or {}
    user = (f"标的: {base} 永续合约\n"
            f"方向: {'开多' if sig.get('dir') == 'long' else '开空'}\n"
            f"信号分: {score}\n"
            f"6维子分: {json.dumps(dims, ensure_ascii=False)}\n"
            f"现价: {price}  止损: {sig.get('stop')}  止盈: {sig.get('tp')}\n"
            f"消息面: F&G={sent.get('fng_value')} 新闻情感={sent.get('news')} "
            f"合成={sent.get('composite')}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        text = analyzer(user) if analyzer else _call_llm(user)
        verdict, reason = parse_verdict(text)
    except Exception:
        return "approve", ""
    return verdict, reason

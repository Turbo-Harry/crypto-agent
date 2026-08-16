#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警 → AI 诊断桥（2026-08-16 用户方案:飞书异常告警自动带 AI 诊断,免人工转述）。

链路: 体检失败(launchd 每 5 分钟) → 本模块收集诊断材料(下单失败/引擎错误/
风控事件/交易概览/日志尾巴) → 调用本机 DeepSeek API 分析(密钥只读 ~/.dsh/
.credentials.yaml,绝不出机、绝不打印) → AI 诊断随告警发飞书(.lark CLI)。

安全边界:
  - AI 只做【只读诊断】,回复是文本建议,不接触任何交易接口;
  - API 失败 → 退回纯文本告警(告警永不因 AI 故障而丢失);
  - 30 分钟去重沿用 health_check 的 NOTIFY_STATE。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRED_FILE = os.path.expanduser("~/.dsh/.credentials.yaml")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
FEISHU_USER_ID = "ou_3c597d18937078f2587b56adb8b960d2"

SYSTEM_PROMPT = (
    "你是加密货币模拟盘交易系统的值守 AI。用户把体检异常转发给你,请你给出"
    "简明诊断与处置建议。约束:只读分析,不得建议绕过风控红线(单笔1%/名义150/"
    "总敞口600/交易所侧止损);不确定就说不确定,给出下一步排查命令建议;"
    "用中文回答,200 字以内,按'结论/原因/建议'三段。"
)


def read_key():
    """从 ~/.dsh/.credentials.yaml 读取 DEEPSEEK_API_KEY（绝不打印）。"""
    try:
        with open(CRED_FILE) as f:
            m = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", f.read())
        return m.group(1) if m else None
    except Exception:
        return None


def _q(db, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def build_diagnostics(db=None):
    """收集诊断材料(纯文本,随告警一起发给 AI)。"""
    db = db or os.path.join(ROOT, "crypto_agent.db")
    parts = []
    of = _q(db, "SELECT base, stage, error FROM order_failures "
                "ORDER BY ts DESC LIMIT 5")
    if of:
        parts.append("最近下单失败:\n" + "\n".join(
            f"- {r['base']} [{r['stage']}] {r['error']}" for r in of))
    ee = _q(db, "SELECT engine, error FROM engine_errors "
                "ORDER BY ts DESC LIMIT 3")
    if ee:
        parts.append("最近引擎错误:\n" + "\n".join(
            f"- {r['engine']}: {r['error'][:120]}" for r in ee))
    re_ = _q(db, "SELECT kind, reason, equity FROM risk_events "
                 "ORDER BY ts DESC LIMIT 3")
    if re_:
        parts.append("最近风控事件:\n" + "\n".join(
            f"- {r['kind']}: {r['reason'][:120]} (equity {r['equity']})"
            for r in re_))
    tr = _q(db, "SELECT symbol, direction, status, round(pnl,5) pnl "
                "FROM trades ORDER BY entry_time DESC LIMIT 5")
    if tr:
        parts.append("最近交易:\n" + "\n".join(
            f"- {r['symbol']} {r['direction']} {r['status']} pnl={r['pnl']}"
            for r in tr))
    try:
        log = subprocess.run(["tail", "-30", "/tmp/crypto-agent.out.log"],
                             capture_output=True, text=True, timeout=10).stdout
        parts.append("服务日志尾(30行):\n" + log[-2000:])
    except Exception:
        pass
    return "\n\n".join(parts) or "(无诊断材料)"


def analyze(failed_items, diag, key=None, url=API_URL, model=MODEL):
    """调 DeepSeek API 做诊断;失败返回 None(上层退回纯文本告警)。"""
    key = key or read_key()
    if not key:
        return None
    user = ("体检异常项:\n" + "\n".join(f"- {x}" for x in failed_items)
            + "\n\n诊断材料:\n" + diag[:6000])
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
        "max_tokens": 800,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": "Mozilla/5.0 (crypto-agent alert-diagnosis)",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def send_feishu(text):
    """复用 .lark CLI 发飞书;失败静默(与 notify 同策略)。"""
    try:
        subprocess.run([os.path.join(ROOT, ".lark"), "im", "+messages-send",
                        "--as", "bot", "--user-id", FEISHU_USER_ID,
                        "--text", text], capture_output=True, timeout=20)
    except Exception:
        pass


def _mailbox_write(failed_items):
    """告警入信箱(会话值守循环的待办队列)。"""
    try:
        import storage.db as sdb
        sdb.init_db()
        sig = ",".join(sorted(failed_items))
        dup = sdb.q1("SELECT id FROM alerts WHERE status='new' AND items=?",
                     [sig])
        if dup:
            return
        sdb.x("INSERT INTO alerts (ts, source, items, status) VALUES (?,?,?,?)",
              [time.time(), "health_check", sig, "new"])
    except Exception:
        pass


def diagnose_and_alert(failed_items, db=None):
    """告警主入口: 诊断材料 → AI 分析 → 飞书(告警+AI诊断)。返回发送的文本。"""
    _mailbox_write(failed_items)
    diag = build_diagnostics(db)
    analysis = analyze(failed_items, diag)
    if analysis:
        text = ("🚨 系统体检异常:\n" + "\n".join(f"- {x}" for x in failed_items)
                + "\n\n🤖 AI 初步诊断:\n" + analysis)
    else:
        text = ("🚨 系统体检异常:\n" + "\n".join(f"- {x}" for x in failed_items)
                + "\n\n(AI 诊断暂不可用,请人工查证 tools/health_check.py)")
    send_feishu(text)
    return text


if __name__ == "__main__":
    items = json.loads(sys.argv[1]) if len(sys.argv) > 1 else ["(自检)"]
    print(diagnose_and_alert(items))

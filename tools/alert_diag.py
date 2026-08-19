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

SYSTEM_PROMPT = (
    "你是加密货币模拟盘交易系统的值守 AI。用户把体检异常转发给你,请你给出"
    "简明诊断与处置建议。约束:只读分析,不得建议绕过风控红线(单笔1%/名义150/"
    "总敞口600/交易所侧止损);不确定就说不确定,给出下一步排查命令建议;"
    "用中文回答,200 字以内,按'结论/原因/建议'三段。"
    "事实背景:下单/持仓走 OKX 模拟盘(sandbox, x-simulated-trading),"
    "行情数据源是 Binance 公开数据端点,两者不要混淆;沙盘缺少部分生产合约"
    "(51001)或已退市(51087),下单失败错误码已穿透进 order_failures.error,"
    "报网络问题前先看错误码是不是 51001/51087。"
    "本地服务端口: 引擎 API 8090、服务 API 8899(异常接口 GET /anomalies),"
    "体检脚本 tools/health_check.py;不要建议不存在的端口(如 8000)或脚本。"
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
    of = _q(db, "SELECT base, stage, error, ts FROM order_failures "
                "ORDER BY ts DESC LIMIT 5")
    if of:
        import time as _t
        now = _t.time()
        parts.append("最近下单失败(含时间,供判断新旧):\n" + "\n".join(
            f"- {r['base']} [{r['stage']}] {r['error']}"
            f"（{now - r['ts']:.0f}s 前）" for r in of))
    # 2026-08-17: 异常处置状态进诊断材料——已 resolved 的项不得再被当成
    # 新故障(此前 AI 拿过期失败记录反复报'通道全灭',造成恐慌式误诊)。
    an = _q(db, "SELECT source, title, status, substr(detail,1,120) detail "
                "FROM anomalies ORDER BY id DESC LIMIT 8")
    if an:
        parts.append("异常中心最新状态(含处置说明):\n" + "\n".join(
            f"- [{r['status']}] {r['source']}: {r['title']} → {r['detail']}"
            for r in an))
    ru = _q(db, "SELECT symbol, category, conditions, strength, member_count "
                "FROM lesson_rollups ORDER BY strength DESC LIMIT 5")
    if ru:
        parts.append("场景归纳经验(教训聚合层):\n" + "\n".join(
            f"- {r['symbol']} {r['category']} {r['conditions']} "
            f"强度{r['strength']} 成员{r['member_count']}" for r in ru))
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


def _plain(text):
    """兼容旧名：会话注入通道不渲染 MD，剥标记。"""
    from decision.notify import plain
    return plain(text)


def send_feishu(text):
    """飞书发告警——走 decision.notify 的 interactive + lark_md 卡片。

    2026-08-17 三版演进: --text 纯文本不渲染;--markdown 包装为 post 的 md
    标签,飞书 post 同样不解析星号(dry-run 实证);只有卡片 lark_md 元素
    真正渲染 **加粗/列表/代码。2026-08-20 收进共享 notify,不再各写一套 CLI。
    """
    from decision.notify import notify
    title = "🚨 统一异常中心" if (text or "").startswith("🚨") else "📢 交易系统告警"
    notify(text, title=title, template="red")


def _register_anomalies(failed_items):
    """统一异常中心登记(2026-08-17: alerts 信箱退役,所有异常进 anomalies 表)。
    只推送本轮【新登记】的项(register 返回 True)——此前 list_new() 会把所有
    未解决异常每轮重推一遍,造成同一批告警重复轰炸(2026-08-17 噪音修复)。"""
    try:
        from tools.anomalies import register
        newly = []
        for item in failed_items:
            if register("health", item, severity="error"):
                newly.append(item)
        return [f"[error] health: {item}" for item in newly]
    except Exception:
        return [f"- {x}" for x in failed_items]


def inject_session(text):
    """POST /alert-inject 推入 DSH 当前会话(真·推送;失败静默,飞书仍兜底)。
    注入前同样做 Markdown 清洗(2026-08-17 用户反馈: 会话里也不渲染)。"""
    try:
        body = json.dumps({"text": _plain(text)}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:3080/alert-inject",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def diagnose_and_alert(failed_items, db=None):
    """告警主入口: 统一异常中心登记 → AI 分析 → 飞书 + 注入本 session(统一格式)。"""
    items = _register_anomalies(failed_items)
    diag = build_diagnostics(db)
    analysis = analyze(failed_items, diag)
    head = "🚨 统一异常中心:\n" + "\n".join(items)
    if analysis:
        text = head + "\n\n🤖 AI 初步诊断:\n" + analysis
    else:
        text = head + "\n\n(AI 诊断暂不可用,请人工查证 tools/health_check.py)"
    # 2026-08-17 渠道治理: 双通道并行导致同一告警被用户看到两遍(飞书+会话
    # 双份轰炸)。改主从: session 注入优先,成功则不再发飞书;注入失败才走
    # 飞书兜底(值守模式 = 浏览器开着的 session 是主通道)。
    if not inject_session(text):
        send_feishu(text)
    return text


if __name__ == "__main__":
    items = json.loads(sys.argv[1]) if len(sys.argv) > 1 else ["(自检)"]
    print(diagnose_and_alert(items))

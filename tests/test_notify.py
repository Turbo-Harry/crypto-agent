"""飞书通知：lark_md 卡片组装与 Markdown 清洗（不真正发消息）。

运行：PYTHONPATH=lib python3 tests/test_notify.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.notify import (to_lark_md, plain, build_card, notify,
                             _template_of, trade_notifications_enabled)
import decision.notify as nmod

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


class _Run:
    def __init__(self, code=0, recover=True):
        self.calls = []
        self.code = code
        self.recover = recover

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        class R:
            pass
        r = R()
        r.returncode = self.code
        if self.recover:
            self.code = 0
        return r


def test_to_lark_md():
    print("== to_lark_md 把 GitHub MD 收成飞书子集 ==")
    out = to_lark_md("### 结论\n正文")
    check("ATX 标题变加粗", out.startswith("**结论**"), out)
    out = to_lark_md("```python\nx=1\n```")
    check("围栏代码去掉 ```", "```" not in out and "x=1" in out, out)
    out = to_lark_md("* 第一项\n* 第二项")
    check("星号列表改成 - 列表", out.split("\n")[0].startswith("- "), out)
    out = to_lark_md("| a | b |\n| --- | --- |\n| 1 | 2 |")
    check("表格收成分隔文本", "---" not in out and "a · b" in out and "1 · 2" in out, out)
    check("加粗标记保留给 lark_md", "**入场**" in to_lark_md("**入场** 123"))


def test_plain():
    print("== plain 剥标记（--text / 会话兜底）==")
    check("剥 **", plain("盈亏 **+1.2 USDT**") == "盈亏 +1.2 USDT")
    check("剥标题符", "结论" in plain("## 结论") and "#" not in plain("## 结论"))
    check("剥行内代码", plain("用 `scan.py`") == "用 scan.py")


def test_card():
    print("== interactive 卡片结构 ==")
    msg = "🎯 开多 ETH\n入场 **3456.78**\n止损 3400"
    card = build_card(msg)
    check("header 是 plain_text", card["header"]["title"]["tag"] == "plain_text")
    check("header 不含星号", "**" not in card["header"]["title"]["content"])
    body = card["elements"][0]["text"]
    check("正文是 lark_md", body["tag"] == "lark_md")
    check("正文保留加粗", "**3456.78**" in body["content"])
    check("开仓蓝色", card["header"]["template"] == "blue")
    check("盈利平仓绿色", _template_of("📊 平仓 ETH\n盈亏 **+1.20 USDT**") == "green")
    check("亏损平仓红色", _template_of("📊 平仓 ETH\n盈亏 **-1.20 USDT**") == "red")
    check("熔断红色", _template_of("⛔ 风控熔断: 回撤") == "red")


def test_notify_cli():
    print("== notify 走卡片，失败才纯文本 ==")
    old_stat = nmod._stat
    # 通知格式/重试单测不应写共享 kv 统计；这里只验证 CLI 调用语义。
    nmod._stat = lambda *_: None
    fake = _Run(code=0)
    old = nmod.subprocess.run
    nmod.subprocess.run = fake
    try:
        notify("🎯 开多 ETH\n入场 **100**")
        check("发了一次", len(fake.calls) == 1, str(len(fake.calls)))
        args = fake.calls[0]
        check("msg-type=interactive", "--msg-type" in args and "interactive" in args)
        check("没有 --text", "--text" not in args)
        content = args[args.index("--content") + 1]
        payload = json.loads(content)
        check("content 是卡片 JSON", payload["elements"][0]["text"]["tag"] == "lark_md")
    finally:
        nmod.subprocess.run = old

    # 瞬时失败：第二次卡片重试成功，不应过早退回纯文本。
    fake = _Run(code=1, recover=True)
    nmod.subprocess.run = fake
    old_sleep = nmod.time.sleep
    nmod.time.sleep = lambda _: None
    try:
        notify("盈亏 **+1.2 USDT**")
        check("瞬时失败后重试卡片成功", len(fake.calls) == 2,
              str(len(fake.calls)))
        check("第二次仍是 interactive", "--msg-type" in fake.calls[1]
              and "--text" not in fake.calls[1])
    finally:
        nmod.subprocess.run = old

    # 持续失败：3 次卡片都失败后才退回去标记纯文本。
    fake = _Run(code=1, recover=False)
    nmod.subprocess.run = fake
    try:
        notify("盈亏 **+1.2 USDT**")
        check("持续失败共 3 次卡片 + 1 次兜底", len(fake.calls) == 4,
              str(len(fake.calls)))
        check("兜底是 --text", "--text" in fake.calls[3])
        txt = fake.calls[3][fake.calls[3].index("--text") + 1]
        check("兜底已剥星号", "**" not in txt and "+1.2" in txt, txt)
    finally:
        nmod.subprocess.run = old
        nmod.time.sleep = old_sleep
        nmod._stat = old_stat


def test_adapter_and_event_isolation():
    print("== 适配器通知判断 + JSONL 测试隔离 ==")
    check("原生 OKX 发通知", trade_notifications_enabled("okx"))
    check("ccxt OKX 发通知", trade_notifications_enabled("okx-ccxt"))
    check("FakeAdapter 自动静音", not trade_notifications_enabled("fake"))

    import tempfile
    import execution.events as events
    old_file = events.EVENTS_FILE
    old_env = os.environ.pop("CRYPTO_AGENT_EVENTS_FILE", None)
    try:
        with tempfile.TemporaryDirectory(prefix="event_isolation_") as tmp:
            db = os.path.join(tmp, "case.db")
            default_file = os.path.join(tmp, "global-events.jsonl")
            events.EVENTS_FILE = default_file
            check("临时 db 事件写入成功",
                  events.log_event("open", {"symbol": "BTC"}, db_path=db))
            isolated = db + ".events.jsonl"
            check("事件落在临时 db 同生命周期文件",
                  os.path.exists(isolated), isolated)
            check("默认活体事件文件未被触碰",
                  not os.path.exists(default_file))
            rows = events.tail_events(db_path=db)
            check("隔离事件可回读",
                  len(rows) == 1 and rows[0]["type"] == "open")
    finally:
        events.EVENTS_FILE = old_file
        if old_env is not None:
            os.environ["CRYPTO_AGENT_EVENTS_FILE"] = old_env


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    test_to_lark_md()
    test_plain()
    test_card()
    test_notify_cli()
    test_adapter_and_event_isolation()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

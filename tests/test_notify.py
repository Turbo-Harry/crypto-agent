"""飞书通知：lark_md 卡片组装与 Markdown 清洗（不真正发消息）。

运行：PYTHONPATH=lib python3 tests/test_notify.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.notify import to_lark_md, plain, build_card, notify, _template_of
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
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        class R:
            pass
        r = R()
        r.returncode = self.code
        # 第一次失败后，后续调用视为成功，避免无限兜底
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

    fake = _Run(code=1)
    nmod.subprocess.run = fake
    try:
        notify("盈亏 **+1.2 USDT**")
        check("卡片失败后兜底", len(fake.calls) == 2, str(len(fake.calls)))
        check("兜底是 --text", "--text" in fake.calls[1])
        txt = fake.calls[1][fake.calls[1].index("--text") + 1]
        check("兜底已剥星号", "**" not in txt and "+1.2" in txt, txt)
    finally:
        nmod.subprocess.run = old


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    test_to_lark_md()
    test_plain()
    test_card()
    test_notify_cli()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

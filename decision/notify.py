"""飞书通知（共享层）— 各引擎/分析师/告警统一从这里发。

飞书个人消息的 Markdown 不能当 GitHub MD 用：
  --text       纯文本，**加粗** 会原样显示星号
  --markdown   包装成 post 的 md 标签，实测同样不解析星号
  interactive  卡片里 tag=lark_md 的元素才会渲染加粗/列表/行内代码

因此本模块一律发 interactive 卡片；lark_md 不支持的语法（# 标题、```
代码块、表格）先收成它能渲染的子集。卡片发送失败再退回去标记的纯文本，
避免通知通道因格式问题哑火。
"""
import json
import os
import re
import subprocess

LARK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".lark")
FEISHU_USER_ID = "ou_3c597d18937078f2587b56adb8b960d2"


def plain(text):
    """剥掉 Markdown 标记，会话注入 / --text 兜底用（通道不渲染 MD）。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.M)
    return text


def to_lark_md(text):
    """把常见 Markdown 收成飞书 lark_md 子集（加粗/列表/行内代码/链接）。

    lark_md 不支持 ATX 标题、围栏代码块、表格——这些会变成满屏星号或原样
    符号，所以转换掉，而不是原样塞进卡片。
    """
    if not text:
        return text
    # ### 标题 → **加粗一行**
    text = re.sub(r"^#{1,6}\s+(.+)$", r"**\1**", text, flags=re.M)
    # ```lang ... ``` → 去掉围栏，内容按行保留
    def _fence(m):
        body = m.group(1).strip("\n")
        return body
    text = re.sub(r"```[\w+-]*\n?(.*?)```", _fence, text, flags=re.S)
    # 行首 "* " 列表（易与 *italic* 撞车）统一成 lark_md 的 "- "
    text = re.sub(r"^\*\s+", "- ", text, flags=re.M)
    # 表格：把 | a | b | 收成 "a / b"，丢掉分隔行
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if re.match(r"^\|?\s*:?-{3,}", s):
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            lines.append(" · ".join(c for c in cells if c))
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _header_and_body(msg):
    """首行做卡片标题（plain_text，不能带 MD）；其余（或整段）做 lark_md 正文。"""
    raw = (msg or "").strip("\n")
    if not raw:
        return "通知", ""
    lines = raw.split("\n")
    title = plain(lines[0]).strip() or "通知"
    if len(title) > 50:
        title = title[:50]
    rest = "\n".join(lines[1:]).strip()
    body_src = rest if rest else lines[0]
    return title, to_lark_md(body_src)


def _template_of(msg):
    """按文案选卡片颜色，方便一眼区分开仓/平仓盈亏/告警。"""
    if any(x in msg for x in ("⛔", "🚨", "❌", "熔断")):
        return "red"
    if "⚠️" in msg:
        return "orange"
    if "✅" in msg:
        return "green"
    if "平仓" in msg:
        # "+12" / "-3" 都可能带 **
        if re.search(r"盈亏\s*\**\+", msg):
            return "green"
        if re.search(r"盈亏\s*\**-", msg):
            return "red"
        return "blue"
    if any(x in msg for x in ("开多", "开空", "开仓")):
        return "blue"
    return "turquoise"


def build_card(msg, title=None, template=None):
    """组装飞书 interactive 卡片（纯数据，便于单测、不真正发）。"""
    h, body = _header_and_body(msg)
    header_title = title or h
    color = template or _template_of(msg)
    elements = []
    if body:
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": body}})
    else:
        elements.append({"tag": "div",
                         "text": {"tag": "plain_text", "content": header_title}})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color,
                   "title": {"tag": "plain_text", "content": header_title}},
        "elements": elements,
    }


def notify(msg, title=None, template=None):
    """发飞书。优先 interactive + lark_md；失败则 --text 纯文本兜底。"""
    if not msg:
        return
    card = build_card(msg, title=title, template=template)
    try:
        r = subprocess.run(
            [LARK, "im", "+messages-send", "--as", "bot",
             "--user-id", FEISHU_USER_ID, "--msg-type", "interactive",
             "--content", json.dumps(card, ensure_ascii=False)],
            capture_output=True, timeout=20)
        if getattr(r, "returncode", 0) == 0:
            return
    except Exception:
        pass
    try:
        subprocess.run(
            [LARK, "im", "+messages-send", "--as", "bot",
             "--user-id", FEISHU_USER_ID, "--text", plain(msg)],
            capture_output=True, timeout=20)
    except Exception:
        pass

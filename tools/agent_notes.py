#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 协作占用工具（2026-08-21 用户指示'建立协作机制'）——
多 agent 线并行提交同一仓库,用 AGENT_NOTES.md 的占用区协调文件级互斥。

用法:
  python3 tools/agent_notes.py status                 # 看活跃占用
  python3 tools/agent_notes.py claim <tag> <file...>  # 声明文件占用
  python3 tools/agent_notes.py release <tag> [commit] # 释放占用
  python3 tools/agent_notes.py conflicts <file...>    # 供 pre-commit 钩子调用

占用 60 分钟自动过期(防忘释放死锁)。
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES = os.path.join(ROOT, "docs", "AGENT_NOTES.md")
TTL = 3600


def _load():
    """解析占用区,返回 {tag: {"files": [...], "ts": float, "commit": str}}。"""
    try:
        src = open(NOTES, encoding="utf-8").read()
    except Exception:
        return {}
    m = re.search(r"<!-- AGENT_CLAIMS_BEGIN -->\n(.*?)\n<!-- AGENT_CLAIMS_END -->",
                  src, flags=re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = line[2:].split("|")
        if len(parts) < 3:
            continue
        tag, ts_s, files_s = parts[0].strip(), parts[1].strip(), parts[2].strip()
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        if time.time() - ts > TTL:
            continue   # 过期忽略(不落盘清理,保存时统一刷)
        out[tag] = {"files": [f.strip() for f in files_s.split(",") if f.strip()],
                    "ts": ts, "commit": parts[3].strip() if len(parts) > 3 else ""}
    return out


def _save(claims):
    src = open(NOTES, encoding="utf-8").read()
    lines = []
    for tag, c in sorted(claims.items()):
        files = ",".join(sorted(set(c["files"])))
        lines.append(f"- {tag} | {c['ts']:.0f} | {files} | {c.get('commit', '')}")
    block = ("<!-- AGENT_CLAIMS_BEGIN -->\n" + "\n".join(lines)
             + ("\n" if lines else "") + "<!-- AGENT_CLAIMS_END -->")
    src = re.sub(r"<!-- AGENT_CLAIMS_BEGIN -->\n(.*?)\n?<!-- AGENT_CLAIMS_END -->",
                 block, src, flags=re.S)
    open(NOTES, "w", encoding="utf-8").write(src)


def status():
    claims = _load()
    if not claims:
        print("✅ 无活跃占用")
        return 0
    for tag, c in sorted(claims.items()):
        age = int(time.time() - c["ts"])
        print(f"🔶 {tag} ({age}s 前): {', '.join(c['files'])}"
              + (f" [commit {c['commit']}]" if c.get("commit") else ""))
    return 0


def claim(tag, files):
    claims = _load()
    # 与其它活跃占用冲突检查
    mine = set(files)
    conflicts = []
    for other, c in claims.items():
        if other == tag:
            continue
        hit = mine & set(c["files"])
        if hit:
            conflicts.append((other, hit))
    if conflicts:
        for other, hit in conflicts:
            print(f"⚠️ 冲突: {other} 正占用 {', '.join(sorted(hit))}")
        print("请等对方释放或人工协调后重试。")
        return 1
    c = claims.get(tag, {"files": [], "ts": time.time(), "commit": ""})
    c["ts"] = time.time()
    c["files"] = list(set(c["files"]) | mine)
    claims[tag] = c
    _save(claims)
    print(f"✅ {tag} 占用: {', '.join(sorted(set(files)))}")
    return 0


def release(tag, commit=""):
    claims = _load()
    if tag in claims:
        c = claims.pop(tag)
        files = ", ".join(sorted(c["files"]))
        print(f"✅ {tag} 释放: {files}" + (f" → {commit}" if commit else ""))
    else:
        print(f"{tag} 无活跃占用")
    _save(claims)
    return 0


def conflicts(files):
    """pre-commit 钩子用: 检测本次提交文件是否被其它线占用。退出码 0=无冲突,
    1=有冲突(钩子只警告不阻止,这里返回 0 但打印警告)。"""
    claims = _load()
    mine = set(files)
    found = False
    for tag, c in claims.items():
        hit = mine & set(c["files"])
        if hit:
            found = True
            print(f"⚠️ 协作警告: {tag} 正占用 {', '.join(sorted(hit))}"
                  f"({int(time.time()-c['ts'])}s 前声明)——确认无并行编辑冲突")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        sys.exit(status())
    if cmd == "claim" and len(sys.argv) >= 4:
        sys.exit(claim(sys.argv[2], sys.argv[3:]))
    if cmd == "release" and len(sys.argv) >= 3:
        sys.exit(release(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))
    if cmd == "conflicts" and len(sys.argv) >= 3:
        sys.exit(conflicts(sys.argv[2:]))
    print(__doc__)
    sys.exit(1)

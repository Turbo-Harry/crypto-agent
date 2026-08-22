#!/usr/bin/env python3
"""AI 仓库入口自检：把文档地图和协作约束从约定变成机器检查。

检查范围只包含仓库结构与本地文档链接，不读取凭证、不连接交易所、不写状态。
运行：python3 tools/ai_repo_check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Set, Tuple
from urllib.parse import unquote, urlsplit


ROOT_ENTRY_FILES = ("README.md", "AGENTS.md", "llms.txt", "docs/README.md")
ALLOWED_ROOT_MARKDOWN = {"README.md", "AGENTS.md"}
REQUIRED_LLM_TARGETS = {
    "README.md",
    "AGENTS.md",
    "docs/README.md",
    "docs/AGENT_NOTES.md",
    "docs/architecture/ai_friendly_repo.md",
    "docs/reports/pitfalls.md",
    "docs/reports/optimization_notes.md",
}
REQUIRED_AGENT_GUIDANCE = {
    "CRYPTO_AGENT_MODE=paper": "AI 快速启动必须显式限定模拟盘",
    "python3 tools/agent_notes.py status": "写入前检查协作者占用",
    "python3 tools/ai_repo_check.py": "运行 AI 仓库自检",
    "python3 tools/code_graph.py --check": "运行分层检查",
    "docs/reports/pitfalls.md": "写代码前读取踩坑档案",
}

_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
_EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "data"}


def _outside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return False
    except ValueError:
        return True


def _content_without_fences(path: Path) -> Iterator[Tuple[int, str]]:
    in_fence = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield lineno, line


def _link_destination(raw: str) -> str:
    """提取 Markdown inline link 的目标，忽略可选 title 与片段。"""
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1:value.index(">")]
    else:
        value = re.split(r"\s+[\"']", value, maxsplit=1)[0]
    return unquote(value.strip().split("#", 1)[0])


def local_links(path: Path, root: Path) -> Iterator[Tuple[int, str, Path]]:
    """产生 (行号, 原目标, 绝对目标)；外链与纯锚点不在结果中。"""
    for lineno, line in _content_without_fences(path):
        for raw in _LINK_RE.findall(line):
            destination = _link_destination(raw)
            parsed = urlsplit(destination)
            if not destination or parsed.scheme.lower() in _EXTERNAL_SCHEMES:
                continue
            target = (path.parent / destination).resolve()
            yield lineno, destination, target


def markdown_sources(root: Path) -> List[Path]:
    sources = [root / "README.md", root / "AGENTS.md", root / "llms.txt"]
    sources.extend(sorted((root / "docs").rglob("*.md")))
    return [path for path in sources if path.exists()]


def linked_targets(path: Path, root: Path) -> Set[str]:
    targets: Set[str] = set()
    for _, _, target in local_links(path, root):
        if not _outside(root, target):
            targets.add(target.relative_to(root).as_posix())
    return targets


def check_repo(root: Path | str = Path.cwd()) -> List[str]:
    root = Path(root).resolve()
    errors: List[str] = []

    for relative in ROOT_ENTRY_FILES:
        if not (root / relative).is_file():
            errors.append(f"缺少 AI 入口文件: {relative}")

    root_markdown = {path.name for path in root.glob("*.md")}
    for name in sorted(root_markdown - ALLOWED_ROOT_MARKDOWN):
        errors.append(f"根目录存在散装 Markdown: {name}")

    for source in markdown_sources(root):
        relative_source = source.relative_to(root).as_posix()
        for lineno, destination, target in local_links(source, root):
            if _outside(root, target):
                errors.append(
                    f"{relative_source}:{lineno} 本地链接越出仓库: {destination}"
                )
            elif not target.exists():
                errors.append(
                    f"{relative_source}:{lineno} 本地链接失效: {destination}"
                )

    llms_path = root / "llms.txt"
    if llms_path.exists():
        llms_targets = linked_targets(llms_path, root)
        for relative in sorted(REQUIRED_LLM_TARGETS - llms_targets):
            errors.append(f"llms.txt 缺少关键入口: {relative}")

    agents_path = root / "AGENTS.md"
    if agents_path.exists():
        guidance = agents_path.read_text(encoding="utf-8")
        for snippet, purpose in REQUIRED_AGENT_GUIDANCE.items():
            if snippet not in guidance:
                errors.append(f"AGENTS.md 缺少操作护栏（{purpose}）: {snippet}")

    docs_root = root / "docs"
    docs_index = docs_root / "README.md"
    if docs_index.exists():
        indexed = linked_targets(docs_index, root)
        documents = {
            path.relative_to(root).as_posix()
            for path in docs_root.rglob("*.md")
            if path != docs_index
        }
        for relative in sorted(documents - indexed):
            errors.append(f"docs/README.md 未索引文档: {relative}")

    return errors


def _print_result(errors: Sequence[str]) -> int:
    if errors:
        print(f"AI 仓库自检失败：{len(errors)} 项")
        for index, error in enumerate(errors, 1):
            print(f"  {index}. {error}")
        return 1
    print("AI 仓库自检通过：入口、文档链接、llms.txt 与 docs 索引均一致")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1]
    return _print_result(check_repo(root))


if __name__ == "__main__":
    raise SystemExit(main())

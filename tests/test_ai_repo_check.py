"""AI 仓库入口与文档索引的机器守卫。

运行：python3 tests/test_ai_repo_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ai_repo_check import check_repo


passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


def _write(path: Path, content: str = "# 文档\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _minimal_repo(root: Path) -> None:
    _write(root / "README.md")
    _write(
        root / "AGENTS.md",
        "# 规则\n"
        "CRYPTO_AGENT_MODE=paper\n"
        "python3 tools/agent_notes.py status\n"
        "python3 tools/ai_repo_check.py\n"
        "python3 tools/code_graph.py --check\n"
        "docs/reports/pitfalls.md\n",
    )
    docs = [
        "docs/AGENT_NOTES.md",
        "docs/architecture/ai_friendly_repo.md",
        "docs/reports/pitfalls.md",
        "docs/reports/optimization_notes.md",
    ]
    for relative in docs:
        _write(root / relative)
    index_links = "\n".join(f"- [{path}]({path.removeprefix('docs/')})" for path in docs)
    _write(root / "docs/README.md", f"# 索引\n{index_links}\n")
    llms_targets = ["README.md", "AGENTS.md", "docs/README.md", *docs]
    llms_links = "\n".join(f"- [{path}]({path})" for path in llms_targets)
    _write(root / "llms.txt", f"# llms\n{llms_links}\n")


def test_current_repo() -> None:
    print("== 当前仓库 AI 入口自检 ==")
    errors = check_repo(Path(__file__).resolve().parents[1])
    check("当前仓库零漂移", not errors, "; ".join(errors[:5]))


def test_broken_link_is_caught() -> None:
    print("== 失效链接可被捕获 ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _minimal_repo(root)
        with (root / "llms.txt").open("a", encoding="utf-8") as handle:
            handle.write("- [失效](engines/missing.py)\n")
        errors = check_repo(root)
        check("失效路径报错", any("本地链接失效" in item for item in errors), str(errors))


def test_unindexed_doc_is_caught() -> None:
    print("== 未索引文档可被捕获 ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _minimal_repo(root)
        _write(root / "docs/ops/orphan.md")
        errors = check_repo(root)
        check("孤儿文档报错", any("未索引文档" in item for item in errors), str(errors))


def test_root_markdown_is_caught() -> None:
    print("== 根目录散装文档可被捕获 ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _minimal_repo(root)
        _write(root / "NOTES.md")
        errors = check_repo(root)
        check("散装 Markdown 报错", any("根目录存在散装" in item for item in errors), str(errors))


def test_missing_guidance_is_caught() -> None:
    print("== AGENTS 操作护栏回退可被捕获 ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _minimal_repo(root)
        _write(root / "AGENTS.md", "# 规则被误删\n")
        errors = check_repo(root)
        check("缺少 paper/协作/验证指引时报错",
              any("AGENTS.md 缺少操作护栏" in item for item in errors), str(errors))


def test_link_escape_is_caught() -> None:
    print("== 越出仓库的本地链接可被捕获 ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _minimal_repo(root)
        with (root / "README.md").open("a", encoding="utf-8") as handle:
            handle.write("[越界](../outside.md)\n")
        errors = check_repo(root)
        check("越界链接报错", any("本地链接越出仓库" in item for item in errors), str(errors))


if __name__ == "__main__":
    test_current_repo()
    test_broken_link_is_caught()
    test_unindexed_doc_is_caught()
    test_root_markdown_is_caught()
    test_missing_guidance_is_caught()
    test_link_escape_is_caught()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    raise SystemExit(1 if failed else 0)

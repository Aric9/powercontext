"""证据简报生成测试。"""

from __future__ import annotations

import re
from pathlib import Path

from readdesk.brief import generate_brief_markdown, write_brief
from readdesk.store import Store


def test_brief_markdown_structure(lib_store: Store) -> None:
    md = generate_brief_markdown(lib_store, "混合检索 分块 引用")
    assert "## 覆盖统计" in md
    assert "## 使用说明" in md
    citations = set(re.findall(r"[\w.\-/]+\.md#L\d+-L\d+", md))
    assert len(citations) >= 3
    assert "> " in md  # 引用原文


def test_brief_default_path(lib_store: Store) -> None:
    path = write_brief(lib_store, "本地优先 隐私")
    assert path.parent == lib_store.settings.briefs_dir
    assert path.exists() and path.suffix == ".md"
    text = path.read_text(encoding="utf-8")
    assert "证据简报：本地优先 隐私" in text


def test_brief_custom_out(lib_store: Store, tmp_path: Path) -> None:
    out = tmp_path / "sub" / "my-brief.md"
    path = write_brief(lib_store, "RRF 融合", out=out)
    assert path == out
    assert out.read_text(encoding="utf-8").startswith("# 证据简报：RRF 融合")

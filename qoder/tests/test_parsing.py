"""解析与分块测试。"""

from __future__ import annotations

from pathlib import Path

from readdesk.parsing import MAX_CHARS, MAX_OVERLAP_CHARS, iter_import_files, parse_document


def _total_len(body: str) -> int:
    return len(body)


def test_front_matter_fields(samples_dir: Path) -> None:
    doc = parse_document(samples_dir / "01-hybrid-retrieval.md", "01-hybrid-retrieval.md")
    assert doc.title.startswith("混合检索")
    assert doc.authors == ["林舟", "周其"]
    assert doc.year == 2025
    assert doc.tags == ["retrieval", "hybrid", "rag"]
    assert doc.venue == "内部技术报告"
    assert doc.doi == "10.0000/readdesk.2025.01"


def test_no_front_matter_uses_filename(tmp_path: Path) -> None:
    f = tmp_path / "note.md"
    f.write_text("# 标题\n\n正文一段。\n", encoding="utf-8")
    doc = parse_document(f, "note.md")
    assert doc.title == "note"
    assert doc.year is None and doc.tags == [] and doc.authors == []


def test_blocks_have_valid_line_ranges(samples_dir: Path) -> None:
    path = samples_dir / "02-chunking-strategies.md"
    doc = parse_document(path, path.name)
    n_lines = len(path.read_text(encoding="utf-8").splitlines())
    assert len(doc.blocks) >= 3
    for b in doc.blocks:
        assert 1 <= b.start_line <= b.end_line <= n_lines
        assert b.section
        assert b.text.startswith(b.section)  # 块文本 = 章节标题 + 正文


def test_block_sizes_and_overlap(samples_dir: Path) -> None:
    doc = parse_document(samples_dir / "05-local-first-rag.md", "05-local-first-rag.md")
    assert doc.blocks
    for b in doc.blocks:
        assert _total_len(b.body) <= MAX_CHARS + 200  # 单段超长时允许一个段落自成一块
    # 同文档相邻块的行号重叠字符数不超过上限
    for x, y in zip(doc.blocks, doc.blocks[1:]):
        if y.start_line <= x.end_line:
            overlap_lines = range(y.start_line, x.end_line + 1)
            lines = Path(samples_dir / "05-local-first-rag.md").read_text(encoding="utf-8").splitlines()
            chars = sum(len(lines[i - 1]) + 1 for i in overlap_lines)
            assert chars <= MAX_OVERLAP_CHARS + 60


def test_txt_without_headings(tmp_path: Path) -> None:
    f = tmp_path / "lorem.txt"
    para = "这是一段没有任何标题的中文正文，" * 30
    f.write_text(para + "\n\n" + para, encoding="utf-8")
    doc = parse_document(f, "lorem.txt")
    assert doc.title == "lorem"
    assert len(doc.blocks) >= 2
    assert all(b.section for b in doc.blocks)


def test_iter_import_files_recursive_with_skip(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("y", encoding="utf-8")
    (sub / ".hidden.md").write_text("z", encoding="utf-8")
    rd = tmp_path / ".readdesk"
    rd.mkdir()
    (rd / "skip.md").write_text("z", encoding="utf-8")
    (tmp_path / "c.pdf").write_text("z", encoding="utf-8")
    files = iter_import_files([tmp_path])
    rels = {rel for _, rel in files}
    assert rels == {"a.md", str(Path("sub") / "b.txt")}

"""Markdown/TXT 解析：front matter 与带行号的标题分块。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from readdesk.util import sha256_file, split_list_value

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*?)\s*$")
_SUPPORTED_SUFFIXES = {".md", ".txt"}

# 分块参数（SPEC 3.2）：目标 600–900 字符，重叠不超过 120 字符。
MAX_CHARS = 900
SOFT_MIN_CHARS = 600
MAX_OVERLAP_CHARS = 120


@dataclass
class Block:
    """一个正文块：章节标题 + 段落文本 + 1-based 闭区间行号。"""

    section: str
    text: str
    body: str
    start_line: int
    end_line: int


@dataclass
class ParsedDocument:
    """解析结果：文档级元数据与分块列表。"""

    rel_path: str
    title: str
    authors: list[str]
    year: int | None
    tags: list[str]
    venue: str
    doi: str
    sha256: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class _Para:
    """段落（连续非空行），保留行号用于计算块边界。"""

    start_line: int
    end_line: int
    text: str


def is_supported(path: Path) -> bool:
    """是否为可导入的 .md/.txt 文件（排除隐藏文件）。"""
    return path.suffix.lower() in _SUPPORTED_SUFFIXES and not path.name.startswith(".")


def iter_import_files(paths: list[Path]) -> list[tuple[Path, str]]:
    """展开文件/目录参数为 (文件, 相对路径) 列表；目录递归，跳过隐藏文件与 .readdesk。"""
    out: list[tuple[Path, str]] = []
    for root in paths:
        if root.is_file():
            if is_supported(root):
                out.append((root, root.name))
        elif root.is_dir():
            for f in sorted(root.rglob("*")):
                if not f.is_file() or not is_supported(f):
                    continue
                parts = f.relative_to(root).parts
                if any(p.startswith(".") or p == "node_modules" for p in parts):
                    continue
                out.append((f, str(f.relative_to(root))))
        else:
            raise FileNotFoundError(f"路径不存在：{root}")
    return out


def parse_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """解析文件开头 `---` 块，返回 (原始键值, 正文起始下标 0-based)。"""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta: dict[str, str] = {}
            for raw in lines[1:i]:
                m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
                if m:
                    meta[m.group(1).lower()] = m.group(2).strip()
            return meta, i + 1
    return {}, 0


def parse_document(path: Path, rel_path: str) -> ParsedDocument:
    """读取文件并解析元数据与分块；无 front matter 时用文件名作为标题。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    fm, body_start = parse_front_matter(lines)
    stem = path.stem
    year_raw = fm.get("year", "").strip()
    year = int(year_raw) if re.fullmatch(r"\d{4}", year_raw) else None
    doc = ParsedDocument(
        rel_path=rel_path,
        title=fm.get("title", "").strip() or stem,
        authors=split_list_value(fm.get("authors", "")),
        year=year,
        tags=[t.lower() for t in split_list_value(fm.get("tags", ""))],
        venue=fm.get("venue", "").strip(),
        doi=fm.get("doi", "").strip(),
        sha256=sha256_file(path),
    )
    doc.blocks = _chunk_lines(lines, body_start)
    return doc


def _chunk_lines(lines: list[str], body_start: int) -> list[Block]:
    """按 #/##/### 标题切节；无标题或超长节按段落窗口切分（重叠 ≤120 字符）。"""
    sections: list[tuple[str, int | None, list[_Para]]] = []
    section = ""
    heading_line: int | None = None
    paras: list[_Para] = []
    in_fence = False
    idx = body_start
    n = len(lines)
    while idx < n:
        line = lines[idx]
        lineno = idx + 1
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            idx += 1
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            if paras:
                sections.append((section, heading_line, paras))
                paras = []
            section = m.group(2)
            heading_line = lineno
            idx += 1
            continue
        if not line.strip():
            idx += 1
            continue
        buf, start = [line], lineno
        j = idx
        while j + 1 < n and lines[j + 1].strip() and not _HEADING_RE.match(lines[j + 1]) \
                and not lines[j + 1].lstrip().startswith("```"):
            j += 1
            buf.append(lines[j])
        paras.append(_Para(start, j + 1, "\n".join(buf)))
        idx = j + 1
    if paras:
        sections.append((section, heading_line, paras))

    blocks: list[Block] = []
    for sec_title, heading_line, paras in sections:
        if not sec_title and len(paras) == 1 and len(paras[0].text) <= MAX_CHARS:
            sec_title = "正文"
        windows = _window_paras(paras)
        for k, win in enumerate(windows):
            body = "\n".join(p.text for p in win)
            start = heading_line if (k == 0 and heading_line is not None) else win[0].start_line
            text = f"{sec_title}\n\n{body}" if sec_title else body
            blocks.append(Block(section=sec_title or "正文", text=text, body=body,
                                start_line=start, end_line=win[-1].end_line))
    return blocks


def _window_paras(paras: list[_Para]) -> list[list[_Para]]:
    """把段落聚合成 ≤900 字符的窗口，窗口间保留 ≤120 字符的段落重叠。"""
    windows: list[list[_Para]] = []
    cur: list[_Para] = []
    cur_len = 0
    for p in paras:
        if cur and cur_len + len(p.text) > MAX_CHARS:
            windows.append(cur)
            carry: list[_Para] = []
            acc = 0
            for q in reversed(cur):
                if acc + len(q.text) > MAX_OVERLAP_CHARS:
                    break
                carry.insert(0, q)
                acc += len(q.text)
            cur, cur_len = carry, acc
        cur.append(p)
        cur_len += len(p.text)
    if cur:
        windows.append(cur)
    return windows

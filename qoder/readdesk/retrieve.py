"""混合检索：dense + $contains 全文 + 元数据下推过滤，RRF 融合与证据片段生成。"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Any

from readdesk.embedding import embed, extract_query_terms
from readdesk.store import Store
from readdesk.util import normalize_tag, tag_field

RRF_K = 60
DENSE_TOPK = 30
FTS_TOPK = 30
TITLE_BOOST = 0.02
SECTION_BOOST = 0.01
SNIPPET_CHARS = 300


class QueryError(ValueError):
    """查询不合法（空查询等），中文消息。"""


@dataclass
class Evidence:
    """一条可回溯的证据段落。"""

    chunk_id: str
    doc_id: str
    path: str
    title: str
    section: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    matched_by: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    year: int | None = None
    text: str = ""

    @property
    def citation(self) -> str:
        """`相对路径#L起-L止` 引用串。"""
        return f"{self.path}#L{self.start_line}-L{self.end_line}"

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "title": self.title,
            "section": self.section,
            "citation": self.citation,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": round(self.score, 6),
            "snippet": self.snippet,
            "matched_by": self.matched_by,
            "tags": self.tags,
            "year": self.year,
        }


def build_where(tag: str | None = None, year: int | None = None) -> dict[str, Any]:
    """构造下推到 seekdb 的 where 条件（kind + tag 布尔字段 + year）。"""
    where: dict[str, Any] = {"kind": "chunk"}
    if tag:
        where[tag_field(normalize_tag(tag))] = True
    if year:
        where["year"] = int(year)
    return where


def hybrid_search(store: Store, query: str, tag: str | None = None, year: int | None = None,
                  limit: int = 10) -> list[Evidence]:
    """执行混合检索并返回按融合分数排序的证据列表。"""
    query = (query or "").strip()
    if not query:
        raise QueryError("查询不能为空，请输入关键词，例如：readdesk search 混合检索")
    terms = extract_query_terms(query)
    if not terms:
        raise QueryError(f"无法从查询中提取检索词：{query!r}")
    store.ensure_not_empty()
    where = build_where(tag, year)
    filtered = tag is not None or year is not None

    dense = store.dense_candidates(embed(query), where, n=DENSE_TOPK)
    fts_lists = [store.fts_candidates(t, where, limit=FTS_TOPK) for t in terms]

    scores: dict[str, float] = {}
    cand: dict[str, dict[str, Any]] = {}
    matched: dict[str, set[str]] = {}

    def add(c: dict[str, Any], src: str) -> None:
        cid = c["chunk_id"]
        cand.setdefault(cid, c)
        matched.setdefault(cid, set()).add(src)

    for rank, c in enumerate(dense, start=1):
        add(c, "dense")
        scores[c["chunk_id"]] = scores.get(c["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)
    for term, lst in zip(terms, fts_lists):
        for rank, c in enumerate(lst, start=1):
            add(c, "fts")
            scores[c["chunk_id"]] = scores.get(c["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)

    results: list[Evidence] = []
    for cid, base in scores.items():
        c = cand[cid]
        meta = c["metadata"]
        title = str(meta.get("title", ""))
        section = str(meta.get("section", ""))
        score = base
        if any(t in title.lower() for t in terms):
            score += TITLE_BOOST
        if any(t in section.lower() for t in terms):
            score += SECTION_BOOST
        srcs = set(matched[cid])
        if filtered:
            srcs.add("metadata")
        tags = [t for t in str(meta.get("tags_csv", "")).split(",") if t]
        year_v = int(meta.get("year", 0) or 0) or None
        text = c["text"] or str(meta.get("text", ""))
        evidence = Evidence(
            chunk_id=cid,
            doc_id=str(meta.get("doc_id", "")),
            path=str(meta.get("path", "")),
            title=title,
            section=section,
            start_line=int(meta.get("start_line", 0)),
            end_line=int(meta.get("end_line", 0)),
            score=score,
            snippet=build_snippet(text, terms),
            matched_by=sorted(srcs),
            tags=tags,
            year=year_v,
            text=text,
        )
        results.append(evidence)
    results.sort(key=lambda e: (-e.score, e.start_line, e.path))
    return results[:limit]


def _escape(text: str, as_html: bool) -> str:
    return _html.escape(text, quote=False) if as_html else text


def highlight_terms(text: str, terms: list[str], open_mark: str, close_mark: str, escape_html: bool = False) -> str:
    """把命中词包裹为高亮标记（CLI 用 `**`，Web 用 `<mark>`）；相邻/重叠区间自动合并。"""
    if not text or not terms:
        return _escape(text, escape_html)
    pattern = "|".join(re.escape(t) for t in sorted({t for t in terms if t}, key=len, reverse=True))
    spans: list[tuple[int, int]] = []
    if pattern:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            s, e = m.span()
            if spans and s <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], e))
            else:
                spans.append((s, e))
    out, pos = [], 0
    for s, e in spans:
        out.append(_escape(text[pos:s], escape_html))
        out.append(open_mark + _escape(text[s:e], escape_html) + close_mark)
        pos = e
    out.append(_escape(text[pos:], escape_html))
    return "".join(out)


def build_snippet(text: str, terms: list[str], max_chars: int = SNIPPET_CHARS) -> str:
    """生成 CLI 版证据片段：以首个命中词为中心截窗，并用 ** 高亮。"""
    return _snippet_window(text, terms, "**", "**", escape_html=False, max_chars=max_chars)


def build_snippet_html(text: str, terms: list[str], max_chars: int = SNIPPET_CHARS) -> str:
    """生成 Web 版证据片段（HTML 转义 + <mark> 高亮），返回值可安全 |safe。"""
    return _snippet_window(text, terms, "<mark>", "</mark>", escape_html=True, max_chars=max_chars)


def _snippet_window(text: str, terms: list[str], open_mark: str, close_mark: str,
                    escape_html: bool, max_chars: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    low = flat.lower()
    positions = [low.find(t.lower()) for t in terms if t]
    positions = [p for p in positions if p >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - 40)
    end = min(len(flat), start + max_chars)
    window = flat[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return prefix + highlight_terms(window, terms, open_mark, close_mark, escape_html) + suffix

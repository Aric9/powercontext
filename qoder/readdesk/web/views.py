"""把 Evidence 列表转换为模板友好的视图（含 <mark> 高亮片段）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from readdesk.retrieve import Evidence, build_snippet_html


@dataclass
class EvidenceView:
    """搜索结果卡片视图。"""

    doc_id: str
    title: str
    section: str
    citation: str
    score: float
    snippet_html: str
    matched_by: str
    tags: list[str]
    year: int | None
    url: str


def build_view_results(results: list[Evidence], terms: list[str]) -> list[EvidenceView]:
    """为每条证据生成高亮片段与文档链接。"""
    views: list[EvidenceView] = []
    for e in results:
        views.append(EvidenceView(
            doc_id=e.doc_id,
            title=e.title,
            section=e.section,
            citation=e.citation,
            score=e.score,
            snippet_html=build_snippet_html(e.text, terms),
            matched_by=" + ".join(e.matched_by),
            tags=e.tags,
            year=e.year,
            url=f"/doc/{e.doc_id}",
        ))
    return views


def chunks_to_views(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """文档详情页的块列表视图。"""
    out = []
    for b in blocks:
        out.append({
            "section": b.get("section", ""),
            "start_line": int(b.get("start_line", 0)),
            "end_line": int(b.get("end_line", 0)),
            "citation": f"{b.get('path', '')}#L{int(b.get('start_line', 0))}-L{int(b.get('end_line', 0))}",
            "text": b.get("text", ""),
        })
    return out

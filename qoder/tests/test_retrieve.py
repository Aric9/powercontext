"""混合检索：dense/fts/过滤下推、RRF 融合与引用格式。"""

from __future__ import annotations

import re

import pytest

from readdesk.retrieve import QueryError, build_where, highlight_terms, hybrid_search
from readdesk.store import Store


def test_hybrid_top1_hybrid_retrieval(lib_store: Store) -> None:
    results = hybrid_search(lib_store, "混合检索 怎么融合 BM25 和向量结果", limit=5)
    assert results, "应命中证据"
    top = results[0]
    assert top.path == "01-hybrid-retrieval.md"
    assert "RRF" in top.snippet
    assert {"dense", "fts"} <= set(top.matched_by)


def test_chunking_query_in_top3(lib_store: Store) -> None:
    results = hybrid_search(lib_store, "分块 重叠 比例", limit=5)
    paths = [r.path for r in results[:3]]
    assert "02-chunking-strategies.md" in paths


def test_tag_filter_pushdown(lib_store: Store) -> None:
    results = hybrid_search(lib_store, "评测 哈希 embedding", tag="evaluation", limit=10)
    assert results
    for r in results:
        assert "evaluation" in r.tags
    assert all(r.path.startswith("03") for r in results)


def test_year_filter_pushdown(lib_store: Store) -> None:
    results = hybrid_search(lib_store, "隐私 延迟 本地", year=2026, limit=10)
    assert results
    assert all(r.year == 2026 for r in results)
    assert "metadata" in results[0].matched_by


def test_citation_format(lib_store: Store) -> None:
    results = hybrid_search(lib_store, "引用 行号", limit=3)
    for r in results:
        assert re.fullmatch(r"[\w.\-/]+#L\d+-L\d+", r.citation), r.citation
        assert r.start_line <= r.end_line


def test_dedup_and_score_order(lib_store: Store) -> None:
    results = hybrid_search(lib_store, "混合检索 融合 RRF", limit=10)
    ids = [r.chunk_id for r in results]
    assert len(ids) == len(set(ids))
    assert results == sorted(results, key=lambda e: -e.score)


def test_empty_query_raises(lib_store: Store) -> None:
    with pytest.raises(QueryError):
        hybrid_search(lib_store, "   ")


def test_build_where_pushes_filters() -> None:
    w = build_where(tag="user-study", year=2025)
    assert w == {"kind": "chunk", "tag_user_study": True, "year": 2025}


def test_highlight_marks_and_merge() -> None:
    out = highlight_terms("混合检索与RRF融合", ["混合检索", "rrf", "融合"], "**", "**")
    assert out == "**混合检索**与**RRF融合**"  # 相邻命中合并为一个标记段
    html_out = highlight_terms("<b>RRF 融合</b>", ["RRF"], "<mark>", "</mark>", escape_html=True)
    assert html_out.startswith("&lt;b&gt;") and "<mark>RRF</mark>" in html_out

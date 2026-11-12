"""哈希 embedding 的确定性、归一化与相似度行为测试。"""

from __future__ import annotations

import math

from readdesk.config import EMBEDDING_DIM
from readdesk.embedding import embed, extract_query_terms, tokenize


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。"""
    return sum(x * y for x, y in zip(a, b))


def test_deterministic_and_dimension() -> None:
    v1 = embed("混合检索 RRF 融合 k=60")
    v2 = embed("混合检索 RRF 融合 k=60")
    assert len(v1) == EMBEDDING_DIM
    assert v1 == v2
    assert math.isclose(cosine(v1, v1), 1.0, abs_tol=1e-9)


def test_l2_normalized() -> None:
    v = embed("分块策略 章节切分 窗口 重叠 chunking")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-9)


def test_related_text_scores_higher() -> None:
    q = embed("混合检索怎么融合 BM25 和向量结果")
    related = embed("我们使用 BM25 与向量召回，再用 Reciprocal Rank Fusion 合并，RRF 平滑常数 k=60。")
    unrelated = embed("受访者平均维护 3.4 个笔记工具，最常用的是 Markdown 与 PDF 批注。")
    assert cosine(q, related) > cosine(q, unrelated)


def test_empty_text_is_zero_vector() -> None:
    assert set(embed("！！！，。")) in {0.0} or all(x == 0.0 for x in embed(""))


def test_tokenize_handles_mixed_text() -> None:
    toks = tokenize("RRF k=60 混合检索 user-study 2025")
    assert "rrf" in toks and "60" in toks and "user-study" in toks
    assert "混合检索" in toks or "混合" in toks


def test_extract_query_terms() -> None:
    terms = extract_query_terms("混合检索 怎么融合 BM25 和向量结果")
    assert "bm25" in terms
    assert any(t in ("混合", "检索", "混合检索") for t in terms)
    assert len(terms) <= 8

from __future__ import annotations

from pathlib import Path

from sdd_context.embedding import HashingEmbeddingFunction, split_identifier, tokenize

ROOT = Path(__file__).resolve().parents[2]


def test_embedding_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbeddingFunction(64)
    first = embedder.embed("累计退款上限 half-up REF-003")
    second = embedder.embed("累计退款上限 half-up REF-003")
    assert first == second
    assert len(first) == 64
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_embedding_separates_different_text() -> None:
    embedder = HashingEmbeddingFunction(128)
    first = embedder.embed("累计退款上限")
    second = embedder.embed("幂等重复请求")
    assert first != second


def test_identifier_split_and_tokens() -> None:
    assert "refund" in split_identifier("RefundService.refund_amount")
    assert "amount" in split_identifier("refund_amount")
    tokens = tokenize("使用 round_money 做 half-up 舍入")
    assert "round_money" in tokens
    assert "half-up" in tokens


def test_specs_extract_clauses_with_locators() -> None:
    from sdd_context.extract.specs import parse_specs

    chunks = parse_specs(ROOT / "business" / "specs", ROOT)
    clauses = {chunk.clause_id: chunk for chunk in chunks if chunk.clause_id}
    assert set(clauses) == {f"REF-00{i}" for i in range(1, 8)}
    ref003 = clauses["REF-003"]
    assert "累计成功退款金额" in ref003.text
    assert ref003.source == "business/specs/refund-policy.md"
    assert ref003.start_line <= ref003.end_line
    assert ref003.locator.startswith("business/specs/refund-policy.md#L")


def test_code_extract_finds_service_symbols() -> None:
    from sdd_context.extract.code import parse_code_file

    path = ROOT / "business" / "refund" / "service.py"
    chunks = parse_code_file(path, ROOT)
    symbols = {chunk.symbol for chunk in chunks}
    assert "RefundService.refund" in symbols
    assert "RefundService._reject" in symbols
    refund = next(chunk for chunk in chunks if chunk.symbol == "RefundService.refund")
    assert "def refund" in (refund.signature or "")
    assert "REF-003" in refund.spec_refs or "REF-005" in refund.spec_refs


def test_test_extract_captures_assertions() -> None:
    from sdd_context.extract.tests import parse_test_file

    path = ROOT / "business" / "tests" / "test_refund_target.py"
    chunks = parse_test_file(path, ROOT)
    names = {chunk.title for chunk in chunks}
    assert "test_cumulative_refunds_cannot_exceed_paid" in names
    target = next(chunk for chunk in chunks if chunk.title == "test_service_fee_rounds_half_up")
    assert "90.05" in target.text
    assert "REF-006" in target.spec_refs

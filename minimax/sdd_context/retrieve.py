"""Hybrid retrieval over the seekdb index.

Two native seekdb paths are fused with Reciprocal Rank Fusion (RRF):

* dense vector search over the local hashing embedding;
* full-text ``$contains`` filtering for exact clause ids, identifiers and
  domain terms.

The second stage re-queries with the selected spec clauses so that code symbols
and tests are retrieved by the requirement they implement, not only by surface
similarity to the task sentence.  A final deterministic pass guarantees that
declared change-scope files and test files are represented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdd_context.models import SourceItem
from sdd_context.store.seekdb_store import SeekDBStore

CLAUSE_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
HYPHEN_RE = re.compile(r"[A-Za-z]{2,}-[A-Za-z]{2,}")
PATH_RE = re.compile(r"[\w./-]+\.(?:py|md)")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

STOP_TERMS = {
    "test",
    "tests",
    "business",
    "sdd",
    "python",
    "refund",
    "service",
}

STOP_CJK = {
    "需要",
    "支持",
    "当前",
    "实现",
    "可以",
    "不得",
    "使用",
    "进行",
    "以及",
    "通过",
    "这个",
    "一个",
    "同一",
    "本次",
    "订单",  # too generic in this corpus; clause ids carry the specificity
    "退款",
    "金额",
    "服务",
    "要求",
    "提供",
    "根据",
    "相关",
    "所有",
    "必须",
    "如果",
    "超过",
    "规则",
    "代码",
    "测试",
}


@dataclass
class Candidate:
    id: str
    document: str
    metadata: dict[str, Any]
    rrf: float = 0.0
    dense_score: float = 0.0
    matched_by: list[str] = field(default_factory=list)


def task_text(task: dict[str, Any]) -> str:
    return " ".join(part for part in [task.get("title", ""), task.get("request", "")] if part).strip()


def _jieba_terms(text: str) -> list[str]:
    try:
        import jieba

        tokens = [token.strip() for token in jieba.cut(text)]
    except Exception:  # noqa: BLE001 - jieba is optional at runtime
        tokens = CJK_RUN_RE.findall(text)
    found: list[str] = []
    for token in tokens:
        if len(token) < 2 or not CJK_RUN_RE.fullmatch(token):
            continue
        if token in STOP_CJK:
            continue
        found.append(token)
    return found


def lexical_terms(task: dict[str, Any], limit: int = 10) -> list[tuple[str, float, str]]:
    """Return ``(term, weight, kind)`` probes for exact seekdb full-text search."""

    text = task_text(task)
    weighted: list[tuple[str, float, str]] = []
    for match in CLAUSE_RE.findall(text):
        weighted.append((match, 2.6, "clause"))
    for match in HYPHEN_RE.findall(text):
        weighted.append((match, 2.0, "hyphen"))
    for match in IDENT_RE.findall(text):
        lowered = match.lower()
        if lowered in STOP_TERMS or len(match) < 4:
            continue
        weight = 2.2 if ("_" in match or any(ch.isupper() for ch in match[1:])) else 1.4
        weighted.append((match, weight, "identifier"))
    for match in PATH_RE.findall(text):
        weighted.append((Path(match).stem, 1.2, "path"))
        weighted.append((match, 1.0, "path"))
    for match in _jieba_terms(text):
        weight = 0.9 if len(match) >= 3 else 0.5
        weighted.append((match, weight, "cjk"))
    for scope in task.get("change_scope", []) or []:
        stem = Path(scope).stem
        weighted.append((stem, 1.4, "scope"))
        weighted.append((f"{stem}.py", 1.2, "scope"))
    for keep in task.get("must_keep", []) or []:
        for match in CLAUSE_RE.findall(str(keep)):
            weighted.append((match, 2.6, "clause"))

    seen: set[str] = set()
    unique: list[tuple[str, float, str]] = []
    for term, weight, kind in weighted:
        key = term.lower()
        if key in seen or len(key) < 2:
            continue
        seen.add(key)
        unique.append((term, weight, kind))
    unique.sort(key=lambda item: -item[1])
    return unique[:limit]


def _add(candidates: dict[str, Candidate], hit: dict[str, Any], weight: float, rank: int, tag: str) -> None:
    candidate = candidates.get(hit["id"])
    if candidate is None:
        candidate = Candidate(id=hit["id"], document=hit.get("document", ""), metadata=hit.get("metadata", {}))
        candidates[hit["id"]] = candidate
    candidate.rrf += weight / (60.0 + rank + 1)
    candidate.dense_score = max(candidate.dense_score, float(hit.get("score", 0.0)))
    if tag not in candidate.matched_by:
        candidate.matched_by.append(tag)


def _rank(candidates: dict[str, Candidate], kind: str, limit: int) -> list[Candidate]:
    ranked = sorted(
        (
            candidate
            for candidate in candidates.values()
            if candidate.metadata.get("kind") == kind
            and candidate.metadata.get("scope") != "overview"
        ),
        key=lambda candidate: (-candidate.rrf, candidate.id),
    )
    return ranked[:limit]


def retrieve(task: dict[str, Any], store: SeekDBStore, per_kind: dict[str, int] | None = None) -> dict[str, Any]:
    """Run two-stage hybrid retrieval and return selected sources plus a trace."""

    per_kind = per_kind or {"spec": 8, "code": 7, "test": 8}
    candidates: dict[str, Candidate] = {}
    trace: dict[str, Any] = {"dense": [], "lexical": [], "selected": {}}

    def dense(query: str, weight: float, tag: str, n: int = 30) -> None:
        hits = store.dense_query(query, n_results=n)
        trace["dense"].append({"query": query[:160], "hits": [hit["id"] for hit in hits]})
        for rank, hit in enumerate(hits):
            _add(candidates, hit, weight, rank, tag)

    def contains(term: str, weight: float, kind: str | None = None, n: int = 40) -> None:
        hits = store.contains_query(term, n_results=n, kind=kind)
        trace["lexical"].append({"term": term, "kind": kind, "hits": [hit["id"] for hit in hits]})
        for rank, hit in enumerate(hits):
            _add(candidates, hit, weight, rank, f"fts:{term}")

    # Stage 1: locate the requirements from the task itself.
    dense(task.get("title", "") or task_text(task), 2.0, "dense:title")
    dense(task_text(task), 1.2, "dense:task")
    for keep in (task.get("must_keep") or [])[:3]:
        dense(str(keep), 0.8, "dense:must_keep")
    for scope in (task.get("change_scope") or [])[:2]:
        dense(f"{scope} implementation", 0.8, "dense:scope")
    for term, weight, _kind in lexical_terms(task):
        contains(term, weight)

    specs = _rank(candidates, "spec", per_kind["spec"])

    # Stage 2: retrieve implementation and tests for the selected requirements.
    for spec in specs[:4]:
        dense(spec.document[:900], 1.1, "dense:clause")
        clause_id = spec.metadata.get("clause_id")
        if clause_id:
            contains(str(clause_id), 1.8)

    # Guarantee declared files and their symbols are present.
    change_scope = {Path(path).as_posix() for path in task.get("change_scope", [])}
    for path in change_scope:
        for hit in store.get_by_source(path, limit=30):
            _add(candidates, hit, 2.0, 0, "scope:file")

    required_clauses = set(CLAUSE_RE.findall(task_text(task)))
    for item in task.get("must_keep", []) or []:
        required_clauses.update(CLAUSE_RE.findall(str(item)))
    declared_tests = {
        Path(path).as_posix() for path in (task.get("target_tests", []) + task.get("regression_tests", []))
    }
    for candidate in candidates.values():
        meta = candidate.metadata
        if meta.get("clause_id") in required_clauses:
            candidate.rrf += 0.05
            candidate.matched_by.append("task:clause")
        if meta.get("source") in declared_tests:
            candidate.rrf += 0.03

    code = _rank(candidates, "code", per_kind["code"])
    tests = _rank(candidates, "test", per_kind["test"])

    selected: dict[str, list[dict[str, Any]]] = {
        "spec": [_as_hit(item) for item in specs],
        "code": [_as_hit(item) for item in code],
        "test": [_as_hit(item) for item in tests],
    }

    present = {item["id"] for item in selected["test"]}
    for path in sorted(declared_tests):
        for hit in store.get_by_source(path, limit=20):
            if hit["id"] in present:
                continue
            selected["test"].append(
                {
                    "id": hit["id"],
                    "document": hit["document"],
                    "metadata": hit["metadata"],
                    "score": 0.0,
                    "matched_by": ["task:declared_test_file"],
                }
            )
            present.add(hit["id"])

    for kind, items in selected.items():
        trace["selected"][kind] = [item["id"] for item in items]
    trace["candidates"] = len(candidates)
    return {"selected": selected, "trace": trace}


def _as_hit(candidate: Candidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "document": candidate.document,
        "metadata": candidate.metadata,
        "score": candidate.rrf,
        "matched_by": candidate.matched_by,
    }


def to_source_item(hit: dict[str, Any], root: Path) -> SourceItem:
    metadata = hit["metadata"]
    document = hit["document"]
    return SourceItem(
        kind=str(metadata.get("kind", "")),
        title=str(metadata.get("title", "")),
        source=str(metadata.get("source", "")),
        locator=str(metadata.get("locator", "")),
        text=document,
        start_line=int(metadata.get("start_line", 0)),
        end_line=int(metadata.get("end_line", 0)),
        score=float(hit.get("score", 0.0)),
        clause_id=metadata.get("clause_id"),
        symbol=metadata.get("symbol"),
        signature=metadata.get("signature"),
        matched_by=list(hit.get("matched_by", [])),
        verified=False,
    )

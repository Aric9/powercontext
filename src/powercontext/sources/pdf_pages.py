# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Page-level PDF Sources with lexical evidence-page retrieval.

A financial report is captured one physical page at a time so a retrieval hit can be
traced back to an exact revision (document digest) and an exact physical page. The
retriever rewrites one natural-language question into several short lexical queries,
merges their candidates and re-ranks whole pages by query-term coverage, so a page that
only matches a fragment of a long question is not lost to a single admission threshold.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from itertools import pairwise
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_VERSION = "1"
_IDENTITY_PREFIX = "pdf"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"[a-z0-9]+")
# Candidate pool per lexical query; the ranker reduces it to whole pages afterwards.
_CANDIDATE_LIMIT = 100
_MAX_PROBE_TERMS = 12
_MIN_ASSUMED_CORPUS = 200
_PHRASE_BOOST = 1.5
_MAX_PHRASE_TERMS = 10

# Boilerplate that FinanceBench-style prompts add around the actual question. It carries
# no page-level evidence, and left in the query it would dilute term coverage.
_PROMPT_NOISE = frozenset([
    "according",
    "answer",
    "approach",
    "assume",
    "assuming",
    "assumption",
    "based",
    "brief",
    "calculate",
    "calculated",
    "calculation",
    "concise",
    "concisely",
    "consider",
    "considering",
    "define",
    "defined",
    "definition",
    "detail",
    "details",
    "directly",
    "explain",
    "explicitly",
    "figure",
    "following",
    "formula",
    "give",
    "given",
    "identify",
    "indicate",
    "information",
    "listed",
    "mention",
    "mentioned",
    "note",
    "outlined",
    "perspective",
    "please",
    "provide",
    "provided",
    "question",
    "relate",
    "related",
    "rely",
    "relying",
    "report",
    "reported",
    "reporting",
    "respond",
    "response",
    "section",
    "shown",
    "source",
    "specified",
    "state",
    "stated",
    "statement",
    "statements",
    "use",
    "using",
    "within",
])
_STOP_WORDS = frozenset([
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "might",
    "must",
    "no",
    "not",
    "of",
    "on",
    "or",
    "our",
    "over",
    "per",
    "s",
    "shall",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "under",
    "up",
    "us",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
])
# Terms that describe the whole document rather than one page; kept for ranking but not
# used to build the short queries that decide the candidate pool.
_GENERIC_FINANCE = frozenset([
    "company",
    "fiscal",
    "fy",
    "million",
    "millions",
    "total",
    "year",
    "years",
])


class PDFDocumentInput(BaseModel):
    """One complete original PDF to be captured page by page."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a parsed PDF revision."""

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def validate_page_identity(self) -> PDFPageInput:
        pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        return self


class PDFPageSource(Source):
    """Captured page whose identity pins the document revision and physical page."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def validate_page_source(self) -> PDFPageSource:
        if self.source_id != self.name:
            raise ValueError("PDF page Source name must equal its identity")  # noqa: TRY003
        if self.source_id != pdf_page_source_id(self.document_id, self.document_sha256, self.page_number):
            raise ValueError("PDF page Source identity does not match its document revision and page")  # noqa: TRY003
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF or fail without returning partial pages."""

    document_id = _require_document_id(value.document_id)
    content = bytes(value.content)
    if not content:
        raise ValueError("PDF document content is empty")  # noqa: TRY003
    document_sha256 = hashlib.sha256(content).hexdigest()

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        _require_readable(reader)
        pages = tuple(
            PDFPageInput(
                document_id=document_id,
                document_sha256=document_sha256,
                page_number=number,
                text=_page_text(reader.pages[number - 1]),
            )
            for number in range(1, len(reader.pages) + 1)
        )
    except ValueError:
        raise
    except Exception as error:  # pypdf raises a broad family of parse errors
        raise ValueError(f"PDF document cannot be parsed: {error}") from error  # noqa: TRY003
    if not pages:
        raise ValueError("PDF document declares no pages")  # noqa: TRY003
    return pages


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int, /) -> str:
    """Return the delimiter-safe, bounded identity of one parsed page."""

    document_id = _require_document_id(document_id)
    if not isinstance(document_sha256, str) or not _SHA256_HEX.match(document_sha256):
        raise ValueError("document_sha256 must be a lowercase hexadecimal SHA-256 digest")  # noqa: TRY003
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    source_id = f"{_IDENTITY_PREFIX}:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"PDF page Source identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class PDFPageSourceAdapter:
    """Resolve and read the page-level PDF Source."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one page as standard textual evidence prefixed by its document identity."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory: object, question: str, limit: int = 10) -> tuple:
    """Return up to ``limit`` exact page citations for one natural-language question."""

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return ()
    sequence = _question_sequence(question)
    if not sequence:
        return ()
    probes = await _probe_pages(memory, sequence)
    return _witness_citations(probes, limit)


def _hit_key(hit: object) -> tuple[str, str, str, str]:
    reference = hit.memory_ref
    return (reference.family, reference.artifact_id, hit.entry_id, hit.entry_version_id)


async def _probe_pages(memory: object, sequence: tuple[str, ...]) -> list[tuple[float, list[object]]]:
    """Probe single terms and adjacent question phrases, weighted by corpus rarity.

    A single long query is admitted only when a page covers a share of every term, which
    buries the page that holds the one distinctive term. Probing term by term keeps those
    pages reachable, while adjacent two-word probes preserve the question's own phrasing so
    a page that carries the whole phrase outranks pages that merely repeat one word.
    """

    queries: list[tuple[str, float]] = [(term, 1.0) for term in _probe_terms(tuple(dict.fromkeys(sequence)))]
    queries.extend((phrase, _PHRASE_BOOST) for phrase in _adjacent_phrases(sequence))

    probes: list[tuple[float, list[object]]] = []
    seen: set[str] = set()
    for query, boost in queries:
        if query in seen:
            continue
        seen.add(query)
        hits = await _search_hits(memory, query)
        if not hits:
            continue
        probes.append((_probe_weight(len(hits)) * boost, hits))
    return probes


async def _search_hits(memory: object, query: str) -> list[object]:
    """Run one lexical probe, treating a rejected rewrite as an empty candidate list."""

    from powercontext.builtin.runtime import SearchMemoryRequest

    try:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=_CANDIDATE_LIMIT))
    except Exception:  # a rejected probe must not lose the other candidates
        return []
    return list(page.hits)


def _probe_weight(document_frequency: int) -> float:
    """Weight one probe by how narrowly it selects pages in this corpus."""

    return math.log(1.0 + _MIN_ASSUMED_CORPUS / (1.0 + document_frequency))


def _witness_citations(probes: list[tuple[float, list[object]]], limit: int) -> tuple:
    """Rank pages by their strongest single-probe evidence position."""

    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    best: dict[tuple[str, str, str, str], tuple[float, object]] = {}
    support: dict[tuple[str, str, str, str], float] = {}
    for weight, hits in probes:
        for rank, hit in enumerate(hits, start=1):
            value = weight / rank
            key = _hit_key(hit)
            support[key] = support.get(key, 0.0) + value
            current = best.get(key)
            if current is None or value > current[0]:
                best[key] = (value, hit)
    ordered = sorted(best, key=lambda key: (best[key][0], support[key]), reverse=True)
    citations = [
        MemoryCitation(
            memory_ref=best[key][1].memory_ref,
            entry_id=best[key][1].entry_id,
            entry_version_id=best[key][1].entry_version_id,
        )
        for key in ordered[:limit]
    ]
    return tuple(citations)


def _question_sequence(question: object) -> tuple[str, ...]:
    """Return question terms in appearance order with boilerplate removed."""

    if not isinstance(question, str) or not question.strip():
        return ()
    sequence: list[str] = []
    for token in _TOKEN.findall(question.casefold()):
        if token in _STOP_WORDS or token in _PROMPT_NOISE:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        sequence.append(token)
    return tuple(sequence)


def _adjacent_phrases(sequence: tuple[str, ...], /) -> tuple[str, ...]:
    """Return adjacent term pairs, which preserve the question's own phrasing."""

    phrases: list[str] = []
    for left, right in pairwise(sequence):
        if left == right:
            continue
        phrase = f"{left} {right}"
        if phrase not in phrases:
            phrases.append(phrase)
    return tuple(phrases[:_MAX_PHRASE_TERMS])


def _probe_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    """Order probe terms so distinctive content words are probed before boilerplate."""

    specific = tuple(term for term in terms if term not in _GENERIC_FINANCE)
    ordered = tuple(sorted(specific, key=lambda term: (term.isdigit(), -len(term))))
    remainder = tuple(term for term in terms if term not in set(ordered))
    return (ordered + remainder)[:_MAX_PROBE_TERMS]


def _require_readable(reader: object) -> None:
    """Reject PDFs that still need a user password after the empty-password attempt."""

    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF document requires a user password")  # noqa: TRY003


def _page_text(page: object) -> str:
    try:
        text = page.extract_text()
    except Exception as error:
        raise ValueError(f"PDF page text cannot be extracted: {error}") from error  # noqa: TRY003
    return text.strip() if isinstance(text, str) else ""


def _require_document_id(document_id: str, /) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    return document_id


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]

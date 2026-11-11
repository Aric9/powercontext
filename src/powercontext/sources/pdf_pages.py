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
import unicodedata
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
_MAX_PHRASE_TERMS = 10
_MAX_VARIANT_TERMS = 8
_VARIANT_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("ies", 3, ("y", "ic", "ical")),
    ("ions", 4, ("e", "ed", "ing")),
    ("ion", 3, ("", "s", "e", "ed", "ing")),
    ("ing", 3, ("", "e", "ion", "ions")),
    ("ed", 2, ("", "e", "ion")),
    ("es", 1, ("",)),
    ("s", 1, ("",)),
    ("ity", 3, ("", "e")),
    ("ic", 2, ("al", "y")),
    ("ly", 2, ("",)),
    ("e", 0, ("d", "s")),
)
# Filing vocabulary that answers a question without reusing its wording: a question about
# "legal battles" is answered by a "Legal Proceedings" note, and "customers" by a
# customers/clients section. Each entry adds at most two extra probes.
_CONCEPT_TERMS: dict[str, tuple[str, ...]] = {
    "legal": ("litigation", "proceedings"),
    "litigation": ("legal", "proceedings"),
    "lawsuit": ("litigation", "proceedings"),
    "lawsuits": ("litigation", "proceedings"),
    "battles": ("litigation", "proceedings"),
    "claims": ("litigation",),
    "customers": ("clients", "customer"),
    "customer": ("clients", "customers"),
    "clients": ("customers",),
    "employees": ("workforce", "personnel"),
    "workforce": ("employees",),
    "debt": ("borrowings", "indebtedness"),
    "borrowings": ("debt",),
    "revenue": ("revenues", "sales"),
    "revenues": ("revenue", "sales"),
    "sales": ("revenue", "revenues"),
    "income": ("earnings", "profit"),
    "profit": ("income", "earnings"),
    "capex": ("capital expenditures",),
    "capital": ("capital expenditures",),
    "suppliers": ("supplier", "vendors"),
    "competition": ("competitors", "competitive"),
    "pension": ("retirement", "benefit plans"),
    "goodwill": ("impairment",),
    "leases": ("lease",),
    "tax": ("income taxes",),
}
_MAX_CONCEPT_TERMS = 3
# Layout and filing boilerplate shared by nearly every page; useless for feedback.
_DEFAULT_WEIGHTS = {
    "best": 0.433,
    "agreement": 2.211,
    "votes": 1.202,
    "numeric": 2.786,
    "dollar": 0.348,
    "statement": 0.457,
    "opening": 1.044,
    "coverage": 0.191,
    "concept_occurrences": 1.532,
    "concept_distinct": 0.624,
}
# A document-scoped pool ranks pages of one report against each other, so the
# witness and agreement signals carry more weight than in a single-document scope.
_SCOPED_WEIGHTS = {
    "best": 1.745,
    "agreement": 2.666,
    "votes": 0.829,
    "numeric": 2.643,
    "dollar": 1.029,
    "statement": 0.126,
    "opening": 0.642,
    "coverage": 2.848,
    "concept_occurrences": 1.454,
    "concept_distinct": 2.509,
}

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
    # One cheap preview tells us whether this Memory holds several documents. When it
    # does, probes are prefixed with the document identity the question names so a
    # larger report cannot crowd out the pages of the report being asked about.
    preview = await _search_hits(memory, question)
    scope = _question_document(preview, question)
    probes = await _probe_pages(memory, sequence, question, scope=scope)
    if not probes:
        return ()
    weights = _DEFAULT_WEIGHTS if scope is None else _SCOPED_WEIGHTS
    return _rank_pages(probes, _probe_terms(tuple(dict.fromkeys(sequence))), question, limit, weights)


def _hit_key(hit: object) -> tuple[str, str, str, str]:
    reference = hit.memory_ref
    return (reference.family, reference.artifact_id, hit.entry_id, hit.entry_version_id)


def _document_identity(hit: object) -> str:
    """Return the document identity a projected page carries on its first line."""

    raw = getattr(hit, "text", "")
    if not isinstance(raw, str) or "\n" not in raw:
        return ""
    first_line = raw.split("\n", 1)[0].strip()
    return first_line.split("_", 1)[0].strip().lower()


def _question_document(preview: list[object], question: str) -> str | None:
    """Pick the document identity the question names, if the Memory holds several."""

    identities = {identity for hit in preview if (identity := _document_identity(hit))}
    if len(identities) < 2:
        return None
    compact = "".join(question.lower().split())
    matches = [identity for identity in identities if identity and identity in compact]
    if not matches:
        return None
    return max(matches, key=len)


async def _probe_pages(
    memory: object,
    sequence: tuple[str, ...],
    question: str,
    *,
    scope: str | None = None,
) -> list[list[object]]:
    """Probe the question through several independent lexical views.

    One long query is admitted only when a page covers a share of every term, which buries
    the page that holds the one distinctive term. Term probes keep those pages reachable,
    adjacent phrases preserve the question's own wording, word-form variants bridge the
    inflection gap between question and report, and the whole question still contributes a
    plain relevance view.
    """

    terms = _probe_terms(tuple(dict.fromkeys(sequence)))
    queries: list[str] = list(terms)
    queries.extend(_adjacent_phrases(sequence))
    for term in terms[:_MAX_VARIANT_TERMS]:
        queries.extend(_variant_terms(term))
    expanded = 0
    for term in terms:
        related = _CONCEPT_TERMS.get(term)
        if not related:
            continue
        queries.extend(related)
        expanded += 1
        if expanded >= _MAX_CONCEPT_TERMS:
            break
    queries.append(question)

    probes: list[list[object]] = []
    seen: set[str] = set()
    for query in queries:
        if not query or query in seen:
            continue
        seen.add(query)
        effective = query if scope is None else f"{scope} {query}"
        hits = await _search_hits(memory, effective)
        if scope is not None:
            hits = [hit for hit in hits if _document_identity(hit) == scope]
        if hits:
            probes.append(hits)
    return probes


async def _search_hits(memory: object, query: str, limit: int = _CANDIDATE_LIMIT) -> list[object]:
    """Run one lexical probe, treating a rejected rewrite as an empty candidate list."""

    from powercontext.builtin.runtime import SearchMemoryRequest

    try:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=limit))
    except Exception:  # a rejected probe must not lose the other candidates
        return []
    return list(page.hits)


def _rank_pages(
    probes: list[list[object]],
    terms: tuple[str, ...],
    question: str,
    limit: int,
    weights: dict[str, float],
) -> tuple:
    """Rank whole pages by multi-probe agreement and page form.

    A page is scored from how well any single probe ranks it, how much agreement the
    probes show, how many distinct probes reach it, how dense its figures are, and how
    much of the question its opening lines and body carry. Financial-statement pages and
    narrative pages compete with the same score, because a report can answer a question
    from either one.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    harmonic: dict[tuple[str, str, str, str], float] = {}
    votes: dict[tuple[str, str, str, str], int] = {}
    best: dict[tuple[str, str, str, str], int] = {}
    hits: dict[tuple[str, str, str, str], object] = {}
    for probe in probes:
        for rank, hit in enumerate(probe, start=1):
            key = _hit_key(hit)
            hits.setdefault(key, hit)
            harmonic[key] = harmonic.get(key, 0.0) + 1.0 / rank
            votes[key] = votes.get(key, 0) + 1
            if rank < best.get(key, 10**6):
                best[key] = rank

    strongest = max(harmonic.values()) or 1.0
    term_set = frozenset(terms)
    concept_terms = _concept_terms(terms)
    scored: list[tuple[float, tuple[str, str, str, str]]] = []
    for key, value in harmonic.items():
        # The projection prefixes the document identity; page features are measured on
        # the page body alone so the opening lines reflect the page, not the file name.
        raw = hits[key].text
        body = raw.split("\n", 1)[1] if "\n" in raw else raw
        hit_text = body.casefold()
        tokens = _TOKEN.findall(hit_text)
        total = max(len(tokens), 1)
        digits = sum(1 for token in tokens if token.isdigit())
        token_set = set(tokens)
        opening = " ".join(hit_text.split()[:18])
        concept_occurrences = sum(hit_text.count(term) for term in concept_terms)
        concept_distinct = sum(1 for term in concept_terms if term in hit_text)
        score = (
            weights["best"] * (1.0 / best[key])
            + weights["agreement"] * (value / strongest)
            + weights["votes"] * min(votes[key] / 12.0, 1.0)
            + weights["numeric"] * (digits / total)
            + weights["dollar"] * min(hit_text.count("$") / 10.0, 1.0)
            + weights["statement"] * (1.0 if "consolidated statement" in hit_text else 0.0)
            + weights["opening"] * (sum(1 for term in term_set if term in opening) / max(len(term_set), 1))
            + weights["coverage"] * (len(term_set & token_set) / max(len(term_set), 1))
            + weights["concept_occurrences"] * (math.log1p(concept_occurrences) / 4.0)
            + weights["concept_distinct"]
            * (concept_distinct / max(len(concept_terms), 1))
        )
        scored.append((score, key))
    scored.sort(reverse=True)
    return tuple(
        MemoryCitation(
            memory_ref=hits[key].memory_ref,
            entry_id=hits[key].entry_id,
            entry_version_id=hits[key].entry_version_id,
        )
        for _score, key in scored[:limit]
    )


def _variant_terms(term: str) -> tuple[str, ...]:
    """Return plausible inflected or derived forms of one question term.

    The index matches whole tokens, so a question asking about "geographies" cannot reach
    a report that says "geographic". These rewrites are generic English morphology, never
    document-specific vocabulary.
    """

    if len(term) < 5 or term.isdigit() or not term.isalpha() or not term.isascii():
        return ()
    forms: list[str] = [term + "s", term + "ed", term + "ing", term + "ion"]
    for suffix, stem_length, endings in _VARIANT_RULES:
        if not term.endswith(suffix) or len(term) - stem_length < 3:
            continue
        stem = term[: len(term) - stem_length] if stem_length else term
        forms = [stem + ending for ending in endings]
        break
    ordered = [form for form in dict.fromkeys(forms) if form != term and _plausible_form(form)]
    return tuple(ordered[:4])


def _plausible_form(form: str) -> bool:
    if len(form) < 4 or not form.isalpha() or not form.isascii():
        return False
    if any(marker in form for marker in ("sss", "iys", "yy", "iing", "eed", "ionion", "ioned", "ioning")):
        return False
    return form.endswith((
        "s",
        "es",
        "d",
        "ed",
        "ing",
        "ion",
        "ions",
        "er",
        "ers",
        "ic",
        "ical",
        "al",
        "y",
        "ly",
        "ity",
        "ive",
        "e",
        "t",
        "ce",
        "de",
    ))


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


def _concept_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    """Related filing terms already probed for this question."""

    concepts: list[str] = []
    expanded = 0
    for term in terms:
        related = _CONCEPT_TERMS.get(term)
        if not related:
            continue
        for item in related:
            if item not in concepts:
                concepts.append(item)
        expanded += 1
        if expanded >= _MAX_CONCEPT_TERMS:
            break
    return tuple(concepts)

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
    if not isinstance(text, str):
        return ""
    # The Memory layer stores NFC-normalized text; normalize here as well so an exact
    # text comparison against a stored entry cannot differ on accented documents.
    return unicodedata.normalize("NFC", text).strip()


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

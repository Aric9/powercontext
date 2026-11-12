"""Data structures shared by extraction, storage and context assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """One retrievable unit of engineering material."""

    id: str
    kind: str  # spec | code | test
    title: str
    text: str
    source: str
    start_line: int
    end_line: int
    clause_id: str | None = None
    symbol: str | None = None
    signature: str | None = None
    spec_refs: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def locator(self) -> str:
        return f"{self.source}#L{self.start_line}-L{self.end_line}"

    def metadata(self) -> dict[str, str | int | float | bool]:
        """Flatten to seekdb-compatible scalar metadata."""

        meta: dict[str, str | int | float | bool] = {
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "locator": self.locator,
            "start_line": int(self.start_line),
            "end_line": int(self.end_line),
            "spec_refs": ",".join(self.spec_refs),
        }
        if self.clause_id:
            meta["clause_id"] = self.clause_id
        if self.symbol:
            meta["symbol"] = self.symbol
        if self.signature:
            meta["signature"] = self.signature[:500]
        for key, value in self.extra.items():
            if isinstance(value, (str, int, float, bool)):
                meta[key] = value
        return meta


@dataclass
class SourceItem:
    """A cited source in a context pack."""

    kind: str
    title: str
    source: str
    locator: str
    text: str
    start_line: int = 0
    end_line: int = 0
    score: float = 0.0
    clause_id: str | None = None
    symbol: str | None = None
    signature: str | None = None
    matched_by: list[str] = field(default_factory=list)
    verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "locator": self.locator,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
            "score": round(self.score, 6),
            "matched_by": self.matched_by,
            "verified": self.verified,
        }
        if self.clause_id:
            data["clause_id"] = self.clause_id
        if self.symbol:
            data["symbol"] = self.symbol
        if self.signature:
            data["signature"] = self.signature
        return data

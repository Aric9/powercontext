"""Extract atomic, citable clauses from markdown specifications."""

from __future__ import annotations

import re
from pathlib import Path

from sdd_context.models import Chunk

_CLAUSE_RE = re.compile(r"^##\s+(?P<id>[A-Z][A-Z0-9]*-\d+)\s+(?P<title>.+?)\s*$")
_VERSION_RE = re.compile(r"版本[：:]\s*(?P<version>[0-9][0-9A-Za-z.\-]*)")
_SPEC_REF_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")


def _norm(lines: list[str]) -> str:
    text = "\n".join(line.rstrip() for line in lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def parse_spec_file(path: Path, root: Path) -> list[Chunk]:
    """Parse one markdown spec into an overview chunk plus one chunk per clause."""

    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
    version_match = _VERSION_RE.search(raw)
    version = version_match.group("version") if version_match else "unversioned"

    clauses: list[Chunk] = []
    starts = [index for index, line in enumerate(lines) if _CLAUSE_RE.match(line)]

    overview_end = starts[0] if starts else len(lines)
    overview = _norm(lines[:overview_end])
    if overview:
        title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), path.stem)
        clauses.append(
            Chunk(
                id=f"spec:{rel}:overview",
                kind="spec",
                title=title,
                text=overview,
                source=rel,
                start_line=1,
                end_line=max(1, overview_end),
                extra={"spec_version": version, "scope": "overview"},
            )
        )

    for index, start in enumerate(starts):
        match = _CLAUSE_RE.match(lines[start])
        assert match is not None
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        body = _norm(lines[start + 1 : end])
        clause_id = match.group("id")
        title = match.group("title").strip()
        text = f"{clause_id} {title}\n{body}".strip()
        clauses.append(
            Chunk(
                id=f"spec:{clause_id}",
                kind="spec",
                title=title,
                text=text,
                source=rel,
                start_line=start + 1,
                end_line=end,
                clause_id=clause_id,
                spec_refs=sorted(set(_SPEC_REF_RE.findall(text)) | {clause_id}),
                extra={"spec_version": version, "scope": "clause"},
            )
        )
    return clauses


def parse_specs(specs_dir: Path, root: Path) -> list[Chunk]:
    """Parse every markdown file in ``specs_dir``."""

    chunks: list[Chunk] = []
    for path in sorted(specs_dir.rglob("*.md")):
        chunks.extend(parse_spec_file(path, root))
    return chunks

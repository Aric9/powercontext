"""Deterministic local embedding used for seekdb vector search.

The embedder is a signed feature-hashing model over word and CJK n-gram tokens.
It is intentionally dependency-free and reproducible: the same text always maps
to the same unit vector on every machine, which keeps the verification samples
offline and byte-stable.  It is a lexical/statistical embedding rather than a
neural one; see ``docs/ONE_PAGE.md`` for the trade-off.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

_WORD_RE = re.compile(r"[a-z][a-z0-9_]*|\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def split_identifier(name: str) -> list[str]:
    """Split ``RefundService.refund_amount`` into searchable sub-tokens."""

    parts: list[str] = []
    for piece in re.split(r"[^A-Za-z0-9]+", name):
        if not piece:
            continue
        parts.append(piece.lower())
        # camelCase / PascalCase boundaries
        for sub in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", piece):
            parts.append(sub.lower())
    return parts


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English technical text."""

    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    tokens.extend(re.findall(r"[a-z]{2,}-[a-z]{2,}", lowered))
    for ident in _IDENT_RE.findall(text):
        if "_" in ident or any(ch.isupper() for ch in ident[1:]):
            tokens.extend(split_identifier(ident))
    chars = _CJK_RE.findall(lowered)
    tokens.extend(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


def _bucket(token: str, dimension: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dimension
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class HashingEmbeddingFunction:
    """Signed feature hashing with sublinear term frequency and L2 norm."""

    def __init__(self, dimension: int = 512) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = int(dimension)

    @staticmethod
    def name() -> str:
        return "sdd-hash-embedding"

    def get_config(self) -> dict[str, int]:
        return {"dimension": self.dimension}

    @staticmethod
    def build_from_config(config: dict[str, int]) -> "HashingEmbeddingFunction":
        return HashingEmbeddingFunction(int(config.get("dimension", 512)))

    def embed(self, text: str) -> list[float]:
        dimension = self.dimension
        vector = [0.0] * dimension
        counts = Counter(tokenize(text))
        for token, count in counts.items():
            index, sign = _bucket(token, dimension)
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def __call__(self, input):  # noqa: A002 - pyseekdb protocol argument name
        if isinstance(input, str):
            return [self.embed(input)]
        return [self.embed(str(item)) for item in input]

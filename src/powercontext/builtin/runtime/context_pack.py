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

"""Incremental Context Pack wire format.

A full Pack is an ordered list of UTF-8 chunks plus a digest over that list. A Delta
names a prefix of the receiver's base Pack and carries only the target suffix, so a
client that already holds the previous round can rebuild this round without receiving
the shared bytes again. Both objects serialize to canonical JSON with Base64 chunks;
the receiver validates schema, digest, and byte count before trusting a payload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512
PACK_FIELDS = frozenset(("schema", "items", "digest", "byte_count"))
DELTA_FIELDS = frozenset(("schema", "base_digest", "target_digest", "prefix_items", "appended"))
_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LENGTH = 64

_MESSAGES: dict[str, str] = {
    "items": "pack items must be a tuple of bytes chunks",
    "chunk-bytes": "chunk_bytes must be a positive integer that fits every code point",
    "content": "content must be a string",
    "json": "payload is not valid UTF-8 JSON",
    "object": "payload must be a JSON object",
    "pack-fields": "pack payload must contain exactly schema, items, digest, and byte_count",
    "delta-fields": "delta payload must contain exactly schema, base_digest, target_digest, prefix_items, and appended",
    "schema": "payload schema does not match this version",
    "base64": "chunk is not valid Base64",
    "digest": "digest is malformed",
    "digest-mismatch": "payload digest does not match its chunks",
    "byte-count": "byte_count does not match the serialized payload length",
    "base-digest": "delta base digest does not match the supplied pack",
    "prefix": "delta prefix must be an integer between zero and the base item count",
    "target-digest": "delta does not rebuild the target pack",
    "pack": "value must be a ContextPack",
    "delta": "value must be a ContextPackDelta",
}


class ContextPackError(ValueError):
    """Report a malformed, tampered, or inconsistent Context Pack payload."""

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES.get(code, code))
        self.code = code


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete Context body as ordered, UTF-8-safe chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        """Build a full pack from in-memory chunk bytes."""

        values = _require_items(items)
        digest = _pack_digest(values)
        byte_count = 0
        while True:
            wire = _pack_bytes(values, digest, byte_count)
            if len(wire) == byte_count:
                return cls(values, digest, byte_count)
            byte_count = len(wire)

    @classmethod
    def from_content(cls, content: str, /, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split text into deterministic chunks no larger than ``chunk_bytes``."""

        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ContextPackError("chunk-bytes")
        if not isinstance(content, str):
            raise ContextPackError("content")
        chunks: list[bytes] = []
        buffer = bytearray()
        for character in content:
            encoded = character.encode("utf-8")
            if len(encoded) > chunk_bytes:
                raise ContextPackError("chunk-bytes")
            if len(buffer) + len(encoded) > chunk_bytes:
                chunks.append(bytes(buffer))
                buffer.clear()
            buffer.extend(encoded)
        if buffer:
            chunks.append(bytes(buffer))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        """Serialize this pack into its canonical wire payload."""

        return _pack_bytes(self.items, self.digest, self.byte_count)

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        """Decode one wire pack after checking schema, digest, and byte count."""

        payload = _load_payload(data)
        if frozenset(payload) != PACK_FIELDS:
            raise ContextPackError("pack-fields")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("schema")
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        if not _valid_digest(digest):
            raise ContextPackError("digest")
        if _pack_digest(items) != digest:
            raise ContextPackError("digest-mismatch")
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ContextPackError("byte-count")
        if byte_count != len(data):
            raise ContextPackError("byte-count")
        return cls(items, digest, byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A shared prefix of the base pack plus the target pack's remaining chunks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Return the longest shared prefix plus the target's suffix."""

        if not isinstance(base, ContextPack):
            raise ContextPackError("pack")
        if not isinstance(target, ContextPack):
            raise ContextPackError("pack")
        prefix = 0
        limit = min(len(base.items), len(target.items))
        while prefix < limit and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        """Return the serialized delta size; it is not a wire field."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize this delta into its canonical wire payload."""

        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        """Decode one wire delta after checking schema, digests, and prefix shape."""

        payload = _load_payload(data)
        if frozenset(payload) != DELTA_FIELDS:
            raise ContextPackError("delta-fields")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("schema")
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not _valid_digest(base_digest) or not _valid_digest(target_digest):
            raise ContextPackError("digest")
        prefix = payload["prefix_items"]
        if isinstance(prefix, bool) or not isinstance(prefix, int) or prefix < 0:
            raise ContextPackError("prefix")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from a base pack and one validated delta."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("pack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta")
    if base.digest != delta.base_digest:
        raise ContextPackError("base-digest")
    if delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("prefix")
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("target-digest")
    return target


def _require_items(items: tuple[bytes, ...]) -> tuple[bytes, ...]:
    if not isinstance(items, tuple):
        raise ContextPackError("items")
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("items")
    return items


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: Any) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("items")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("base64")
        try:
            decoded.append(base64.b64decode(value.encode("ascii"), validate=True))
        except (UnicodeEncodeError, binascii.Error) as error:
            raise ContextPackError("base64") from error
    return tuple(decoded)


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(items)}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _pack_bytes(items: tuple[bytes, ...], digest: str, byte_count: int) -> bytes:
    return _canonical(
        {
            "schema": PACK_SCHEMA,
            "items": _encode_items(items),
            "digest": digest,
            "byte_count": byte_count,
        }
    )


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_payload(data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise ContextPackError("json")
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("json") from error
    if not isinstance(payload, dict):
        raise ContextPackError("object")
    return payload


def _valid_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        return False
    hex_part = value[len(_DIGEST_PREFIX) :]
    return len(hex_part) == _DIGEST_HEX_LENGTH and all(character in "0123456789abcdef" for character in hex_part)


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

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

"""Source lifecycle graph for withdrawing and restoring dependent evidence.

The caller registers a small DAG per scope: Sources at the roots, Memory entries
derived from Sources or other Memory entries, and Topic Memories derived from any
registered node. One availability event per Source then propagates through every
descendant with the node's ``all`` or ``any`` policy. Memory entries are switched
through the ordinary Memory service so search and Context already reflect the state;
Topic Memories keep their Artifact untouched and are hidden from search at read time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.sources import SourceRef

NodeKind = Literal["source", "memory", "topic"]
POLICIES: tuple[str, ...] = ("all", "any")
PURPOSES: tuple[str, ...] = ("generation", "audit")

MemoryStateApplier = Callable[[MemoryCitation, bool], Awaitable[None]]
ReferenceVerifier = Callable[[Any], Awaitable[None]]

_MESSAGES: dict[str, str] = {
    "policy": "policy must be all or any",
    "node-id": "node_id must be a non-blank string",
    "parents-sequence": "parents must be a sequence of registered node ids",
    "parents-empty": "a derived node requires at least one parent",
    "parents-repeat": "parents must not repeat",
    "parents-id": "parent ids must be non-blank strings",
    "parents-self": "a node cannot depend on itself",
    "conflict": "node id is already bound to a different registration",
    "missing-parent": "parent node is not registered in this scope",
    "revision-positive": "source revision must be a positive integer",
    "live-event": "a live SourceLifecycleEvent is required",
    "unknown-source": "event node is not a registered Source",
    "revision-mismatch": "event revision does not match the registered Source revision",
    "stale-sequence": "event sequence is older than the last applied event",
    "sequence-conflict": "event sequence was reused with different content",
    "purpose": "purpose must be generation or audit",
    "unknown-node": "node is not registered in this scope",
    "unavailable": "node is unavailable for generation",
    "source-reference": "source reference is not readable in this scope",
    "topic-reference": "topic reference is not readable in this scope",
    "memory-reference": "memory citation is not readable in this scope",
    "memory-missing": "memory artifact is missing in this scope",
    "memory-manifest": "memory citation is not part of the current manifest",
}


class SourceLifecycleError(ValueError):
    """Report an invalid lifecycle graph, event, or reference."""

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES.get(code, code))
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One availability change for a registered Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int = Field(ge=1, strict=True)
    sequence: int = Field(ge=1, strict=True)
    available: bool = Field(strict=True)
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_non_blank_text(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("lifecycle text must be a non-blank string")  # noqa: TRY003
        return value


@dataclass(frozen=True, slots=True)
class LifecycleNode:
    """One registered graph node with its persisted availability."""

    node_id: str
    kind: NodeKind
    revision: int
    order: int
    binding: dict[str, Any]
    parents: tuple[str, ...]
    policy: str
    available: bool
    last_sequence: int | None
    last_digest: str | None

    def reference(self) -> SourceRef | MemoryCitation | ArtifactRef:
        if self.kind == "source":
            return SourceRef(source_type=self.binding["source_type"], source_id=self.binding["source_id"])
        if self.kind == "memory":
            return MemoryCitation.model_validate(self.binding)
        return ArtifactRef(
            family=self.binding["family"],
            artifact_id=self.binding["artifact_id"],
            revision=self.binding["revision"],
        )


class SourceLifecycleStore:
    """Persist the registered graph and each node's current availability."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def load(self, scope_id: str, /) -> tuple[LifecycleNode, ...]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(table).where(table.c.scope_id == scope_id).order_by(table.c.registration_order)
                )
            ).mappings().all()
        return tuple(_node_from_row(row) for row in rows)

    async def insert(self, scope_id: str, node: LifecycleNode, /) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            await connection.execute(
                insert(table).values(
                    scope_id=scope_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    lifecycle_revision=node.revision,
                    registration_order=node.order,
                    binding=json.dumps(node.binding, sort_keys=True),
                    parents=json.dumps(list(node.parents)),
                    policy=node.policy,
                    available=node.available,
                )
            )

    async def save(
        self,
        scope_id: str,
        states: Mapping[str, bool],
        /,
        *,
        source_node_id: str,
        sequence: int,
        digest: str,
    ) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            for node_id, available in states.items():
                await connection.execute(
                    update(table)
                    .where(table.c.scope_id == scope_id, table.c.node_id == node_id)
                    .values(available=available)
                )
            await connection.execute(
                update(table)
                .where(table.c.scope_id == scope_id, table.c.node_id == source_node_id)
                .values(last_sequence=sequence, last_digest=digest)
            )

    async def inactive_topic_refs(self, scope_id: str, /) -> frozenset[tuple[str, int]]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(table.c.binding).where(
                        table.c.scope_id == scope_id,
                        table.c.kind == "topic",
                        table.c.available.is_(False),
                    )
                )
            ).scalars().all()
        refs: set[tuple[str, int]] = set()
        for row in rows:
            payload = json.loads(row)
            refs.add((payload["artifact_id"], int(payload["revision"])))
        return frozenset(refs)


class SourceLifecycleService:
    """Register one scope's evidence DAG and propagate Source availability."""

    def __init__(
        self,
        *,
        store: SourceLifecycleStore,
        scope_id: str,
        memory_state: MemoryStateApplier,
        verify_source: ReferenceVerifier,
        verify_memory: ReferenceVerifier,
        verify_topic: ReferenceVerifier,
    ) -> None:
        self._store = store
        self._scope_id = scope_id
        self._memory_state = memory_state
        self._verifiers: dict[str, ReferenceVerifier] = {
            "source": verify_source,
            "memory": verify_memory,
            "topic": verify_topic,
        }

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Register a root Source node at one lifecycle revision."""

        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise SourceLifecycleError("revision-positive")
        binding = {"source_type": source_ref.source_type, "source_id": source_ref.source_id}
        await self._register(
            node_id,
            kind="source",
            revision=revision,
            binding=binding,
            reference=source_ref,
            parents=(),
            policy="all",
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str] = (),
        policy: str = "all",
    ) -> None:
        """Register a Memory node derived from already registered parents."""

        await self._register(
            node_id,
            kind="memory",
            revision=1,
            binding=citation.model_dump(mode="json"),
            reference=citation,
            parents=parents,
            policy=policy,
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str] = (),
        policy: str = "all",
    ) -> None:
        """Register a Topic Memory node derived from already registered parents."""

        await self._register(
            node_id,
            kind="topic",
            revision=1,
            binding={
                "family": artifact_ref.family,
                "artifact_id": artifact_ref.artifact_id,
                "revision": artifact_ref.revision,
            },
            reference=artifact_ref,
            parents=parents,
            policy=policy,
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Propagate one Source event and return every currently available node id."""

        nodes = {node.node_id: node for node in await self._store.load(self._scope_id)}
        source, digest, replay = _validated_event(nodes, event)
        if replay:
            return sorted(node_id for node_id, node in nodes.items() if node.available)
        states = _propagated_states(nodes, source, event.available)

        # Native Memory changes must succeed before the graph records the event, so a
        # failed write leaves the previous availability in place and the retry repeats
        # the same propagation instead of being skipped as a replay.
        for node in sorted(nodes.values(), key=lambda item: item.order):
            target = states[node.node_id]
            if node.kind == "memory" and target != node.available:
                await self._memory_state(_citation_of(node), target)

        await self._store.save(
            self._scope_id,
            states,
            source_node_id=source.node_id,
            sequence=event.sequence,
            digest=digest,
        )
        return sorted(node_id for node_id, value in states.items() if value)

    async def resolve(self, node_id: str, /, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return a registered reference for generation or audit use."""

        if purpose not in PURPOSES:
            raise SourceLifecycleError("purpose")
        nodes = {node.node_id: node for node in await self._store.load(self._scope_id)}
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable")
        return node.reference()

    async def inactive_topic_refs(self) -> frozenset[tuple[str, int]]:
        """Return Topic Revision identities that a lifecycle event withdrew."""

        return await self._store.inactive_topic_refs(self._scope_id)

    async def _register(
        self,
        node_id: str,
        *,
        kind: NodeKind,
        revision: int,
        binding: dict[str, Any],
        reference: Any,
        parents: Sequence[str],
        policy: str,
    ) -> None:
        node_id = _require_node_id(node_id)
        if policy not in POLICIES:
            raise SourceLifecycleError("policy")
        parent_ids = () if kind == "source" else _require_parents(parents, node_id)
        await self._verifiers[kind](reference)
        nodes = {node.node_id: node for node in await self._store.load(self._scope_id)}
        existing = nodes.get(node_id)
        if existing is not None:
            if (
                existing.kind != kind
                or existing.revision != revision
                or existing.binding != binding
                or existing.parents != parent_ids
                or existing.policy != policy
            ):
                raise SourceLifecycleError("conflict")
            return
        for parent_id in parent_ids:
            if parent_id not in nodes:
                raise SourceLifecycleError("missing-parent")
        support = [nodes[parent_id].available for parent_id in parent_ids]
        available = all(support) if policy == "all" else any(support)
        node = LifecycleNode(
            node_id=node_id,
            kind=kind,
            revision=revision,
            order=max((item.order for item in nodes.values()), default=-1) + 1,
            binding=binding,
            parents=parent_ids,
            policy=policy,
            available=available,
            last_sequence=None,
            last_digest=None,
        )
        await self._store.insert(self._scope_id, node)
        if kind == "memory" and not available:
            await self._memory_state(_citation_of(node), False)


def _validated_event(
    nodes: Mapping[str, LifecycleNode],
    event: Any,
) -> tuple[LifecycleNode, str, bool]:
    """Return the Source, its event digest, and whether this event is a replay."""

    if not isinstance(event, SourceLifecycleEvent):
        raise SourceLifecycleError("live-event")
    source = nodes.get(event.node_id)
    if source is None or source.kind != "source":
        raise SourceLifecycleError("unknown-source")
    if source.revision != event.revision:
        raise SourceLifecycleError("revision-mismatch")
    digest = _event_digest(event)
    if source.last_sequence is not None:
        if event.sequence < source.last_sequence:
            raise SourceLifecycleError("stale-sequence")
        if event.sequence == source.last_sequence:
            if source.last_digest != digest:
                raise SourceLifecycleError("sequence-conflict")
            return source, digest, True
    return source, digest, False


def _propagated_states(
    nodes: Mapping[str, LifecycleNode],
    source: LifecycleNode,
    available: bool,
) -> dict[str, bool]:
    """Recompute every node's availability from the event in registration order."""

    ordered = sorted(nodes.values(), key=lambda node: node.order)
    states = {node.node_id: node.available for node in ordered}
    states[source.node_id] = available
    for node in ordered:
        if node.kind == "source":
            continue
        support = [states[parent] for parent in node.parents]
        states[node.node_id] = all(support) if node.policy == "all" else any(support)
    return states


def _citation_of(node: LifecycleNode) -> MemoryCitation:
    return MemoryCitation.model_validate(node.binding)


def _require_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node-id")
    return node_id


def _require_parents(parents: Sequence[str], node_id: str) -> tuple[str, ...]:
    if isinstance(parents, str) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents-sequence")
    values = tuple(parents)
    if not values:
        raise SourceLifecycleError("parents-empty")
    if len(set(values)) != len(values):
        raise SourceLifecycleError("parents-repeat")
    for parent in values:
        if not isinstance(parent, str) or not parent.strip():
            raise SourceLifecycleError("parents-id")
        if parent == node_id:
            raise SourceLifecycleError("parents-self")
    return values


def _event_digest(event: SourceLifecycleEvent) -> str:
    payload = json.dumps(
        {
            "node_id": event.node_id,
            "revision": event.revision,
            "sequence": event.sequence,
            "available": event.available,
            "reason": event.reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _node_from_row(row: Mapping[str, Any]) -> LifecycleNode:
    return LifecycleNode(
        node_id=row["node_id"],
        kind=row["kind"],
        revision=int(row["lifecycle_revision"]),
        order=int(row["registration_order"]),
        binding=json.loads(row["binding"]),
        parents=tuple(json.loads(row["parents"])),
        policy=row["policy"],
        available=bool(row["available"]),
        last_sequence=None if row["last_sequence"] is None else int(row["last_sequence"]),
        last_digest=row["last_digest"],
    )


__all__ = [
    "LifecycleNode",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleService",
    "SourceLifecycleStore",
]

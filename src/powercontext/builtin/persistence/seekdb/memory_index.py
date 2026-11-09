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

"""One-statement hybrid candidate retrieval over seekdb's native indexes.

The seekdb profile already ships a full-text index on the active-head projection and an
HNSW index on the embedding projection. Reading both candidate lists in one statement
keeps scope, head and tag filtering inside the database and removes the extra application
round trip without changing what the shared fusion layer receives.
"""

from __future__ import annotations

from pyobvector import VECTOR
from sqlalchemy import Table, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.persistence.memory_index import memory_channel_hits
from powercontext.builtin.persistence.oceanbase.memory_index import (
    OceanBaseMemoryFTSIndex,
    OceanBaseMemoryVectorIndex,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

_FTS_BRANCH = """
SELECT
    h.memory_artifact_id,
    h.head_revision,
    h.entry_id,
    h.entry_version_id,
    v.text,
    CAST(NULL AS DOUBLE) AS distance,
    'fts' AS channel
FROM pc_memory_entry_heads AS h
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = h.scope_id
 AND v.memory_artifact_id = h.memory_artifact_id
 AND v.entry_version_id = h.entry_version_id
WHERE h.scope_id = :scope_id
  AND h.memory_artifact_id IN :memory_artifact_ids
  {tag}
  AND MATCH(h.searchable_text) AGAINST (:analyzed_query IN NATURAL LANGUAGE MODE)
ORDER BY MATCH(h.searchable_text) AGAINST (:analyzed_query IN NATURAL LANGUAGE MODE) DESC,
         h.memory_artifact_id,
         h.entry_id,
         h.entry_version_id
LIMIT :candidate_limit
"""

_VECTOR_BRANCH = """
SELECT
    m.memory_artifact_id,
    m.head_revision,
    m.entry_id,
    m.entry_version_id,
    v.text,
    l2_distance(m.embedding, :query_vector) AS distance,
    'vector' AS channel
FROM pc_memory_vector_entries AS m
JOIN pc_memory_entry_versions AS v
  ON v.scope_id = m.scope_id
 AND v.memory_artifact_id = m.memory_artifact_id
 AND v.entry_version_id = m.entry_version_id
WHERE m.scope_id = :scope_id
  AND m.memory_artifact_id IN :memory_artifact_ids
  {tag}
ORDER BY l2_distance(m.embedding, :query_vector) {approximate}
LIMIT :candidate_limit
"""


def _combined_statement(dimension: int, *, tagged: bool):
    """Build the single candidate statement shared by both native channels."""

    fts = _FTS_BRANCH.format(tag=memory_tag_sql("h") if tagged else "")
    vector = _VECTOR_BRANCH.format(
        tag=memory_tag_sql("m") if tagged else "",
        # An exact scan is required once a tag predicate must run before the top-k cut.
        approximate="" if tagged else "APPROXIMATE",
    )
    sql = f"({fts})\nUNION ALL\n({vector})"
    binds = [
        bindparam("memory_artifact_ids", expanding=True),
        bindparam("query_vector", type_=VECTOR(dimension)),
    ]
    if tagged:
        binds.extend((
            bindparam("tag_keys", expanding=True),
            bindparam("tag_hashes", expanding=True),
        ))
    return text(sql).bindparams(*binds)


class SeekDBMemoryHybridIndex:
    """Serve FTS, vector and hybrid candidate retrieval from one seekdb statement."""

    def __init__(
        self,
        fts: OceanBaseMemoryFTSIndex,
        vector: OceanBaseMemoryVectorIndex,
        /,
    ) -> None:
        self._fts = fts
        self._vector = vector
        self.profile = vector.profile
        self.capabilities = MemoryCapabilities(
            fts=True,
            vector=True,
            hybrid=True,
            embedding_profile=vector.profile,
            tag_filter=True,
        )
        self.tables: tuple[Table, ...] = (*fts.tables, *vector.tables)
        self._plain = _combined_statement(vector.profile.dimension, tagged=False)
        self._tagged = _combined_statement(vector.profile.dimension, tagged=True)

    async def initialize(self, connection: AsyncConnection, /) -> None:
        await self._fts.initialize(connection)
        await self._vector.initialize(connection)

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self._fts.replace(connection, scope_id, memory_ref, projections)
        await self._vector.replace(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        await self._fts.delete(connection, scope_id, memory_ref, entry_ids)
        await self._vector.delete(connection, scope_id, memory_ref, entry_ids)

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self._fts.upsert(connection, scope_id, memory_ref, projections)
        await self._vector.upsert(connection, scope_id, memory_ref, projections)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode == "fts":
            return await self._fts.search(connection, scope_id, request)
        if request.mode == "vector":
            return await self._vector.search(connection, scope_id, request)
        if request.mode != "hybrid":
            return MemorySearchChannels()
        return await self._search_hybrid(connection, scope_id, request)

    async def _search_hybrid(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        # Reuse the vector index's profile, completeness and head checks so hybrid
        # behaves exactly like the vector-only path before the candidate query runs.
        if request.embedding_profile is None or request.embedding_profile != self.profile:
            raise CapabilityNotSupportedError("embedding-profile")
        if request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self._vector.vector_complete(connection, scope_id, request.memories, self.profile):
            raise CapabilityNotSupportedError("vector")

        tagged = request.tag_filter is not None
        parameters = {
            "scope_id": scope_id,
            "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
            "analyzed_query": request.analyzed_query,
            "query_vector": request.query_vector,
            "candidate_limit": request.candidate_limit,
            **memory_tag_parameters(request.tag_filter),
        }
        rows = (await connection.execute(self._tagged if tagged else self._plain, parameters)).mappings().all()
        fts_rows = [row for row in rows if row["channel"] == "fts"]
        vector_rows = [row for row in rows if row["channel"] == "vector"]
        return MemorySearchChannels(
            fts=memory_channel_hits(fts_rows, request.memories),
            vector=memory_channel_hits(vector_rows, request.memories),
        )

    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        return await self._vector.vector_complete(connection, scope_id, memories, profile)

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        return await self._vector.hydrate(connection, scope_id, projections)


__all__ = ["SeekDBMemoryHybridIndex"]

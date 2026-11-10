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

"""HTTP cross-encoder reranking for coarse Memory search candidates.

The service scores one bounded batch per request, so the client splits the candidate pool
itself and then decides the global order from every batch's scores. Candidate ranks are
the positions in the caller's original pool and are never re-numbered per batch: the
selection that comes back is a set of original ranks, so the runtime keeps the exact
entry version identity it already resolved.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANKER_POLICY_ID = "http-cross-encoder-v1"


class _RerankScore(BaseModel):
    """One service score bound to the rank it was requested for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)

    @field_validator("rank", mode="before")
    @classmethod
    def require_integer_rank(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rerank rank must be an integer")  # noqa: TRY003, TRY004
        return value

    @field_validator("score", mode="before")
    @classmethod
    def require_numeric_score(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("rerank score must be a number")  # noqa: TRY003, TRY004
        return value


class _RerankEnvelope(BaseModel):
    """The service response: exactly one scores list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scores: tuple[_RerankScore, ...]


class HTTPMemoryReranker:
    """Score Memory candidates through one HTTP service and select globally."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANKER_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("reranker endpoint must be a non-empty URL")  # noqa: TRY003
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("reranker batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("reranker policy_id must be a non-empty string")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate in bounded batches and select the global top ranks."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("rerank query must be a non-empty string")  # noqa: TRY003
        _validate_bounds(len(candidates), limit)

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = tuple(enumerate(candidates[start : start + self._batch_size], start=start + 1))
            payload = {
                "query": query,
                "candidates": [{"rank": rank, "text": hit.text} for rank, hit in batch],
            }
            response = await self._client.post(self._endpoint, json=payload)
            requests += 1
            # A non-2xx response keeps its httpx exception type for the caller.
            response.raise_for_status()
            scores.update(_parse_scores(response, {rank for rank, _hit in batch}))

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered),
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(response: httpx.Response, expected: set[int]) -> dict[int, float]:
    """Validate one batch response and return its rank-to-score mapping."""

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("rerank response is not valid JSON") from error  # noqa: TRY003
    try:
        envelope = _RerankEnvelope.model_validate(payload)
    except ValidationError as error:
        raise ValueError("rerank response does not match the scoring schema") from error  # noqa: TRY003

    observed = [entry.rank for entry in envelope.scores]
    if len(observed) != len(set(observed)):
        raise ValueError("rerank response repeats a candidate rank")  # noqa: TRY003
    if set(observed) != expected:
        raise ValueError("rerank response must cover exactly this batch's ranks")  # noqa: TRY003
    return {entry.rank: entry.score for entry in envelope.scores}


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


__all__ = ["HTTP_RERANKER_POLICY_ID", "HTTPMemoryReranker"]

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

"""Coalesce concurrent Embedding requests and cache successful vectors.

One wrapper instance coordinates every caller in the same event loop: texts already
embedded are served from an LRU cache, texts currently being embedded are joined, and
texts captured inside one short window are deduplicated into bounded provider batches.
Each batch runs in its own task, so cancelling one caller never cancels work other
callers are waiting for. Failures are broadcast to that batch's waiters and are never
cached, which lets the next caller retry the provider.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


class _Pending:
    """One text whose vector is being produced, shared by every waiter."""

    __slots__ = ("batch_id", "future", "usage")

    def __init__(self, future: asyncio.Future[EmbeddingVector]) -> None:
        self.future = future
        self.batch_id: int | None = None
        self.usage: InferenceUsage | None = None


class CoalescingEmbeddingModel:
    """Merge concurrent Embedding calls behind one bounded, cached provider path."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1_000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, _Pending] = {}
        self._window: dict[str, _Pending] = {}
        self._window_order: list[str] = []
        self._window_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._next_batch_id = 0

    @property
    def profile(self) -> Any:
        return self._model.profile

    def __getattr__(self, name: str) -> Any:
        # Delegate optional capabilities (for example ``embed_query``) to the wrapped
        # model so callers keep seeing the provider's own feature set.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._model, name)

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        """Embed texts in original order, sharing cached and in-flight work."""

        values = tuple(texts)
        if not values:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        unique = tuple(dict.fromkeys(values))
        resolved: dict[str, EmbeddingVector] = {}
        waiters: dict[str, _Pending] = {}
        for text in unique:
            cached = self._cached(text)
            if cached is not None:
                resolved[text] = cached
                continue
            pending = self._inflight.get(text)
            if pending is None:
                pending = _Pending(asyncio.get_running_loop().create_future())
                pending.future.add_done_callback(_consume_exception)
                self._inflight[text] = pending
                self._window[text] = pending
                self._window_order.append(text)
                self._arm_window()
            waiters[text] = pending

        batches: dict[int, _Pending] = {}
        for text, pending in waiters.items():
            vector = await asyncio.shield(pending.future)
            resolved[text] = vector
            if pending.batch_id is not None:
                batches.setdefault(pending.batch_id, pending)

        usage = _sum_usage([pending.usage for pending in batches.values() if pending.usage is not None])
        return EmbeddingResult(vectors=tuple(resolved[text] for text in values), usage=usage)

    def _cached(self, text: str) -> EmbeddingVector | None:
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _arm_window(self) -> None:
        if self._window_handle is not None:
            return
        loop = asyncio.get_running_loop()
        self._window_handle = loop.call_later(self._coalesce_seconds, self._dispatch)

    def _dispatch(self) -> None:
        self._window_handle = None
        if not self._window_order:
            return
        texts = tuple(self._window_order)
        pendings = tuple(self._window[text] for text in texts)
        self._window.clear()
        self._window_order.clear()
        loop = asyncio.get_running_loop()
        for start in range(0, len(texts), self._max_batch_size):
            batch = texts[start : start + self._max_batch_size]
            members = pendings[start : start + self._max_batch_size]
            self._next_batch_id += 1
            batch_id = self._next_batch_id
            for pending in members:
                pending.batch_id = batch_id
            task = loop.create_task(self._run_batch(batch_id, batch, members))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, batch_id: int, texts: tuple[str, ...], pendings: tuple[_Pending, ...]) -> None:
        try:
            result = await self._model.embed(texts)
        except asyncio.CancelledError:
            for pending in pendings:
                if not pending.future.done():
                    pending.future.cancel()
            raise
        except BaseException as error:
            self._fail(texts, pendings, error)
            return
        vectors = tuple(result.vectors)
        if len(vectors) != len(texts):
            self._fail(texts, pendings, ValueError("embedding vector count does not match the batch"))
            return
        usage = result.usage
        for text, vector, pending in zip(texts, vectors, pendings, strict=True):
            value: EmbeddingVector = tuple(float(component) for component in vector)
            pending.usage = usage
            self._remember(text, value)
            if not pending.future.done():
                pending.future.set_result(value)
        self._forget(texts)

    def _fail(self, texts: tuple[str, ...], pendings: tuple[_Pending, ...], error: BaseException) -> None:
        for pending in pendings:
            if not pending.future.done():
                pending.future.set_exception(error)
        self._forget(texts)

    def _forget(self, texts: tuple[str, ...]) -> None:
        for text in texts:
            self._inflight.pop(text, None)

    def _remember(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _consume_exception(future: asyncio.Future[EmbeddingVector]) -> None:
    """Mark a failure as observed so an abandoned waiter cannot warn at collection."""

    if not future.cancelled():
        future.exception()


def _sum_usage(usages: list[InferenceUsage]) -> InferenceUsage:
    """Aggregate the batches one caller waited on, counting each batch once."""

    if not usages:
        return InferenceUsage(requests=0)

    def total(field: str) -> int | None:
        values = [getattr(usage, field) for usage in usages]
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    return InferenceUsage(
        requests=sum(usage.requests for usage in usages),
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
    )


__all__ = [
    "DEFAULT_CACHE_SIZE",
    "DEFAULT_COALESCE_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescingEmbeddingModel",
]

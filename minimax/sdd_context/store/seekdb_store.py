"""Persist engineering materials in seekdb and query them natively.

The collection stores one document per atomic unit (spec clause, code symbol,
test) together with flat metadata.  Dense vector search is provided by seekdb's
HNSW index over the deterministic local embedding; lexical recall uses seekdb's
full-text ``$contains`` filter.  Both paths are executed by the database, not by
an in-memory Python index.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sdd_context.config import Settings
from sdd_context.embedding import HashingEmbeddingFunction
from sdd_context.models import Chunk

COLLECTION_NAME = "sdd_chunks"
BATCH_SIZE = 64


class SeekDBStore:
    """Thin, explicit wrapper around one pyseekdb collection."""

    def __init__(self, settings: Settings, embedder: HashingEmbeddingFunction | None = None) -> None:
        self.settings = settings
        self.embedder = embedder or HashingEmbeddingFunction(settings.embedding_dimension)
        self._client: Any = None
        self._collection: Any = None
        self.query_log: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- lifecycle
    def connect(self) -> "SeekDBStore":
        if self._collection is not None:
            return self
        import pyseekdb

        self.settings.seekdb_path.mkdir(parents=True, exist_ok=True)
        admin = pyseekdb.AdminClient(path=str(self.settings.seekdb_path))
        try:
            names = {getattr(item, "name", str(item)) for item in admin.list_databases()}
            if "sdd" not in names:
                admin.create_database("sdd")
        finally:
            close = getattr(admin, "close", None)
            if callable(close):
                close()
        self._client = pyseekdb.Client(path=str(self.settings.seekdb_path), database="sdd")
        self._pyseekdb = pyseekdb
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=None,
            configuration=pyseekdb.HNSWConfiguration(
                dimension=self.settings.embedding_dimension, distance="cosine"
            ),
        )
        return self

    def reset(self) -> None:
        """Drop and recreate the collection (full re-import)."""

        self.connect()
        assert self._client is not None
        try:
            self._client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
        self._collection = self._client.create_collection(
            name=COLLECTION_NAME,
            embedding_function=None,
            configuration=self._pyseekdb.HNSWConfiguration(
                dimension=self.settings.embedding_dimension, distance="cosine"
            ),
        )

    # --------------------------------------------------------------------- write
    def add_chunks(self, chunks: list[Chunk]) -> int:
        self.connect()
        assert self._collection is not None
        total = 0
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start : start + BATCH_SIZE]
            self._collection.add(
                ids=[chunk.id for chunk in batch],
                documents=[chunk.text for chunk in batch],
                metadatas=[chunk.metadata() for chunk in batch],
                embeddings=[self.embedder.embed(chunk.text) for chunk in batch],
            )
            total += len(batch)
        return total

    # ---------------------------------------------------------------------- read
    def count(self, kind: str | None = None) -> int:
        self.connect()
        assert self._collection is not None
        if kind is None:
            return int(self._collection.count())
        result = self._collection.get(where={"kind": kind}, include=[])
        return len(result.get("ids", []))

    def dense_query(self, text: str, n_results: int = 10, kind: str | None = None) -> list[dict[str, Any]]:
        self.connect()
        assert self._collection is not None
        where = {"kind": kind} if kind else None
        started = time.perf_counter()
        result = self._collection.query(
            query_embeddings=[self.embedder.embed(text)],
            n_results=n_results,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        hits: list[dict[str, Any]] = []
        for index, item_id in enumerate(result.get("ids", [[]])[0]):
            distance = float(result.get("distances", [[0.0]])[0][index])
            hits.append(
                {
                    "id": item_id,
                    "document": result["documents"][0][index],
                    "metadata": result["metadatas"][0][index],
                    "distance": distance,
                    "score": 1.0 - distance,
                }
            )
        self.query_log.append(
            {
                "op": "dense",
                "text": text,
                "kind": kind,
                "n_results": n_results,
                "hits": [hit["id"] for hit in hits],
                "elapsed_ms": round(elapsed_ms, 2),
            }
        )
        return hits

    def contains_query(self, term: str, n_results: int = 30, kind: str | None = None) -> list[dict[str, Any]]:
        """Full-text containment search executed by seekdb."""

        self.connect()
        assert self._collection is not None
        where = {"kind": kind} if kind else None
        started = time.perf_counter()
        try:
            result = self._collection.get(
                where=where,
                where_document={"$contains": term},
                limit=n_results,
                include=["documents", "metadatas"],
            )
        except Exception:
            result = {"ids": [], "documents": [], "metadatas": []}
        elapsed_ms = (time.perf_counter() - started) * 1000
        hits: list[dict[str, Any]] = []
        for index, item_id in enumerate(result.get("ids", [])):
            hits.append(
                {
                    "id": item_id,
                    "document": result["documents"][index],
                    "metadata": result["metadatas"][index],
                    "distance": 0.0,
                    "score": 1.0 / (1.0 + index),
                }
            )
        self.query_log.append(
            {
                "op": "contains",
                "term": term,
                "kind": kind,
                "n_results": n_results,
                "hits": [hit["id"] for hit in hits],
                "elapsed_ms": round(elapsed_ms, 2),
            }
        )
        return hits

    def get_by_source(self, source: str, limit: int = 50) -> list[dict[str, Any]]:
        """All chunks belonging to one source file (used to guarantee coverage)."""

        self.connect()
        assert self._collection is not None
        result = self._collection.get(
            where={"source": source},
            limit=limit,
            include=["documents", "metadatas"],
        )
        return [
            {"id": item_id, "document": result["documents"][index], "metadata": result["metadatas"][index]}
            for index, item_id in enumerate(result.get("ids", []))
        ]

    def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        self.connect()
        assert self._collection is not None
        result = self._collection.get(ids=ids, include=["documents", "metadatas"])
        return [
            {"id": item_id, "document": result["documents"][index], "metadata": result["metadatas"][index]}
            for index, item_id in enumerate(result.get("ids", []))
        ]

    # --------------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        self.connect()
        by_kind = {kind: self.count(kind) for kind in ("spec", "code", "test")}
        return {
            "backend": "seekdb",
            "collection": COLLECTION_NAME,
            "path": str(self.settings.seekdb_path),
            "dimension": self.settings.embedding_dimension,
            "total": self.count(),
            "by_kind": by_kind,
        }

    def write_query_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.query_log, indent=2, ensure_ascii=False), encoding="utf-8")

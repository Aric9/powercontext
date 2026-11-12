"""seekdb 存储层：库/集合初始化、幂等导入、统计与原始检索接口。"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyseekdb

from readdesk.config import Settings
from readdesk.embedding import embed
from readdesk.parsing import ParsedDocument, parse_document
from readdesk.util import normalize_tag, tag_field


class StoreError(Exception):
    """存储层通用错误（中文消息）。"""


class NotInitializedError(StoreError):
    """数据目录尚未初始化。"""


class EmptyLibraryError(StoreError):
    """库中没有任何文档。"""


@dataclass
class ImportOutcome:
    """单个文件的导入结果。"""

    rel_path: str
    doc_id: str
    n_chunks: int
    status: str  # imported / updated / skipped


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """封装嵌入式 seekdb：documents 与 chunks 同集合，用 kind 区分。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._collection: Any = None

    # ---------- 初始化 ----------

    def initialize(self) -> bool:
        """确保目录、数据库与集合存在；返回集合是否为本次新建。"""
        self.settings.ensure()
        db_dir = str(self.settings.db_path)
        admin = pyseekdb.AdminClient(path=db_dir)
        names = [getattr(d, "name", str(d)) for d in admin.list_databases()]
        if self.settings.database not in names:
            admin.create_database(self.settings.database)
        client = pyseekdb.Client(path=db_dir, database=self.settings.database)
        try:
            self._collection = client.get_collection(self.settings.collection)
            return False
        except Exception:
            self._collection = client.create_collection(
                name=self.settings.collection,
                embedding_function=None,
                configuration=pyseekdb.HNSWConfiguration(dimension=self.settings.dimension, distance="cosine"),
            )
            return True

    @property
    def collection(self) -> Any:
        """惰性获取集合句柄：数据目录不存在时自动初始化。"""
        if self._collection is None:
            self.initialize()
        if self._collection is None:  # pragma: no cover
            raise NotInitializedError("无法打开 seekdb 集合，请先运行 readdesk init")
        return self._collection

    def refresh(self) -> None:
        """把新写入的向量刷入索引；不调用则 dense 查询看不到新数据。"""
        try:
            self.collection.refresh_index()
        except Exception as exc:  # pragma: no cover
            raise StoreError(f"刷新索引失败：{exc}") from exc

    # ---------- 统计 ----------

    def _ids_where(self, where: dict[str, Any]) -> list[str]:
        return list(self.collection.get(where=where, include=[])["ids"])

    def doc_count(self) -> int:
        """文档数。"""
        return len(self._ids_where({"kind": "document"}))

    def chunk_count(self) -> int:
        """证据块数。"""
        return len(self._ids_where({"kind": "chunk"}))

    def ensure_not_empty(self) -> None:
        """空库时抛出中文错误。"""
        if self.doc_count() == 0:
            raise EmptyLibraryError("库中还没有文档，请先运行 readdesk import <路径> 导入笔记")

    # ---------- 导入 ----------

    def import_file(self, path: Path, rel_path: str, extra_tags: list[str] | None = None) -> ImportOutcome:
        """导入单文件；内容 sha256 相同则跳过，否则删除旧块后重写（幂等）。"""
        doc = parse_document(path, rel_path)
        if extra_tags:
            merged = [normalize_tag(t) for t in extra_tags]
            doc.tags = sorted(set(doc.tags) | set(merged))
        doc_id = _doc_id(rel_path)
        old = self.collection.get(ids=[doc_id], include=["metadatas"])
        if old["ids"]:
            old_sha = (old["metadatas"][0] or {}).get("sha256")
            if old_sha == doc.sha256 and (old["metadatas"][0] or {}).get("tags_csv", "") == ",".join(doc.tags):
                n_chunks = len(self._ids_where({"kind": "chunk", "doc_id": doc_id}))
                return ImportOutcome(doc.rel_path, doc_id, n_chunks, "skipped")
            self.collection.delete(where={"doc_id": doc_id})
            status = "updated"
        else:
            status = "imported"
        self._write_document(doc, doc_id)
        return ImportOutcome(doc.rel_path, doc_id, len(doc.blocks), status)

    def _doc_meta(self, rel_path: str, title: str, authors: list[str], year: int | None,
                  tags: list[str], venue: str, doi: str) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "doc_id": _doc_id(rel_path),
            "path": rel_path,
            "title": title,
            "authors_csv": ", ".join(authors),
            "year": int(year) if year is not None else 0,
            "tags_csv": ",".join(tags),
            "venue": venue,
            "doi": doi,
        }
        for t in tags:
            meta[tag_field(t)] = True
        return meta

    def _write_document(self, doc: ParsedDocument, doc_id: str) -> None:
        meta = self._doc_meta(doc.rel_path, doc.title, doc.authors, doc.year, doc.tags, doc.venue, doc.doi)
        meta.update(kind="document", sha256=doc.sha256, imported_at=_now())
        doc_embedding = embed(f"{doc.title} {doc.venue} {','.join(doc.tags)}")
        self.collection.upsert(ids=[doc_id], embeddings=[doc_embedding], metadatas=[meta],
                               documents=[doc.title])
        chunk_ids: list[str] = []
        embeddings: list[list[float]] = []
        metadatas: list[dict[str, Any]] = []
        documents: list[str] = []
        for i, b in enumerate(doc.blocks):
            cm = dict(meta)
            cm.update(kind="chunk", section=b.section, start_line=b.start_line, end_line=b.end_line)
            chunk_ids.append(f"{doc_id}#{i:04d}")
            embeddings.append(embed(b.text))
            metadatas.append(cm)
            documents.append(b.text)
        if chunk_ids:
            self.collection.upsert(ids=chunk_ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    # ---------- 读取 ----------

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        """按 doc_id 返回文档元数据，不存在返回 None。"""
        res = self.collection.get(ids=[doc_id], include=["metadatas"])
        if not res["ids"]:
            return None
        meta = res["metadatas"][0] or {}
        if meta.get("kind") != "document":
            return None
        return meta

    def list_documents(self) -> list[dict[str, Any]]:
        """全部文档元数据，按路径排序。"""
        res = self.collection.get(where={"kind": "document"}, include=["metadatas"])
        metas = [m or {} for m in res["metadatas"]]
        return sorted(metas, key=lambda m: m.get("path", ""))

    def document_chunks(self, doc_id: str) -> list[dict[str, Any]]:
        """某文档的全部块（metadata + 正文），按起始行排序。"""
        res = self.collection.get(where={"kind": "chunk", "doc_id": doc_id},
                                  include=["metadatas", "documents"])
        out = []
        for i, cid in enumerate(res["ids"]):
            m = dict(res["metadatas"][i] or {})
            m["chunk_id"] = cid
            m["text"] = res["documents"][i] if res.get("documents") else ""
            out.append(m)
        return sorted(out, key=lambda m: (m.get("start_line", 0), m.get("end_line", 0)))

    def all_tags(self) -> list[str]:
        """库内出现过的全部标签。"""
        tags: set[str] = set()
        for d in self.list_documents():
            for t in str(d.get("tags_csv", "")).split(","):
                if t:
                    tags.add(t)
        return sorted(tags)

    def all_years(self) -> list[int]:
        """库内出现过的年份（排除 0/缺省）。"""
        years = {int(d.get("year", 0) or 0) for d in self.list_documents()}
        return sorted(y for y in years if y > 0)

    # ---------- 原始检索 ----------

    def dense_candidates(self, qvec: list[float], where: dict[str, Any], n: int = 30) -> list[dict[str, Any]]:
        """向量近邻查询（where 已下推到 seekdb）。"""
        res = self.collection.query(query_embeddings=[qvec], n_results=n, where=where,
                                    include=["metadatas", "documents", "distances"])
        out = []
        for i, cid in enumerate(res["ids"][0]):
            out.append({
                "chunk_id": cid,
                "metadata": res["metadatas"][0][i] or {},
                "text": res["documents"][0][i] if res.get("documents") else "",
                "distance": res["distances"][0][i] if res.get("distances") else None,
            })
        return out

    def fts_candidates(self, term: str, where: dict[str, Any], limit: int = 30) -> list[dict[str, Any]]:
        """$contains 全文过滤（where 同样下推）。"""
        res = self.collection.get(where_document={"$contains": term}, where=where, limit=limit,
                                  include=["metadatas", "documents"])
        out = []
        for i, cid in enumerate(res["ids"]):
            out.append({
                "chunk_id": cid,
                "metadata": res["metadatas"][i] or {},
                "text": res["documents"][i] if res.get("documents") else "",
            })
        return out

    def fetch_chunks(self, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        """按 id 批量取块内容（融合后补全缺失字段用）。"""
        if not chunk_ids:
            return {}
        res = self.collection.get(ids=chunk_ids, include=["metadatas", "documents"])
        out = {}
        for i, cid in enumerate(res["ids"]):
            out[cid] = {"metadata": res["metadatas"][i] or {},
                        "text": res["documents"][i] if res.get("documents") else ""}
        return out


def _doc_id(rel_path: str) -> str:
    """doc_id 由相对路径哈希得到，保证同路径重复导入落在同一文档上。"""
    return hashlib.sha256(("doc:" + rel_path).encode("utf-8")).hexdigest()[:16]

"""存储层：初始化、导入统计、幂等与读取接口。"""

from __future__ import annotations

from pathlib import Path

from conftest import run_cli
from readdesk.parsing import iter_import_files
from readdesk.store import Store


def test_initialize_then_reinit(tmp_path: Path) -> None:
    """子进程验证：空目录新建集合、计数为 0，二次 init 幂等。"""
    d = tmp_path / "fresh"
    r = run_cli("init", data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "（新建）" in r.stdout and "文档数：0，块数：0" in r.stdout
    r2 = run_cli("init", data_dir=d)
    assert r2.returncode == 0 and "（已存在）" in r2.stdout


def test_import_counts(lib_store: Store) -> None:
    assert lib_store.doc_count() == 6
    assert lib_store.chunk_count() >= 24


def test_reimport_is_idempotent(lib_store: Store, samples_dir: Path) -> None:
    docs_before, chunks_before = lib_store.doc_count(), lib_store.chunk_count()
    for path, rel in iter_import_files([samples_dir]):
        outcome = lib_store.import_file(path, rel)
        assert outcome.status == "skipped"
    lib_store.refresh()
    assert (lib_store.doc_count(), lib_store.chunk_count()) == (docs_before, chunks_before)


def test_modified_file_updates_chunks(lib_store: Store, tmp_path: Path) -> None:
    f = tmp_path / "note-x.md"
    f.write_text("---\ntitle: 试验笔记\nyear: 2023\ntags: [test]\n---\n\n# 甲\n\n第一段内容。\n", encoding="utf-8")
    o1 = lib_store.import_file(f, "note-x.md")
    assert o1.status == "imported"
    f.write_text(f.read_text(encoding="utf-8") + "\n## 乙\n\n第二段内容。\n", encoding="utf-8")
    o2 = lib_store.import_file(f, "note-x.md")
    assert o2.status == "updated"
    blocks = lib_store.document_chunks(o2.doc_id)
    assert len(blocks) == 2
    # 清理，避免影响其他断言
    lib_store.collection.delete(where={"doc_id": o1.doc_id})
    lib_store.refresh()
    assert lib_store.doc_count() == 6


def test_document_metadata_fields(lib_store: Store) -> None:
    docs = {d["path"]: d for d in lib_store.list_documents()}
    assert set(docs) == {
        "01-hybrid-retrieval.md", "02-chunking-strategies.md", "03-embedding-eval.md",
        "04-citation-grounding.md", "05-local-first-rag.md", "06-research-workflow.md",
    }
    d = docs["01-hybrid-retrieval.md"]
    assert isinstance(d["year"], int) and d["year"] == 2025
    assert "retrieval" in d["tags_csv"].split(",")
    assert d["tag_retrieval"] is True
    assert len(d["sha256"]) == 64
    meta = lib_store.get_document(d["doc_id"])
    assert meta and meta["title"].startswith("混合检索")
    assert lib_store.get_document("0" * 16) is None


def test_ensure_not_empty_raises(tmp_path: Path) -> None:
    """空库检索（子进程）应给出中文提示并非 0 退出。"""
    d = tmp_path / "empty-lib"
    run_cli("init", data_dir=d)
    r = run_cli("search", "混合检索", data_dir=d)
    assert r.returncode != 0 and "还没有" in r.stderr
    r2 = run_cli("brief", "任意主题", data_dir=d)
    assert r2.returncode != 0 and "还没有" in r2.stderr


def test_all_tags_and_years(lib_store: Store) -> None:
    assert "evaluation" in lib_store.all_tags()
    assert 2024 in lib_store.all_years() and 2026 in lib_store.all_years()

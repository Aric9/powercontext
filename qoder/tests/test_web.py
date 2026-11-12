"""Web API 测试：TestClient + 会话级临时库，不触碰仓库目录。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from readdesk.config import Settings
from readdesk.web.app import create_app


@pytest.fixture(scope="module")
def client(lib_store, lib_settings: Settings) -> TestClient:
    """基于已导入示例库的 TestClient。"""
    return TestClient(create_app(lib_settings))


def test_healthz(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["docs"] == 6 and body["chunks"] >= 24


def test_index_page(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "研读台" in res.text
    assert "http://" not in res.text and "https://" not in res.text  # 无任何外部资源引用
    assert "/static/style.css" in res.text


def test_search_page_mark_highlight(client: TestClient) -> None:
    res = client.get("/search", params={"q": "混合检索 怎么融合 BM25 和向量结果"})
    assert res.status_code == 200
    assert "<mark>" in res.text
    assert "01-hybrid-retrieval.md#L" in res.text
    assert "复制引用" in res.text


def test_search_empty_query_400(client: TestClient) -> None:
    assert client.get("/search", params={"q": "   "}).status_code == 400


def test_search_bad_year_400(client: TestClient) -> None:
    assert client.get("/search", params={"q": "检索", "year": "abcd"}).status_code == 400


def test_search_tag_filter(client: TestClient) -> None:
    res = client.get("/search", params={"q": "评测 哈希 embedding", "tag": "evaluation"})
    assert res.status_code == 200
    assert "03-embedding-eval.md" in res.text


def test_doc_page(client: TestClient) -> None:
    docs = client.get("/").text
    m = re.search(r"/doc/([0-9a-f]{16})", docs)
    assert m
    res = client.get(f"/doc/{m.group(1)}")
    assert res.status_code == 200
    assert "#L" in res.text
    assert client.get("/doc/" + "0" * 16).status_code == 404
    assert client.get("/doc/bad-id%27").status_code == 400


def test_brief_roundtrip(client: TestClient) -> None:
    res = client.post("/brief", data={"topic": "混合检索 分块 引用"}, follow_redirects=False)
    assert res.status_code == 303
    loc = res.headers["location"]
    page = client.get(loc)
    assert page.status_code == 200
    assert "覆盖统计" in page.text
    name = loc.rsplit("/", 1)[1]
    dl = client.get(f"/brief-file/{name}")
    assert dl.status_code == 200
    assert "text/markdown" in dl.headers["content-type"]
    assert dl.text.count("#L") >= 3


def test_brief_traversal_blocked(client: TestClient) -> None:
    assert client.get("/brief-file/..%2fSPEC.md").status_code == 404
    assert client.get("/brief/nonexistent.md").status_code == 404

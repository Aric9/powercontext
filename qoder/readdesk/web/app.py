"""FastAPI Web UI：首页统计、混合检索、简报生成与文档浏览（全部本地资源，离线可用）。"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from readdesk import __version__
from readdesk.brief import write_brief
from readdesk.config import Settings
from readdesk.embedding import extract_query_terms
from readdesk.retrieve import QueryError, hybrid_search
from readdesk.store import EmptyLibraryError, Store
from readdesk.web.markdown import render_markdown
from readdesk.web.views import build_view_results, chunks_to_views

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
SAMPLE_QUERIES = [
    "混合检索 怎么融合 BM25 和向量结果",
    "分块 重叠 比例",
    "引用 行号 落地",
    "哈希 embedding 评测",
    "本地优先 隐私 延迟",
]
_DOC_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def create_app(settings: Settings) -> FastAPI:
    """创建 Web 应用；数据目录不存在时自动初始化。"""
    store = Store(settings)
    store.initialize()
    app = FastAPI(title="ReadDesk 研读台", version=__version__, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(request: Request, tpl: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("version", __version__)
        ctx.setdefault("q", "")
        return templates.TemplateResponse(request, tpl, ctx, status_code=status_code)

    def error_page(request: Request, status: int, message: str) -> HTMLResponse:
        return render(request, "error.html", status_code=status, message=message)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(request, "index.html", docs=store.doc_count(), chunks=store.chunk_count(),
                      tags=store.all_tags(), years=store.all_years(), documents=store.list_documents(),
                      samples=SAMPLE_QUERIES)

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok", "docs": store.doc_count(), "chunks": store.chunk_count()})

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = "", tag: str = "", year: str = "", limit: str = "10"):
        q = q.strip()
        if not q:
            return error_page(request, 400, "查询不能为空，请输入关键词。")
        year_val: int | None = None
        if year.strip():
            if not re.fullmatch(r"\d{4}", year.strip()):
                return error_page(request, 400, f"年份不合法：{year!r}，应为 4 位数字。")
            year_val = int(year.strip())
        try:
            results = hybrid_search(store, q, tag=tag.strip() or None, year=year_val,
                                    limit=_parse_limit(limit))
        except QueryError as exc:
            return error_page(request, 400, str(exc))
        except EmptyLibraryError as exc:
            return error_page(request, 409, str(exc))
        terms = extract_query_terms(q)
        views = build_view_results(results, terms)
        return render(request, "search.html", docs=store.doc_count(), chunks=store.chunk_count(),
                      tags=store.all_tags(), results=views, terms=terms, q=q, tag=tag, year=year,
                      has_results=bool(views))

    @app.post("/brief")
    def brief(topic: str = Form(...), limit: str = Form("24")):
        topic = topic.strip()
        if not topic:
            return HTMLResponse("<p>主题不能为空。</p>", status_code=400)
        try:
            path = write_brief(store, topic, limit=_parse_limit(limit, default=24, maximum=100))
        except QueryError as exc:
            return HTMLResponse(f"<p>{exc}</p>", status_code=400)
        except EmptyLibraryError as exc:
            return HTMLResponse(f"<p>{exc}</p>", status_code=409)
        return RedirectResponse(url=f"/brief/{path.name}", status_code=303)

    @app.get("/brief/{name}", response_class=HTMLResponse)
    def show_brief(request: Request, name: str):
        path = _safe_brief_path(store.settings, name)
        if path is None:
            return error_page(request, 404, "简报文件不存在。")
        md = path.read_text(encoding="utf-8")
        return render(request, "brief.html", docs=store.doc_count(), chunks=store.chunk_count(),
                      tags=store.all_tags(), name=path.name, body=render_markdown(md))

    @app.get("/brief-file/{name}")
    def download_brief(name: str):
        path = _safe_brief_path(store.settings, name)
        if path is None:
            return JSONResponse({"detail": "简报文件不存在"}, status_code=404)
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)

    @app.get("/doc/{doc_id}", response_class=HTMLResponse)
    def show_doc(request: Request, doc_id: str):
        if not _DOC_ID_RE.match(doc_id):
            return error_page(request, 400, f"doc_id 格式不合法：{doc_id!r}")
        meta = store.get_document(doc_id)
        if meta is None:
            return error_page(request, 404, f"未找到文档：{doc_id}（可能尚未导入）")
        chunks = store.document_chunks(doc_id)
        return render(request, "doc.html", docs=store.doc_count(), chunks=store.chunk_count(),
                      tags=store.all_tags(), meta=meta, blocks=chunks_to_views(chunks), doc_id=doc_id)

    return app


def _parse_limit(raw: str, default: int = 10, maximum: int = 50) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _safe_brief_path(settings: Settings, name: str) -> Path | None:
    """只允许读取 briefs 目录下的 .md 文件，防目录穿越。"""
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".md"):
        return None
    candidate = (settings.briefs_dir / name).resolve()
    briefs = settings.briefs_dir.resolve()
    if candidate.parent != briefs or not candidate.is_file():
        return None
    return candidate

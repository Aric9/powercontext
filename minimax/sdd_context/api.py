"""HTTP API for the SDD context module.

Run with ``python -m sdd_context.cli serve --port 8800``.  The same core is used
by the CLI and the MCP server; this module only translates transport.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from sdd_context.config import Settings
from sdd_context.context import build_context, render_markdown, save_context
from sdd_context.indexer import index_all
from sdd_context.pipeline import load_sample
from sdd_context.state import PowerContextState, StateError
from sdd_context.store.seekdb_store import SeekDBStore


class IndexRequest(BaseModel):
    root: str | None = None


class ContextRequest(BaseModel):
    sample: str | None = None
    task: dict[str, Any] | None = None
    include_state: bool = True


class StateRequest(BaseModel):
    task_id: str
    status: str = "in_progress"
    completed: list[str] = Field(default_factory=list)
    todos: list[str] = Field(default_factory=list)
    verified: list[str] = Field(default_factory=list)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="SDD Context Module", version="0.1.0")
    lock = threading.Lock()
    store = SeekDBStore(settings)

    def _task(payload: ContextRequest) -> dict[str, Any]:
        if payload.sample:
            _, task = load_sample(settings, payload.sample)
            return task
        if payload.task:
            return payload.task
        raise HTTPException(status_code=422, detail="either sample or task is required")

    @app.get("/health")
    def health() -> dict[str, Any]:
        try:
            stats = store.stats()
            seekdb_ok = True
        except Exception as exc:  # noqa: BLE001
            stats, seekdb_ok = {"error": str(exc)}, False
        try:
            scope = PowerContextState(settings).scope_id()
        except StateError as exc:
            scope = f"error: {exc}"
        return {"status": "ok" if seekdb_ok else "degraded", "seekdb": stats, "powercontext_scope": scope}

    @app.post("/v1/index")
    def index(_: IndexRequest | None = None) -> dict[str, Any]:
        with lock:
            manifest = index_all(settings, store=store, reset=True)
        return {"counts": manifest["counts"], "seekdb": store.stats()}

    @app.post("/v1/context")
    def context(payload: ContextRequest) -> dict[str, Any]:
        task = _task(payload)
        state = None
        if payload.include_state:
            try:
                state = PowerContextState(settings).load(task["task_id"])
            except StateError as exc:
                state = None
                task = {**task, "state_warning": str(exc)}
        pack = build_context(task, settings, store, state=state)
        save_context(pack, settings.runs_dir)
        return pack

    @app.get("/v1/context/markdown")
    def context_markdown(sample: str) -> str:
        _, task = load_sample(settings, sample)
        pack = build_context(task, settings, store, state=None)
        return render_markdown(pack)

    @app.post("/v1/state")
    def save_state(payload: StateRequest) -> dict[str, Any]:
        try:
            return PowerContextState(settings).save(
                payload.task_id,
                status=payload.status,
                completed=payload.completed,
                todos=payload.todos,
                verified=payload.verified,
            )
        except StateError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/state/{task_id}")
    def get_state(task_id: str) -> dict[str, Any] | None:
        try:
            return PowerContextState(settings).load(task_id)
        except StateError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/stats")
    def stats() -> dict[str, Any]:
        return store.stats()

    @app.get("/v1/tools")
    def tools() -> list[dict[str, Any]]:
        from sdd_context.mcp_server import TOOLS

        return TOOLS

    return app

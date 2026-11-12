"""Task state persistence and recall through the PowerContext REST API.

One Memory entry is written per state version with ``kind=sdd_task_state``.
The newest version is reconstructed by listing the kind and reading each
immutable entry's text; this survives process restarts and works across
sessions without any local cache.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from sdd_context.config import Settings

KIND = "sdd_task_state"
SCOPE_TITLE = "SDD Refund Context"
SCOPE_SUMMARY = "工程上下文模块的任务进展、验证结果与跨会话恢复记录"


class StateError(RuntimeError):
    """Raised when PowerContext cannot serve a state request."""


class PowerContextState:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(base_url=settings.powercontext_url, timeout=20.0)
        self._scope_id: str | None = None

    # ------------------------------------------------------------------ scopes
    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise StateError(f"PowerContext request {method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise StateError(
                f"PowerContext {method} {path} returned {response.status_code}: {response.text[:500]}"
            )
        if not response.content:
            return {}
        return response.json()

    def scope_id(self) -> str:
        if self._scope_id:
            return self._scope_id
        if not _server_ready(self._client):
            raise StateError(f"PowerContext is not reachable at {self.settings.powercontext_url}")
        scopes = self._request("GET", "/v1/scopes").get("items", [])
        for scope in scopes:
            if scope.get("title") == SCOPE_TITLE:
                self._scope_id = scope["scope_id"]
                return self._scope_id
        created = self._request(
            "POST",
            "/v1/scopes",
            json={
                "title": SCOPE_TITLE,
                "summary": SCOPE_SUMMARY,
                "idempotency_key": "sdd-refund-scope-v1",
            },
        )
        self._scope_id = created["scope_id"]
        return self._scope_id

    # ------------------------------------------------------------------- write
    def load(self, task_id: str) -> dict[str, Any] | None:
        """Return the newest state version for ``task_id`` or ``None``."""

        scope = self.scope_id()
        payload = self._request(
            "POST",
            "/v1/atomic-memory/list",
            json={"scope_id": scope, "kind": KIND, "limit": 100},
        )
        latest: dict[str, Any] | None = None
        for item in payload.get("items", []):
            text = item.get("text", "")
            try:
                state = json.loads(text)
            except json.JSONDecodeError:
                continue
            if state.get("task_id") != task_id:
                continue
            state.setdefault("citation", item.get("artifact"))
            if latest is None or int(state.get("version", 0)) > int(latest.get("version", 0)):
                latest = state
        return latest

    def save(self, task_id: str, *, status: str, completed: list[str], todos: list[str],
             verified: list[str], context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one immutable state version and return it."""

        previous = self.load(task_id)
        version = int(previous.get("version", 0)) + 1 if previous else 1
        state: dict[str, Any] = {
            "schema": "sdd.task_state.v1",
            "task_id": task_id,
            "version": version,
            "status": status,
            "completed": completed[-12:],
            "todos": todos[-12:],
            "verified": verified[-12:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "context_snapshot": {
                "spec_clauses": (context or {}).get("sources", {}).get("spec_clauses", []),
                "code_symbols": (context or {}).get("sources", {}).get("code_symbols", []),
                "test_symbols": (context or {}).get("sources", {}).get("test_symbols", []),
            },
        }
        text = json.dumps(state, ensure_ascii=False)
        if len(text.encode("utf-8")) > 8000:
            state["completed"] = state["completed"][-6:]
            state["todos"] = state["todos"][-6:]
            state["verified"] = state["verified"][-6:]
            text = json.dumps(state, ensure_ascii=False)
        response = self._request(
            "POST",
            "/v1/memory/remember",
            json={
                "scope_id": self.scope_id(),
                "kind": KIND,
                "text": text,
                "description": f"{task_id} v{version} {status}"[:200],
            },
        )
        records = response.get("records", [])
        if records:
            state["citation"] = records[0].get("artifact")
        return state

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Cross-session recall of related task state entries."""

        payload = self._request(
            "POST",
            "/v1/memory/search",
            json={"scope_id": self.scope_id(), "query": query, "mode": "fts", "limit": limit},
        )
        hits = []
        for hit in payload.get("hits", []):
            memory = hit.get("memory", {})
            try:
                state = json.loads(memory.get("text", ""))
            except json.JSONDecodeError:
                state = {"text": memory.get("text", "")}
            state["score"] = hit.get("score")
            state["citation"] = memory.get("artifact")
            hits.append(state)
        return hits

    def history_text(self) -> list[str]:
        """Compact history used in the one-page technical report."""

        scope = self.scope_id()
        payload = self._request(
            "POST",
            "/v1/atomic-memory/list",
            json={"scope_id": scope, "kind": KIND, "limit": 100},
        )
        lines = []
        for item in payload.get("items", []):
            try:
                state = json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
            lines.append(
                f"v{state.get('version')} {state.get('task_id')} {state.get('status')} "
                f"completed={len(state.get('completed', []))} todos={len(state.get('todos', []))}"
            )
        return lines


def _server_ready(client: httpx.Client) -> bool:
    try:
        return client.get("/health/live", timeout=3.0).status_code == 200
    except Exception:
        return False

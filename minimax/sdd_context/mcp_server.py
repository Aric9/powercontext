"""Minimal MCP (stdio) server exposing the context module as tools."""

from __future__ import annotations

import json
import sys
from typing import Any

from sdd_context.config import Settings
from sdd_context.context import build_context
from sdd_context.indexer import index_all
from sdd_context.pipeline import load_sample
from sdd_context.state import PowerContextState
from sdd_context.store.seekdb_store import SeekDBStore

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "context_pack",
        "description": "Build the SDD development context (specs, code symbols, tests, constraints, task state).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sample": {"type": "string", "description": "Sample name, e.g. S1_refund_cap"},
                "task": {"type": "object", "description": "Inline task object"},
            },
        },
    },
    {
        "name": "index_workspace",
        "description": "Import specs, code and tests into seekdb.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_state_get",
        "description": "Read the newest saved task state from PowerContext.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "task_state_save",
        "description": "Append a task state version to PowerContext.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "todos": {"type": "array", "items": {"type": "string"}},
                "verified": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "status"],
        },
    },
]


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


def call_tool(settings: Settings, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    store = SeekDBStore(settings)
    if name == "index_workspace":
        return _text(index_all(settings, store=store, reset=True)["counts"])
    if name == "context_pack":
        if arguments.get("sample"):
            _, task = load_sample(settings, arguments["sample"])
        else:
            task = arguments.get("task") or {}
        state = None
        try:
            state = PowerContextState(settings).load(task.get("task_id", ""))
        except Exception:  # noqa: BLE001 - state is optional for this tool
            state = None
        return _text(build_context(task, settings, store, state=state))
    if name == "task_state_get":
        return _text(PowerContextState(settings).load(arguments["task_id"]))
    if name == "task_state_save":
        return _text(
            PowerContextState(settings).save(
                arguments["task_id"],
                status=arguments.get("status", "in_progress"),
                completed=arguments.get("completed", []),
                todos=arguments.get("todos", []),
                verified=arguments.get("verified", []),
            )
        )
    raise ValueError(f"unknown tool: {name}")


def _handle(settings: Settings, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sdd-context", "version": "0.1.0"},
            },
        }
    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            result = call_tool(settings, params.get("name", ""), params.get("arguments", {}))
        except Exception as exc:  # noqa: BLE001 - report tool errors over MCP
            result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main(settings: Settings | None = None) -> int:
    settings = settings or Settings.load()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(settings, request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Assemble a verification-ready context pack from seekdb and PowerContext."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sdd_context.config import Settings
from sdd_context.models import SourceItem
from sdd_context.retrieve import CLAUSE_RE, retrieve, to_source_item, task_text
from sdd_context.store.seekdb_store import SeekDBStore

MAX_TEXT_CHARS = 4000


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def verify_item(item: SourceItem, root: Path) -> bool:
    """Check that the cited file, line range and identity really exist."""

    path = root / item.source
    if not path.is_file():
        return False
    lines = _read_lines(path)
    if item.start_line < 1 or item.end_line < item.start_line or item.end_line > len(lines):
        return False
    window = "\n".join(lines[item.start_line - 1 : item.end_line])
    if item.kind == "spec" and item.clause_id and item.clause_id not in window:
        return False
    if item.kind in {"code", "test"} and item.symbol:
        leaf = item.symbol.split("::")[-1].split(".")[-1]
        if leaf and leaf not in window:
            return False
    return True


def _trim(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "\n... [truncated]"


def _constraints(task: dict[str, Any], specs: list[SourceItem]) -> list[str]:
    constraints = list(task.get("must_keep", []) or [])
    for item in specs:
        for line in item.text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("-", "*")) and any(word in stripped for word in ("不得", "必须", "禁止")):
                if stripped not in constraints:
                    constraints.append(stripped.lstrip("-* ").strip())
    return constraints[:12]


def _state_summary(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    return {
        "task_id": state.get("task_id"),
        "version": state.get("version"),
        "status": state.get("status"),
        "completed": state.get("completed", []),
        "todos": state.get("todos", []),
        "verified": state.get("verified", []),
        "updated_at": state.get("updated_at"),
        "source": "PowerContext memory",
    }


def build_context(
    task: dict[str, Any],
    settings: Settings,
    store: SeekDBStore,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the context pack consumed by the MiniMax agent."""

    started = time.perf_counter()
    result = retrieve(task, store)
    specs = [to_source_item(hit, settings.root) for hit in result["selected"]["spec"]]
    code = [to_source_item(hit, settings.root) for hit in result["selected"]["code"]]
    tests = [to_source_item(hit, settings.root) for hit in result["selected"]["test"]]

    for item in specs + code + tests:
        item.verified = verify_item(item, settings.root)
        item.text = _trim(item.text)
    specs = [item for item in specs if item.verified]
    code = [item for item in code if item.verified]
    tests = [item for item in tests if item.verified]

    pack: dict[str, Any] = {
        "schema": "sdd.context.v1",
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "task": task.get("request"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(settings.root),
        "change_scope": task.get("change_scope", []),
        "specs": [item.as_dict() for item in specs],
        "code": [item.as_dict() for item in code],
        "tests": [item.as_dict() for item in tests],
        "constraints": _constraints(task, specs),
        "state": _state_summary(state),
        "retrieval": {
            "backend": "seekdb",
            "collection": "sdd_chunks",
            "database_path": str(settings.seekdb_path),
            "embedding": {"name": "sdd-hash-embedding", "dimension": settings.embedding_dimension},
            "queries": len(result["trace"]["dense"]) + len(result["trace"]["lexical"]),
            "candidates": result["trace"]["candidates"],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "trace": result["trace"],
        },
        "sources": {
            "spec_clauses": [item.clause_id for item in specs if item.clause_id],
            "code_symbols": [item.symbol for item in code if item.symbol],
            "test_symbols": [item.symbol for item in tests if item.symbol],
        },
    }
    pack["coverage"] = coverage(pack)
    return pack


def coverage(pack: dict[str, Any]) -> dict[str, Any]:
    """Summarise which source categories were located."""

    return {
        "specs": len(pack["specs"]),
        "code": len(pack["code"]),
        "tests": len(pack["tests"]),
        "state_restored": bool(pack.get("state")),
        "all_sources_verified": all(
            item["verified"] for group in ("specs", "code", "tests") for item in pack[group]
        ),
    }


def save_context(pack: dict[str, Any], runs_dir: Path) -> Path:
    task_id = str(pack.get("task_id") or "task")
    target = runs_dir / task_id / "context.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def render_markdown(pack: dict[str, Any]) -> str:
    """Human-readable rendering used by the CLI and HTTP API."""

    stats = pack.get("coverage") or coverage(pack)
    lines: list[str] = [
        f"# 开发上下文：{pack.get('title') or pack.get('task_id')}",
        "",
        f"- 任务：{pack.get('task')}",
        f"- 检索后端：{pack['retrieval']['backend']} / {pack['retrieval']['collection']}"
        f"（候选 {pack['retrieval']['candidates']}，{pack['retrieval']['elapsed_ms']} ms）",
        f"- 来源校验：{'全部通过' if stats['all_sources_verified'] else '存在未通过项'}",
        "",
        "## 适用规格",
    ]
    for item in pack["specs"]:
        lines.append(f"### {item.get('clause_id') or item['title']} — {item['locator']}")
        lines.append(item["text"])
        lines.append("")
    lines.append("## 需要修改的代码")
    for item in pack["code"]:
        lines.append(f"### {item.get('symbol')} — {item['locator']}")
        lines.append("```python")
        lines.append(item["text"])
        lines.append("```")
        lines.append("")
    lines.append("## 验证测试")
    for item in pack["tests"]:
        lines.append(f"- `{item.get('symbol')}` — {item['locator']}")
    lines.append("")
    lines.append("## 必须保持的行为")
    for constraint in pack["constraints"]:
        lines.append(f"- {constraint}")
    lines.append("")
    if pack.get("state"):
        state = pack["state"]
        lines.append("## 任务状态（PowerContext）")
        lines.append(f"- 状态：{state.get('status')}（v{state.get('version')}）")
        for done in state.get("completed", []):
            lines.append(f"- [x] {done}")
        for todo in state.get("todos", []):
            lines.append(f"- [ ] {todo}")
    return "\n".join(lines).strip() + "\n"


def cited_clause_ids(pack: dict[str, Any]) -> list[str]:
    return [item["clause_id"] for item in pack["specs"] if item.get("clause_id")]


def required_clause_ids(text: str) -> list[str]:
    return sorted(set(CLAUSE_RE.findall(text)))


__all__ = [
    "build_context",
    "save_context",
    "render_markdown",
    "coverage",
    "verify_item",
    "task_text",
    "cited_clause_ids",
]

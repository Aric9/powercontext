"""MiniMax development assistant.

The assistant receives the assembled context pack (specs, code, tests,
constraints, state) and returns complete replacement files as strict JSON.  The
run directory keeps the exact request, raw response, parsed generation and the
resulting patch so the change can be audited against the committed diff.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from sdd_context.config import Settings

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

SYSTEM_PROMPT = """你是一名资深 Python 工程师，在规格驱动开发（SDD）流程中工作。
你只能依据给定的上下文修改代码，不得引入上下文之外的文件或依赖。
输出必须是严格 JSON，不要输出 markdown 代码围栏，不要输出多余解释。"""


class AgentError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_messages(pack: dict[str, Any], files: dict[str, str]) -> list[dict[str, str]]:
    sections: list[str] = [
        "# 任务",
        str(pack.get("task") or ""),
        "",
        "# 适用规格条款",
    ]
    for item in pack.get("specs", []):
        sections.append(f"[{item.get('clause_id') or item['title']}] {item['locator']}")
        sections.append(item["text"])
        sections.append("")

    sections.append("# 必须保持的既有行为")
    for constraint in pack.get("constraints", []):
        sections.append(f"- {constraint}")
    sections.append("")

    sections.append("# 相关代码（完整文件内容）")
    for path, content in files.items():
        sections.append(f"文件：{path}")
        sections.append("```python")
        sections.append(content)
        sections.append("```")
        sections.append("")

    sections.append("# 验证测试")
    for item in pack.get("tests", []):
        sections.append(f"- {item.get('symbol')} @ {item['locator']}")
        sections.append(item["text"][:1600])
        sections.append("")

    if pack.get("state"):
        state = pack["state"]
        sections.append("# 已保存的任务状态（来自 PowerContext）")
        sections.append(f"status={state.get('status')} version={state.get('version')}")
        if state.get("completed"):
            sections.append("已完成：" + "；".join(state["completed"]))
        if state.get("todos"):
            sections.append("待办：" + "；".join(state["todos"]))

    allowed = sorted(files)
    sections.extend(
        [
            "# 输出要求",
            "返回一个 JSON 对象，结构如下：",
            "{",
            '  "summary": "一句话说明修改内容",',
            '  "rationale": [{"clause_id": "REF-003", "why": "..."}],',
            '  "files": [{"path": "business/refund/service.py", "content": "<完整文件内容>"}],',
            '  "verification": ["修改后应运行的测试或检查"]',
            "}",
            "要求：",
            f"1. files 中的 path 只能是：{', '.join(allowed)}；每个文件必须给出完整、可直接运行的 Python 源码。",
            "2. 不得修改测试文件，不得删除或改写断言。",
            "3. 不得使用二进制浮点数计算金额；使用 Decimal 与已有的 money.round_money。",
            "4. 只输出 JSON，不要 markdown 围栏，不要解释 JSON 之外的内容。",
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(sections)},
    ]


def _clean_content(content: str) -> str:
    text = THINK_RE.sub("", content)
    text = text.strip()
    if text.startswith("```"):
        text = FENCE_RE.sub("", text)
    return text.strip()


def parse_generation(text: str) -> dict[str, Any]:
    cleaned = _clean_content(text)
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            return data
    raise AgentError("MiniMax response did not contain a JSON object with a files array")


def _unified_diff(path: str, before: str, after: str) -> str:
    import difflib

    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    return "".join(diff)


class MiniMaxAgent:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=300.0)

    def _api_key(self) -> str:
        key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not key:
            raise AgentError("MINIMAX_API_KEY is not set; export it before running the agent step")
        return key

    def generate(self, pack: dict[str, Any], files: dict[str, str], run_dir: Path) -> dict[str, Any]:
        """Call MiniMax and persist every artefact of the call."""

        run_dir.mkdir(parents=True, exist_ok=True)
        messages = build_messages(pack, files)
        payload = {
            "model": self.settings.minimax_model,
            "messages": messages,
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 8000,
        }
        request_record = {
            "endpoint": self.settings.minimax_base_url + "/chat/completions",
            "model": self.settings.minimax_model,
            "params": {key: value for key, value in payload.items() if key != "messages"},
            "messages": messages,
            "system_prompt_chars": len(messages[0]["content"]),
            "user_prompt_chars": len(messages[1]["content"]),
        }
        (run_dir / "request.json").write_text(
            json.dumps(request_record, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        headers = {"Authorization": "Bearer " + self._api_key(), "Content-Type": "application/json"}
        started = time.perf_counter()
        last_error: Exception | None = None
        response_json: dict[str, Any] | None = None
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(
                    self.settings.minimax_base_url + "/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if response.status_code >= 400:
                    raise AgentError(f"MiniMax HTTP {response.status_code}: {response.text[:500]}")
                response_json = response.json()
                (run_dir / f"response.attempt{attempt}.json").write_text(
                    json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                content = response_json["choices"][0]["message"]["content"]
                generation = parse_generation(content)
                break
            except Exception as exc:  # noqa: BLE001 - retry any malformed generation
                last_error = exc
                if attempt == attempts:
                    raise AgentError(f"MiniMax generation failed after {attempts} attempts: {exc}") from exc
                payload["temperature"] = 0.0
                payload["messages"] = messages + [
                    {"role": "user", "content": "上一次输出不是合法 JSON。请只输出要求的 JSON 对象。"}
                ]
        else:  # pragma: no cover - defensive
            raise AgentError(str(last_error))

        elapsed = time.perf_counter() - started
        assert response_json is not None
        (run_dir / "response.json").write_text(
            json.dumps(response_json, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (run_dir / "generation.json").write_text(
            json.dumps(generation, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        patches: list[str] = []
        changed: list[str] = []
        for entry in generation.get("files", []):
            path = str(entry.get("path", "")).replace("\\", "/")
            content = str(entry.get("content", ""))
            if path not in files:
                continue
            diff = _unified_diff(path, files[path], content)
            if diff:
                patches.append(diff)
                changed.append(path)
        patch_text = "\n".join(patches)
        (run_dir / "patch.diff").write_text(patch_text, encoding="utf-8")

        usage = response_json.get("usage", {})
        meta = {
            "provider": "MiniMax",
            "model": response_json.get("model", self.settings.minimax_model),
            "endpoint": self.settings.minimax_base_url,
            "started_at": _now(),
            "latency_seconds": round(elapsed, 2),
            "attempts": attempt,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            "changed_files": changed,
            "returned_files": [entry.get("path") for entry in generation.get("files", [])],
            "rationale": generation.get("rationale", []),
            "summary": generation.get("summary", ""),
            "verification": generation.get("verification", []),
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "run_dir": run_dir,
            "generation": generation,
            "patch": patch_text,
            "changed_files": changed,
            "meta": meta,
        }

    def apply(self, generation: dict[str, Any], allowed: list[str]) -> list[str]:
        """Write generated files, refusing anything outside the declared scope."""

        written: list[str] = []
        for entry in generation.get("files", []):
            path = str(entry.get("path", "")).replace("\\", "/")
            content = entry.get("content")
            if path not in allowed:
                raise AgentError(f"generated file outside change_scope: {path}")
            if not isinstance(content, str) or not content.strip():
                raise AgentError(f"generated content for {path} is empty")
            target = self.settings.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(path)
        if not written:
            raise AgentError("generation did not modify any allowed file")
        return written

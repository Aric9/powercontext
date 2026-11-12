from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdd_context.agent import AgentError, parse_generation

ROOT = Path(__file__).resolve().parents[2]


def test_parse_generation_plain_json() -> None:
    payload = {"summary": "s", "files": [{"path": "business/refund/service.py", "content": "x = 1\n"}]}
    parsed = parse_generation(json.dumps(payload))
    assert parsed["files"][0]["path"] == "business/refund/service.py"


def test_parse_generation_strips_think_and_fence() -> None:
    payload = {"summary": "s", "files": [{"path": "a.py", "content": "x = 1\n"}]}
    text = "<think>internal reasoning</think>\n```json\n" + json.dumps(payload) + "\n```"
    parsed = parse_generation(text)
    assert parsed["summary"] == "s"


def test_parse_generation_rejects_non_json() -> None:
    with pytest.raises(AgentError):
        parse_generation("I changed the file, trust me.")


def test_render_markdown_and_coverage() -> None:
    from sdd_context.context import coverage, render_markdown

    pack = {
        "task_id": "T",
        "title": "任务",
        "task": "改代码",
        "specs": [
            {
                "kind": "spec",
                "title": "累计上限",
                "source": "business/specs/refund-policy.md",
                "locator": "business/specs/refund-policy.md#L16-L21",
                "text": "REF-003 累计上限",
                "clause_id": "REF-003",
                "verified": True,
                "score": 0.5,
                "matched_by": [],
            }
        ],
        "code": [],
        "tests": [],
        "constraints": ["保持幂等"],
        "state": {"status": "in_progress", "version": 1, "completed": ["导入"], "todos": ["改码"]},
        "retrieval": {"backend": "seekdb", "collection": "sdd_chunks", "candidates": 3, "elapsed_ms": 1.2},
    }
    markdown = render_markdown(pack)
    assert "REF-003" in markdown
    assert "保持幂等" in markdown
    assert "PowerContext" in markdown
    assert coverage(pack)["all_sources_verified"] is True


def test_verify_item_checks_real_lines() -> None:
    from sdd_context.context import verify_item
    from sdd_context.models import SourceItem

    item = SourceItem(
        kind="spec",
        title="累计上限",
        source="business/specs/refund-policy.md",
        locator="business/specs/refund-policy.md#L16-L21",
        text="REF-003",
        start_line=16,
        end_line=21,
        clause_id="REF-003",
    )
    assert verify_item(item, ROOT) is True
    bad = SourceItem(
        kind="spec",
        title="x",
        source="business/specs/refund-policy.md",
        locator="x",
        text="REF-999",
        start_line=9000,
        end_line=9001,
        clause_id="REF-999",
    )
    assert verify_item(bad, ROOT) is False

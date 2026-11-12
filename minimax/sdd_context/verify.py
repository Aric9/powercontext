"""Acceptance checks for the six mandatory review items.

Run from the repository's ``minimax/`` directory:

    python -m sdd_context.verify

Each check prints PASS/FAIL and writes ``runs/acceptance/acceptance.json`` plus a
human-readable Markdown report.  Checks never modify tracked files: temporary
workspaces are created under the system temp directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sdd_context.agent import MiniMaxAgent
from sdd_context.config import Settings
from sdd_context.context import build_context
from sdd_context.indexer import index_all
from sdd_context.pipeline import load_sample, reset_initial, run_pytest
from sdd_context.powercontext_boot import start as start_powercontext
from sdd_context.powercontext_boot import stop as stop_powercontext
from sdd_context.state import PowerContextState
from sdd_context.store.seekdb_store import SeekDBStore

SAMPLE = "S1_refund_cap"


def _copy_business(source: Path, target: Path) -> None:
    shutil.copytree(source / "business", target / "business")
    shutil.copytree(source / "samples", target / "samples")
    shutil.copy2(source / "pytest.ini", target / "pytest.ini")


def _temp_settings(base: Path) -> Settings:
    settings = Settings.load(base)
    settings.data_dir = base / ".sdd"
    settings.seekdb_path = settings.data_dir / "seekdb"
    settings.runs_dir = base / "runs"
    return settings


def check_environment(settings: Settings) -> dict[str, Any]:
    details: dict[str, Any] = {"python": sys.version.split()[0], "root": str(settings.root)}
    try:
        store = SeekDBStore(settings)
        details["seekdb"] = store.stats()
        details["index_present"] = details["seekdb"]["total"] > 0
    except Exception as exc:  # noqa: BLE001
        details["seekdb_error"] = str(exc)
        return {"name": "1. 能够运行（环境与索引）", "passed": False, "details": details}
    passed = details["index_present"] and details["seekdb"]["by_kind"]["spec"] >= 7
    return {"name": "1. 能够运行（环境与索引）", "passed": passed, "details": details}


def check_context(settings: Settings) -> dict[str, Any]:
    sample_dir, task = load_sample(settings, SAMPLE)
    expected = json.loads((sample_dir / "expected_context.json").read_text(encoding="utf-8"))
    store = SeekDBStore(settings)
    pack = build_context(task, settings, store)
    clauses = {item.get("clause_id") for item in pack["specs"]}
    symbols = {item.get("symbol") for item in pack["code"]}
    tests = {item.get("symbol", "").split("::")[-1] for item in pack["tests"]}
    missing = {
        "clauses": sorted(set(expected["required_clause_ids"]) - clauses),
        "symbols": sorted(set(expected["required_symbols"]) - symbols),
        "tests": sorted(set(expected["required_test_symbols"]) - tests),
    }
    verified = all(item["verified"] for group in ("specs", "code", "tests") for item in pack[group])
    passed = not any(missing.values()) and verified and bool(pack["constraints"])
    return {
        "name": "3. 上下文正确（规格/代码/测试/约束）",
        "passed": passed,
        "details": {
            "clauses": sorted(clauses),
            "symbols": sorted(symbol for symbol in symbols if symbol),
            "tests": sorted(tests),
            "missing": missing,
            "all_sources_verified": verified,
            "constraints": len(pack["constraints"]),
            "retrieval_ms": pack["retrieval"]["elapsed_ms"],
        },
    }


def check_spec_update(settings: Settings) -> dict[str, Any]:
    """Update a spec in a temp workspace, re-import, and prove the output changed."""

    with tempfile.TemporaryDirectory(prefix="sdd-spec-") as tmp:
        base = Path(tmp)
        _copy_business(settings.root, base)
        temp_settings = _temp_settings(base)
        _, task = load_sample(temp_settings, SAMPLE)

        spec_path = base / "business" / "specs" / "refund-policy.md"
        original = spec_path.read_text(encoding="utf-8")
        store = SeekDBStore(temp_settings)
        index_all(temp_settings, store=store, reset=True)
        pack_before = build_context(task, temp_settings, store)
        before_text = next(
            (item["text"] for item in pack_before["specs"] if item.get("clause_id") == "REF-004"), ""
        )

        updated = original.replace("按申请金额的 90% 退款", "按申请金额的 80% 退款")
        if updated == original:
            return {"name": "3b. 规格更新后重新导入生效", "passed": False, "details": {"error": "marker not found"}}
        spec_path.write_text(updated, encoding="utf-8")
        store2 = SeekDBStore(temp_settings)
        index_all(temp_settings, store=store2, reset=True)
        pack_after = build_context(task, temp_settings, store2)
        after_text = next(
            (item["text"] for item in pack_after["specs"] if item.get("clause_id") == "REF-004"), ""
        )

        passed = "80%" in after_text and "90%" in before_text and "90%" not in after_text
        return {
            "name": "3b. 规格更新后重新导入生效",
            "passed": passed,
            "details": {
                "before_excerpt": before_text[:120],
                "after_excerpt": after_text[:120],
                "before_has_90": "90%" in before_text,
                "after_has_80": "80%" in after_text,
            },
        }


def check_state(settings: Settings) -> dict[str, Any]:
    task_id = f"acceptance-{int(time.time())}"
    start_powercontext(settings)
    client = PowerContextState(settings)
    first = client.save(
        task_id,
        status="in_progress",
        completed=["导入工程资料"],
        todos=["完成代码修改"],
        verified=[],
    )
    stop_powercontext(settings)
    start_powercontext(settings)
    reloaded = PowerContextState(settings).load(task_id)
    second = PowerContextState(settings).save(
        task_id,
        status="verified",
        completed=["导入工程资料", "代码修改并通过测试"],
        todos=[],
        verified=["target tests passed"],
    )
    third = PowerContextState(settings).load(task_id)
    passed = (
        reloaded is not None
        and reloaded.get("version") == first["version"]
        and reloaded.get("status") == "in_progress"
        and third is not None
        and third.get("version") == second["version"] == first["version"] + 1
        and third.get("status") == "verified"
    )
    return {
        "name": "5. 状态能够复用（PowerContext 重启后读取）",
        "passed": passed,
        "details": {
            "after_restart": reloaded,
            "latest": third,
            "citation": second.get("citation"),
        },
    }


def check_minimax(settings: Settings) -> dict[str, Any]:
    recorded = settings.root / "runs" / "recorded" / SAMPLE
    required = ["meta.json", "request.json", "response.json", "generation.json", "patch.diff"]
    missing = [name for name in required if not (recorded / name).exists()]
    if missing:
        return {"name": "2. MiniMax 实际参与", "passed": False, "details": {"missing": missing}}
    meta = json.loads((recorded / "meta.json").read_text(encoding="utf-8"))
    request = json.loads((recorded / "request.json").read_text(encoding="utf-8"))
    generation = json.loads((recorded / "generation.json").read_text(encoding="utf-8"))
    patch = (recorded / "patch.diff").read_text(encoding="utf-8")
    prompt = "\n".join(message.get("content", "") for message in request.get("messages", []))
    usage = meta.get("usage", {})
    passed = (
        "minimax" in str(meta.get("provider", "")).lower()
        and int(usage.get("prompt_tokens") or 0) > 0
        and int(usage.get("completion_tokens") or 0) > 0
        and "REF-003" in prompt
        and any("service.py" in str(item.get("path", "")) for item in generation.get("files", []))
        and "exceeds_refundable" in patch
        and len(patch.splitlines()) > 20
    )
    return {
        "name": "2. MiniMax 实际参与",
        "passed": passed,
        "details": {
            "provider": meta.get("provider"),
            "model": meta.get("model"),
            "usage": usage,
            "prompt_chars": len(prompt),
            "changed_files": meta.get("changed_files"),
            "patch_lines": len(patch.splitlines()),
            "summary": meta.get("summary"),
        },
    }


def check_code_change(settings: Settings) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sdd-code-") as tmp:
        base = Path(tmp)
        _copy_business(settings.root, base)
        temp_settings = _temp_settings(base)
        sample_dir, task = load_sample(temp_settings, SAMPLE)
        reset_initial(temp_settings, sample_dir)

        before_target = run_pytest(base, task["target_tests"], cwd=base)
        before_regression = run_pytest(base, task["regression_tests"], cwd=base)
        generation = json.loads(
            (settings.root / "runs" / "recorded" / SAMPLE / "generation.json").read_text(encoding="utf-8")
        )
        MiniMaxAgent(temp_settings).apply(generation, task.get("change_scope", []))
        after_target = run_pytest(base, task["target_tests"], cwd=base)
        after_regression = run_pytest(base, task["regression_tests"], cwd=base)

        generated = generation["files"][0]["content"]
        committed = (settings.root / task["change_scope"][0]).read_text(encoding="utf-8")
        matches_commit = generated.strip() == committed.strip()

        passed = (
            not before_target["passed"]
            and before_regression["passed"]
            and after_target["passed"]
            and after_regression["passed"]
            and matches_commit
        )
        return {
            "name": "6. 代码修改有效（目标测试先失败后通过）",
            "passed": passed,
            "details": {
                "target_before": before_target["passed"],
                "regression_before": before_regression["passed"],
                "target_after": after_target["passed"],
                "regression_after": after_regression["passed"],
                "matches_committed_source": matches_commit,
                "before_tail": before_target["output"][-600:],
                "after_tail": after_target["output"][-400:],
            },
        }


def check_interfaces(settings: Settings) -> dict[str, Any]:
    """Prove that CLI, HTTP app and MCP stdio expose the same context."""

    details: dict[str, Any] = {}
    cli = subprocess.run(
        [sys.executable, "-m", "sdd_context.cli", "context", "--sample", SAMPLE, "--json", "--no-state"],
        cwd=str(settings.root),
        capture_output=True,
        text=True,
        timeout=180,
    )
    try:
        pack = json.loads(cli.stdout)
        details["cli"] = {"returncode": cli.returncode, "clauses": pack["sources"]["spec_clauses"]}
    except json.JSONDecodeError:
        details["cli"] = {"returncode": cli.returncode, "error": cli.stderr[-400:]}

    from sdd_context.api import create_app

    from sdd_context.mcp_server import TOOLS

    app = create_app(settings)
    details["http_routes"] = sorted(route.path for route in app.routes)
    details["mcp_tools"] = [tool["name"] for tool in TOOLS]
    passed = (
        details["cli"].get("returncode") == 0
        and "/v1/context" in details["http_routes"]
        and "context_pack" in details["mcp_tools"]
    )
    return {"name": "1b. 调用入口（CLI / HTTP / MCP）", "passed": passed, "details": details}


def main() -> int:
    settings = Settings.load()
    checks: list[dict[str, Any]] = []

    # A missing index is rebuilt so a clean checkout can run acceptance directly.
    store = SeekDBStore(settings)
    if store.count() == 0:
        index_all(settings, store=store, reset=True)

    checks.append(check_environment(settings))
    checks.append(check_context(settings))
    checks.append(check_minimax(settings))
    checks.append(check_spec_update(settings))
    checks.append(check_state(settings))
    checks.append(check_code_change(settings))
    checks.append(check_interfaces(settings))

    passed = all(check["passed"] for check in checks)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(settings.root),
        "passed": passed,
        "checks": checks,
    }
    out_dir = settings.root / "runs" / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "acceptance.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# 验收自测结果", "", f"- 结论：{'全部通过' if passed else '存在未通过项'}", ""]
    for check in checks:
        lines.append(f"## {'PASS' if check['passed'] else 'FAIL'} — {check['name']}")
        lines.append("```json")
        lines.append(json.dumps(check["details"], indent=2, ensure_ascii=False)[:4000])
        lines.append("```")
        lines.append("")
    (out_dir / "acceptance.md").write_text("\n".join(lines), encoding="utf-8")

    for check in checks:
        print(f"{'PASS' if check['passed'] else 'FAIL'}  {check['name']}")
    print(f"\nreport: {out_dir / 'acceptance.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end pipeline used by the demo and acceptance checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sdd_context.agent import MiniMaxAgent
from sdd_context.config import Settings
from sdd_context.context import build_context, render_markdown, save_context
from sdd_context.indexer import index_all
from sdd_context.powercontext_boot import start as start_powercontext
from sdd_context.powercontext_boot import stop as stop_powercontext
from sdd_context.state import PowerContextState
from sdd_context.store.seekdb_store import SeekDBStore


def _run_dir(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / stamp


def run_pytest(root: Path, targets: list[str], cwd: Path | None = None) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
    started = time.perf_counter()
    env = {**os.environ, "PYTHONPATH": str(cwd or root)}
    proc = subprocess.run(
        cmd,
        cwd=str(cwd or root),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "seconds": round(time.perf_counter() - started, 2),
        "output": output.strip(),
    }


def reset_initial(settings: Settings, sample_dir: Path) -> list[str]:
    """Restore the sample's declared initial files (pre-change state)."""

    initial = sample_dir / "initial"
    restored: list[str] = []
    if not initial.exists():
        return restored
    for path in sorted(initial.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(initial)
        target = settings.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        restored.append(relative.as_posix())
    return restored


def load_sample(settings: Settings, sample: str) -> tuple[Path, dict[str, Any]]:
    sample_dir = settings.root / "samples" / sample
    task = json.loads((sample_dir / "task.json").read_text(encoding="utf-8"))
    return sample_dir, task


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _log(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def demo(
    settings: Settings,
    sample: str = "S1_refund_cap",
    live: bool = False,
    out: Path | None = None,
) -> dict[str, Any]:
    """Run the full import → context → MiniMax → verify → state flow."""

    sample_dir, task = load_sample(settings, sample)
    run_dir = out or _run_dir(settings.runs_dir / "demo")
    run_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "sample": sample,
        "mode": "live" if live else "replay",
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    # 1. Clean, reproducible starting point: empty data dir and declared baseline files.
    stop_powercontext(settings)
    if settings.data_dir.exists():
        shutil.rmtree(settings.data_dir, ignore_errors=True)
    restored = reset_initial(settings, sample_dir)
    result["steps"]["reset"] = {"restored": restored, "data_dir": str(settings.data_dir)}

    # 2. Import specs/code/tests into a fresh seekdb collection.
    store = SeekDBStore(settings)
    manifest = index_all(settings, store=store, reset=True)
    stats = store.stats()
    result["steps"]["index"] = {"manifest": manifest["counts"], "stats": stats}

    # 3. Start PowerContext on an empty database.
    server = start_powercontext(settings)
    state_client = PowerContextState(settings)
    result["steps"]["powercontext"] = server

    # 4. Baseline tests: target must fail, regression must pass.
    target = task.get("target_tests", [])
    regression = task.get("regression_tests", [])
    before_target = run_pytest(settings.root, target, cwd=settings.root)
    before_regression = run_pytest(settings.root, regression, cwd=settings.root)
    _log(before_target["output"], run_dir / "tests_before_target.txt")
    _log(before_regression["output"], run_dir / "tests_before_regression.txt")
    result["steps"]["tests_before"] = {
        "target_passed": before_target["passed"],
        "regression_passed": before_regression["passed"],
        "target_output_tail": before_target["output"][-1200:],
    }
    if before_target["passed"]:
        raise RuntimeError("target tests passed on the baseline; the sample cannot demonstrate a fix")
    if not before_regression["passed"]:
        raise RuntimeError("regression tests failed on the baseline")

    # 5. Persist the starting task state in PowerContext (version 1).
    state_v1 = state_client.save(
        task["task_id"],
        status="in_progress",
        completed=["导入规格、代码和测试到 seekdb", "运行基线测试并确认目标用例失败"],
        todos=["修复累计退款上限", "改为 half-up 金额舍入", "运行回归测试并保存验证结果"],
        verified=[],
    )
    _json(run_dir / "state_v1.json", state_v1)

    # 6. Build the context pack from seekdb + PowerContext.
    pack = build_context(task, settings, store, state=state_v1)
    _json(run_dir / "context_before.json", pack)
    _log(render_markdown(pack), run_dir / "context_before.md")
    result["steps"]["context"] = {
        "coverage": pack["coverage"],
        "clauses": pack["sources"]["spec_clauses"],
        "symbols": pack["sources"]["code_symbols"],
        "tests": pack["sources"]["test_symbols"],
        "constraints": len(pack["constraints"]),
        "state_restored": bool(pack["state"]),
    }

    files = {
        path: _read(settings.root / path)
        for path in task.get("change_scope", [])
        if (settings.root / path).is_file()
    }
    if not files:
        raise RuntimeError("change_scope does not contain any existing file")

    # 7. MiniMax generates the change (or the recorded generation is replayed).
    agent = MiniMaxAgent(settings)
    if live:
        generation = agent.generate(pack, files, run_dir)
        generation_record = generation["generation"]
        _json(run_dir / "generation.json", generation_record)
        result["steps"]["minimax"] = generation["meta"]
    else:
        recorded = settings.root / "runs" / "recorded" / sample
        recorded_generation = json.loads((recorded / "generation.json").read_text(encoding="utf-8"))
        generation_record = recorded_generation
        shutil.copy2(recorded / "generation.json", run_dir / "generation.json")
        for name in ("request.json", "response.json", "meta.json", "patch.diff"):
            if (recorded / name).exists():
                shutil.copy2(recorded / name, run_dir / name)
        result["steps"]["minimax"] = {
            "mode": "replay",
            "source": str(recorded),
            "meta": json.loads((recorded / "meta.json").read_text(encoding="utf-8")),
        }

    # 8. Apply only inside the declared change scope.
    written = agent.apply(generation_record, task.get("change_scope", []))
    result["steps"]["apply"] = {"written": written}

    # 9. Verify the changed behaviour.
    after_target = run_pytest(settings.root, target, cwd=settings.root)
    after_regression = run_pytest(settings.root, regression, cwd=settings.root)
    _log(after_target["output"], run_dir / "tests_after_target.txt")
    _log(after_regression["output"], run_dir / "tests_after_regression.txt")
    result["steps"]["tests_after"] = {
        "target_passed": after_target["passed"],
        "regression_passed": after_regression["passed"],
        "target_output_tail": after_target["output"][-1200:],
    }
    if not after_target["passed"] or not after_regression["passed"]:
        raise RuntimeError("tests did not pass after applying the generated change")

    # 10. Record the verified state (version 2), then restart PowerContext.
    state_v2 = state_client.save(
        task["task_id"],
        status="verified",
        completed=[
            "导入规格、代码和测试到 seekdb",
            "运行基线测试并确认目标用例失败",
            "由 MiniMax 依据上下文生成代码修改",
            "目标测试与回归测试全部通过",
        ],
        todos=[],
        verified=[
            "test_cumulative_refunds_cannot_exceed_paid",
            "test_service_fee_rounds_half_up",
            "test_rejection_does_not_consume_refundable_balance",
            "regression suite 5 passed",
        ],
        context=pack,
    )
    _json(run_dir / "state_v2.json", state_v2)

    stop_powercontext(settings)
    restarted = start_powercontext(settings)
    reloaded = PowerContextState(settings).load(task["task_id"])
    if not reloaded or reloaded.get("version") != state_v2["version"]:
        raise RuntimeError("PowerContext state was not restored after restart")
    pack_after = build_context(task, settings, store, state=reloaded)
    _json(run_dir / "context_after_restart.json", pack_after)
    _log(render_markdown(pack_after), run_dir / "context_after_restart.md")
    result["steps"]["state_restart"] = {
        "restarted": restarted,
        "version": reloaded.get("version"),
        "status": reloaded.get("status"),
        "restored_in_context": bool(pack_after.get("state")),
    }

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["summary"] = {
        "imported_chunks": stats["total"],
        "spec_clauses": pack["sources"]["spec_clauses"],
        "code_symbols": pack["sources"]["code_symbols"],
        "target_passed_before": before_target["passed"],
        "target_passed_after": after_target["passed"],
        "regression_passed": after_regression["passed"],
        "state_versions": [state_v1["version"], state_v2["version"]],
        "minimax_model": result["steps"]["minimax"].get("model")
        or result["steps"]["minimax"].get("meta", {}).get("model"),
    }
    _json(run_dir / "summary.json", result)
    return result

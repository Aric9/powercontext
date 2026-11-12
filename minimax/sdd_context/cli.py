"""Command line entry point for the SDD context module."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sdd_context.config import Settings
from sdd_context.context import build_context, render_markdown, save_context
from sdd_context.indexer import index_all
from sdd_context.pipeline import demo, load_sample
from sdd_context.state import PowerContextState
from sdd_context.store.seekdb_store import SeekDBStore


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(Path(args.root) if getattr(args, "root", None) else None)


def cmd_index(args: argparse.Namespace) -> int:
    settings = _settings(args)
    manifest = index_all(settings)
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(f"seekdb: {settings.seekdb_path}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    settings = _settings(args)
    sample_dir, task = load_sample(settings, args.sample) if args.sample else (None, json.loads(Path(args.task).read_text(encoding="utf-8")))
    store = SeekDBStore(settings)
    state = None
    if not args.no_state:
        try:
            state = PowerContextState(settings).load(task["task_id"])
        except Exception as exc:  # noqa: BLE001 - context still works without state
            print(f"warning: state unavailable: {exc}", file=sys.stderr)
    pack = build_context(task, settings, store, state=state)
    path = save_context(pack, settings.runs_dir)
    if args.json:
        print(json.dumps(pack, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(pack), end="")
    print(f"\ncontext saved: {path}", file=sys.stderr)
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    settings = _settings(args)
    client = PowerContextState(settings)
    if args.action == "show":
        state = client.load(args.task_id)
        print(json.dumps(state, indent=2, ensure_ascii=False) if state else "null")
        return 0
    if args.action == "search":
        print(json.dumps(client.search(args.query), indent=2, ensure_ascii=False))
        return 0
    state = client.save(
        args.task_id,
        status=args.status,
        completed=args.completed or [],
        todos=args.todos or [],
        verified=args.verified or [],
    )
    print(json.dumps(state, indent=2, ensure_ascii=False))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from sdd_context.agent import MiniMaxAgent
    from sdd_context.pipeline import load_sample

    settings = _settings(args)
    sample_dir, task = load_sample(settings, args.sample)
    store = SeekDBStore(settings)
    state = None
    try:
        state = PowerContextState(settings).load(task["task_id"])
    except Exception as exc:  # noqa: BLE001
        print(f"warning: state unavailable: {exc}", file=sys.stderr)
    pack = build_context(task, settings, store, state=state)
    files = {
        path: (settings.root / path).read_text(encoding="utf-8")
        for path in task.get("change_scope", [])
        if (settings.root / path).is_file()
    }
    run_dir = Path(args.out) if args.out else settings.runs_dir / "ask" / task["task_id"]
    result = MiniMaxAgent(settings).generate(pack, files, run_dir)
    print(json.dumps(result["meta"], indent=2, ensure_ascii=False))
    print(f"patch lines: {len(result['patch'].splitlines())}")
    print(f"run: {run_dir}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    settings = _settings(args)
    result = demo(
        settings,
        sample=args.sample,
        live=args.live,
        out=Path(args.out) if args.out else None,
    )
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    print(f"run: {result['run_dir']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _settings(args)
    from sdd_context.api import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from sdd_context.mcp_server import main

    return main(_settings(args))


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    report = {"root": str(settings.root), "data": str(settings.data_dir)}
    try:
        import pyseekdb

        report["pyseekdb"] = getattr(pyseekdb, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        report["pyseekdb"] = f"error: {exc}"
    try:
        import powercontext

        report["powercontext"] = getattr(powercontext, "__version__", "1.5.1")
    except Exception as exc:  # noqa: BLE001
        report["powercontext"] = f"error: {exc}"
    try:
        store = SeekDBStore(settings)
        report["seekdb"] = store.stats()
    except Exception as exc:  # noqa: BLE001
        report["seekdb"] = f"error: {exc}"
    try:
        report["powercontext_server"] = PowerContextState(settings).scope_id()
    except Exception as exc:  # noqa: BLE001
        report["powercontext_server"] = f"error: {exc}"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    settings = _settings(args)
    print(json.dumps(SeekDBStore(settings).stats(), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdd", description="Spec-driven development context module")
    parser.add_argument("--root", help="workspace root (defaults to cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="import specs/code/tests into seekdb")
    index.set_defaults(func=cmd_index)

    context = sub.add_parser("context", help="build a context pack")
    context.add_argument("--sample", help="sample directory name, e.g. S1_refund_cap")
    context.add_argument("--task", help="path to task.json")
    context.add_argument("--json", action="store_true")
    context.add_argument("--no-state", action="store_true")
    context.set_defaults(func=cmd_context)

    state = sub.add_parser("state", help="save or read task state in PowerContext")
    state.add_argument("action", choices=["save", "show", "search"])
    state.add_argument("--task-id")
    state.add_argument("--status", default="in_progress")
    state.add_argument("--completed", action="append")
    state.add_argument("--todos", action="append")
    state.add_argument("--verified", action="append")
    state.add_argument("--query", default="")
    state.set_defaults(func=cmd_state)

    demo_cmd = sub.add_parser("demo", help="run the full import/context/MiniMax/verify flow")
    demo_cmd.add_argument("--sample", default="S1_refund_cap")
    demo_cmd.add_argument("--live", action="store_true", help="call MiniMax instead of replaying the record")
    demo_cmd.add_argument("--out", help="run directory")
    demo_cmd.set_defaults(func=cmd_demo)

    ask = sub.add_parser("ask", help="ask MiniMax to generate the change without applying it")
    ask.add_argument("--sample", default="S1_refund_cap")
    ask.add_argument("--out", help="run directory")
    ask.set_defaults(func=cmd_ask)

    serve = sub.add_parser("serve", help="start the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8800)
    serve.set_defaults(func=cmd_serve)

    mcp = sub.add_parser("mcp", help="serve the MCP tools over stdio")
    mcp.set_defaults(func=cmd_mcp)

    doctor = sub.add_parser("doctor", help="print dependency and backend status")
    doctor.set_defaults(func=cmd_doctor)

    stats = sub.add_parser("stats", help="print seekdb collection statistics")
    stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

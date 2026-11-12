"""ReadDesk 命令行入口：init / import / search / brief / serve / doctor。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import readdesk
from readdesk.brief import write_brief
from readdesk.config import Settings
from readdesk.retrieve import QueryError, hybrid_search
from readdesk.store import Store, StoreError
from readdesk.util import normalize_tag


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="readdesk", description="ReadDesk 研读台：本地科研阅读与证据检索")
    p.add_argument("--data-dir", metavar="PATH", help="数据目录（默认 ./.readdesk，可用环境变量 READDESK_DATA_DIR）")
    p.add_argument("--version", action="version", version=f"readdesk {readdesk.__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据目录与 seekdb 集合")

    pi = sub.add_parser("import", help="导入 .md/.txt 文件或目录（递归）")
    pi.add_argument("paths", nargs="+", metavar="PATH")
    pi.add_argument("--tag", action="append", default=[], metavar="TAG", help="导入时附加标签，可重复")

    ps = sub.add_parser("search", help="混合检索证据段落")
    ps.add_argument("query", help="查询语句")
    ps.add_argument("--tag", metavar="TAG", help="按标签过滤（下推到存储层）")
    ps.add_argument("--year", type=int, metavar="YEAR", help="按年份过滤")
    ps.add_argument("--limit", type=int, default=10, metavar="N")
    ps.add_argument("--json", action="store_true", help="以 JSON 输出")

    pb = sub.add_parser("brief", help="生成 Markdown 证据简报")
    pb.add_argument("topic", help="简报主题（作为查询）")
    pb.add_argument("--out", metavar="FILE", help="输出文件（默认 <数据目录>/briefs/<slug>-<时间>.md）")
    pb.add_argument("--limit", type=int, default=24, metavar="N")

    pv = sub.add_parser("serve", help="启动本地 Web UI")
    pv.add_argument("--host", default="127.0.0.1")
    pv.add_argument("--port", type=int, default=8765)

    sub.add_parser("doctor", help="打印环境与库状态")
    return p


def _cmd_init(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings)
    created = store.initialize()
    print(f"数据目录：{settings.data_dir}")
    print(f"seekdb 路径：{settings.db_path}")
    print(f"集合：{settings.collection}（{'新建' if created else '已存在'}）")
    print(f"文档数：{store.doc_count()}，块数：{store.chunk_count()}")
    return 0


def _cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    from readdesk.parsing import iter_import_files

    roots = [Path(x).expanduser() for x in args.paths]
    missing = [str(x) for x in roots if not x.exists()]
    if missing:
        raise FileNotFoundError("路径不存在：" + "、".join(missing))
    files = iter_import_files(roots)
    if not files:
        raise FileNotFoundError("指定路径下没有找到 .md/.txt 文件（隐藏文件与 .readdesk 会被忽略）")
    store = Store(settings)
    store.initialize()
    tags = [normalize_tag(t) for t in args.tag]
    n_docs = n_chunks = 0
    for path, rel in files:
        outcome = store.import_file(path, rel, extra_tags=tags)
        n_docs += 1
        n_chunks += outcome.n_chunks
        print(f"[{outcome.status}] {outcome.rel_path} · doc_id={outcome.doc_id} · {outcome.n_chunks} 块")
    store.refresh()
    print(f"完成：处理 {n_docs} 个文件（{n_chunks} 块）；当前文档 {store.doc_count()} 篇、块 {store.chunk_count()} 个")
    return 0


def _cmd_search(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings)
    store.initialize()
    results = hybrid_search(store, args.query, tag=args.tag, year=args.year, limit=args.limit)
    if args.json:
        print(json.dumps([e.to_dict() for e in results], ensure_ascii=False, indent=2))
    else:
        for i, e in enumerate(results, start=1):
            print(f"[{i}] score={e.score:.4f} · {e.title}")
            print(f"    引用：{e.citation} · 章节：{e.section} · 命中：{'+'.join(e.matched_by)}")
            print(f"    {e.snippet}\n")
    if not results:
        print("没有找到匹配的证据段落，请更换关键词或去掉过滤条件。", file=sys.stderr)
        return 3
    return 0


def _cmd_brief(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings)
    store.initialize()
    out = Path(args.out).expanduser() if args.out else None
    path = write_brief(store, args.topic, out=out, limit=args.limit)
    print(f"简报已生成：{path}")
    return 0


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from readdesk.web.app import create_app

    store = Store(settings)
    store.initialize()
    print(f"ReadDesk Web UI：http://{args.host}:{args.port}（数据目录 {settings.data_dir}）")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    import platform

    import pyseekdb
    import seekdb

    store = Store(settings)
    store.initialize()
    print(f"Python：{platform.python_version()}（{sys.executable}）")
    print(f"pyseekdb：{getattr(pyseekdb, '__version__', '未知')} · seekdb：{getattr(seekdb, '__version__', '未知')}")
    print(f"数据目录：{settings.data_dir}")
    print(f"文档数：{store.doc_count()} · 块数：{store.chunk_count()}")
    print(f"集合：{store.collection.name} · 维度：{store.collection.dimension} · 距离：{store.collection.distance}")
    status = "ok" if store.doc_count() >= 0 else "异常"
    print(f"集合健康：{status}")
    return 0


_COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {
    "init": _cmd_init,
    "import": _cmd_import,
    "search": _cmd_search,
    "brief": _cmd_brief,
    "serve": _cmd_serve,
    "doctor": _cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口：成功返回 0，参数/数据错误返回非 0 并在 stderr 输出中文错误。"""
    args = _build_parser().parse_args(argv)
    settings = Settings.resolve(args.data_dir)
    handler = _COMMANDS[args.command]
    try:
        return handler(settings, args)
    except QueryError as exc:
        print(f"查询错误:{exc}", file=sys.stderr)
        return 2
    except (StoreError, FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断。", file=sys.stderr)
        return 130

"""Extract structured code symbols from Python sources with the standard AST."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sdd_context.models import Chunk

_SPEC_REF_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
_MAX_CODE_CHARS = 3500


def _spec_refs(text: str) -> list[str]:
    return sorted(set(_SPEC_REF_RE.findall(text)))


def _docstring(node: ast.AST) -> str:
    try:
        return ast.get_docstring(node) or ""
    except TypeError:  # pragma: no cover - defensive
        return ""


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        args = ast.unparse(node.args)
    except Exception:  # pragma: no cover - very old syntax nodes
        args = "..."
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({args}){returns}"


def _segment(source: str, node: ast.AST, limit: int = _MAX_CODE_CHARS) -> str:
    segment = ast.get_source_segment(source, node) or ""
    return segment[:limit]


def _start_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", []) or []
    line = getattr(node, "lineno", 1)
    if decorators:
        line = min(line, min(item.lineno for item in decorators))
    return int(line)


def parse_code_file(path: Path, root: Path) -> list[Chunk]:
    """Return one chunk per module, class, function and method."""

    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    chunks: list[Chunk] = []
    module_doc = _docstring(tree)
    top_names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    chunks.append(
        Chunk(
            id=f"code:{rel}::<module>",
            kind="code",
            title=rel,
            text=f"module {rel}\n{module_doc}\nexports: {', '.join(top_names)}".strip(),
            source=rel,
            start_line=1,
            end_line=max(1, len(source.splitlines())),
            symbol="<module>",
            spec_refs=_spec_refs(module_doc),
            extra={"role": "module"},
        )
    )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            chunks.extend(_parse_class(node, source, rel))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunks.append(_function_chunk(node, source, rel, qualname=node.name, role="function"))
    return chunks


def _parse_class(node: ast.ClassDef, source: str, rel: str) -> list[Chunk]:
    base_names = []
    for base in node.bases:
        try:
            base_names.append(ast.unparse(base))
        except Exception:  # pragma: no cover
            continue
    methods = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    doc = _docstring(node)
    header = f"class {node.name}({', '.join(base_names)}):"
    text = f"{header}\n{doc}\nmethods: {', '.join(methods)}".strip()
    chunks = [
        Chunk(
            id=f"code:{rel}::{node.name}",
            kind="code",
            title=node.name,
            text=text,
            source=rel,
            start_line=_start_line(node),
            end_line=int(node.end_lineno or node.lineno),
            symbol=node.name,
            signature=header,
            spec_refs=_spec_refs(text),
            extra={"role": "class"},
        )
    ]
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunks.append(
                _function_chunk(item, source, rel, qualname=f"{node.name}.{item.name}", role="method")
            )
    return chunks


def _function_chunk(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    rel: str,
    qualname: str,
    role: str,
) -> Chunk:
    signature = _signature(node)
    segment = _segment(source, node)
    doc = _docstring(node)
    text = f"{qualname}\n{signature}\n{doc}\n{segment}".strip()
    return Chunk(
        id=f"code:{rel}::{qualname}",
        kind="code",
        title=qualname,
        text=text,
        source=rel,
        start_line=_start_line(node),
        end_line=int(node.end_lineno or node.lineno),
        symbol=qualname,
        signature=signature,
        spec_refs=_spec_refs(text),
        extra={"role": role},
    )


def parse_code(files: list[Path], root: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in files:
        chunks.extend(parse_code_file(path, root))
    return chunks

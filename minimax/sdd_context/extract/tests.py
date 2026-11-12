"""Extract executable behaviour contracts from pytest modules."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sdd_context.models import Chunk

_SPEC_REF_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")


def _assertions(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            try:
                found.append(ast.unparse(child.test))
            except Exception:  # pragma: no cover
                found.append("<assert>")
        elif isinstance(child, ast.With):
            for item in child.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    try:
                        rendered = ast.unparse(call.func)
                    except Exception:  # pragma: no cover
                        continue
                    if rendered.endswith("raises"):
                        found.append(f"{rendered}({ast.unparse(call.args[0]) if call.args else ''})")
    return found


def _test_id(rel: str, name: str) -> str:
    return f"{rel}::{name}"


def parse_test_file(path: Path, root: Path) -> list[Chunk]:
    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    chunks: list[Chunk] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        segment = ast.get_source_segment(source, node) or ""
        doc = ast.get_docstring(node) or ""
        asserts = _assertions(node)
        refs = sorted(set(_SPEC_REF_RE.findall(segment)) | set(_SPEC_REF_RE.findall(doc)))
        readable = node.name.removeprefix("test_").replace("_", " ")
        text = (
            f"test {readable}\n{node.name}\n{doc}\n"
            f"asserts:\n" + "\n".join(f"- {item}" for item in asserts[:12]) + f"\n\n{segment[:2500]}"
        )
        chunks.append(
            Chunk(
                id=f"test:{_test_id(rel, node.name)}",
                kind="test",
                title=node.name,
                text=text,
                source=rel,
                start_line=int(node.lineno),
                end_line=int(node.end_lineno or node.lineno),
                symbol=_test_id(rel, node.name),
                spec_refs=refs,
                extra={"assert_count": len(asserts)},
            )
        )
    return chunks


def parse_tests(files: list[Path], root: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in files:
        chunks.extend(parse_test_file(path, root))
    return chunks

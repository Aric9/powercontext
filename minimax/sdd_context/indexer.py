"""Import specifications, code and tests into seekdb."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sdd_context.config import Settings
from sdd_context.extract.code import parse_code
from sdd_context.extract.specs import parse_specs
from sdd_context.extract.tests import parse_tests
from sdd_context.models import Chunk
from sdd_context.store.seekdb_store import SeekDBStore


def discover(settings: Settings) -> dict[str, list[Path]]:
    business = settings.business_dir
    code_files = [
        path
        for path in sorted(business.rglob("*.py"))
        if "tests" not in path.parts and "__pycache__" not in path.parts
    ]
    return {
        "specs": sorted((business / "specs").glob("*.md")),
        "code": code_files,
        "tests": sorted((business / "tests").glob("test_*.py")),
    }


def collect_chunks(settings: Settings) -> tuple[list[Chunk], dict[str, list[Path]]]:
    files = discover(settings)
    chunks: list[Chunk] = []
    chunks.extend(parse_specs(settings.business_dir / "specs", settings.root))
    chunks.extend(parse_code(files["code"], settings.root))
    chunks.extend(parse_tests(files["tests"], settings.root))
    return chunks, files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def index_all(settings: Settings, store: SeekDBStore | None = None, reset: bool = True) -> dict[str, Any]:
    """Parse the business workspace and (re)build the seekdb index."""

    store = store or SeekDBStore(settings)
    chunks, files = collect_chunks(settings)
    if reset:
        store.reset()
    inserted = store.add_chunks(chunks)

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "root": str(settings.root),
        "seekdb_path": str(settings.seekdb_path),
        "collection": "sdd_chunks",
        "embedding": {"name": "sdd-hash-embedding", "dimension": settings.embedding_dimension},
        "counts": {
            "spec": sum(1 for chunk in chunks if chunk.kind == "spec"),
            "code": sum(1 for chunk in chunks if chunk.kind == "code"),
            "test": sum(1 for chunk in chunks if chunk.kind == "test"),
            "total": inserted,
        },
        "files": {
            name: {
                "count": len(paths),
                "sha256": {settings.rel(path): _sha256(path) for path in paths},
            }
            for name, paths in files.items()
        },
    }
    manifest_path = settings.data_dir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest

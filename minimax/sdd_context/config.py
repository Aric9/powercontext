"""Runtime configuration for the SDD context module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    """All paths and endpoints resolved from environment variables."""

    root: Path
    data_dir: Path
    seekdb_path: Path
    runs_dir: Path
    embedding_dimension: int = 512
    embedding_host: str = "127.0.0.1"
    embedding_port: int = 8799
    powercontext_url: str = "http://127.0.0.1:8791"
    powercontext_scope: str = "sdd-refund"
    powercontext_home: Path = field(default_factory=Path)
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M3"
    minimax_key_env: str = "MINIMAX_API_KEY"

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = Path(root or _env("SDD_ROOT", os.getcwd())).resolve()
        data_dir = Path(_env("SDD_DATA_DIR", str(root / ".sdd"))).resolve()
        return cls(
            root=root,
            data_dir=data_dir,
            seekdb_path=Path(_env("SDD_SEEKDB_PATH", str(data_dir / "seekdb"))),
            runs_dir=Path(_env("SDD_RUNS_DIR", str(root / "runs"))),
            embedding_dimension=_env_int("SDD_EMBED_DIM", 512),
            embedding_host=_env("SDD_EMBED_HOST", "127.0.0.1"),
            embedding_port=_env_int("SDD_EMBED_PORT", 8799),
            powercontext_url=_env("POWERCONTEXT_URL", "http://127.0.0.1:8791"),
            powercontext_scope=_env("SDD_POWERCONTEXT_SCOPE", "sdd-refund"),
            powercontext_home=Path(_env("POWERCONTEXT_HOME", str(data_dir / "powercontext"))),
            minimax_base_url=_env("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
            minimax_model=_env("MINIMAX_MODEL", "MiniMax-M3"),
        )

    @property
    def business_dir(self) -> Path:
        return self.root / "business"

    @property
    def specs_dir(self) -> Path:
        return self.business_dir / "specs"

    def rel(self, path: Path | str) -> str:
        """Return a workspace-relative POSIX path."""

        target = Path(path).resolve()
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            return target.as_posix()

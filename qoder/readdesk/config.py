"""数据目录与集合配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(".readdesk")
DATABASE_NAME = "readdesk"
COLLECTION_NAME = "readdesk_chunks"
EMBEDDING_DIM = 512


@dataclass(frozen=True)
class Settings:
    """全局运行配置：只依赖 data_dir，其余路径按约定派生。"""

    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR.resolve())
    database: str = DATABASE_NAME
    collection: str = COLLECTION_NAME
    dimension: int = EMBEDDING_DIM

    @classmethod
    def resolve(cls, data_dir: str | os.PathLike[str] | None = None) -> "Settings":
        """优先级：显式参数 > 环境变量 READDESK_DATA_DIR > 默认 ./.readdesk。"""
        if data_dir is None:
            data_dir = os.environ.get("READDESK_DATA_DIR") or DEFAULT_DATA_DIR
        return cls(data_dir=Path(data_dir).expanduser().resolve())

    @property
    def db_path(self) -> Path:
        """嵌入式 seekdb 的数据目录。"""
        return self.data_dir / "seekdb"

    @property
    def briefs_dir(self) -> Path:
        """简报输出目录。"""
        return self.data_dir / "briefs"

    def ensure(self) -> None:
        """创建数据目录结构。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.briefs_dir.mkdir(parents=True, exist_ok=True)

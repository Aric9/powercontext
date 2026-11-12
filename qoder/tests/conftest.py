"""共享测试夹具：基于 samples/library 的临时数据目录，绝不污染仓库 .readdesk。

注意：同一进程内嵌入式 seekdb 只允许打开一个实例（后开的 path 会附着到先开实例），
因此“空库/新建集合”这类场景一律用 run_cli 子进程覆盖。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from readdesk.config import Settings
from readdesk.parsing import iter_import_files
from readdesk.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "samples" / "library"


def run_cli(*argv: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    """在子进程里执行 `python -m readdesk ...`（独立 seekdb 实例）。"""
    cmd = [sys.executable, "-m", "readdesk", "--data-dir", str(data_dir), *argv]
    env = {k: v for k, v in os.environ.items() if k != "READDESK_DATA_DIR"}
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    """示例库目录。"""
    assert SAMPLES_DIR.is_dir(), f"缺少示例数据：{SAMPLES_DIR}"
    return SAMPLES_DIR


@pytest.fixture(scope="session")
def lib_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """整个测试会话共享的一次性完整库（临时目录）。"""
    return Settings(data_dir=tmp_path_factory.mktemp("readdesk-once"))


@pytest.fixture(scope="session")
def lib_store(lib_settings: Settings) -> Store:
    """初始化并导入全部示例文档后的 Store。"""
    store = Store(lib_settings)
    store.initialize()
    for path, rel in iter_import_files([SAMPLES_DIR]):
        store.import_file(path, rel)
    store.refresh()
    return store

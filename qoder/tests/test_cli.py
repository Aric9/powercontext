"""CLI 端到端测试：使用独立临时数据目录与子进程，避免污染仓库。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "samples" / "library"


def run_cli(*argv: str, data_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    """执行 `python -m readdesk ...`，返回 CompletedProcess。"""
    cmd = [sys.executable, "-m", "readdesk"]
    if data_dir is not None:
        cmd += ["--data-dir", str(data_dir)]
    cmd += list(argv)
    env = {k: v for k, v in os.environ.items() if k != "READDESK_DATA_DIR"}
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_cli_full_workflow(tmp_path: Path) -> None:
    d = tmp_path / "cli-data"
    r = run_cli("init", data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "文档数：0" in r.stdout

    r = run_cli("import", str(SAMPLES_DIR), data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "当前文档 6 篇" in r.stdout

    # 幂等导入
    r = run_cli("import", str(SAMPLES_DIR), data_dir=d)
    assert r.returncode == 0
    assert "[skipped]" in r.stdout

    r = run_cli("search", "混合检索 怎么融合 BM25 和向量结果", "--json", "--limit", "3", data_dir=d)
    assert r.returncode == 0, r.stderr
    results = json.loads(r.stdout)
    assert results[0]["path"] == "01-hybrid-retrieval.md"
    assert "RRF" in results[0]["snippet"]
    assert re.fullmatch(r"[\w.\-/]+#L\d+-L\d+", results[0]["citation"])

    r = run_cli("search", "分块 重叠 比例", "--limit", "3", data_dir=d)
    assert r.returncode == 0 and "02-chunking-strategies.md" in r.stdout

    r = run_cli("brief", "混合检索 引用", "--out", str(tmp_path / "b.md"), data_dir=d)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "b.md").read_text(encoding="utf-8").count("#L") >= 3

    r = run_cli("doctor", data_dir=d)
    assert r.returncode == 0, r.stderr
    assert "pyseekdb" in r.stdout and "文档数：6" in r.stdout


def test_cli_errors(tmp_path: Path) -> None:
    d = tmp_path / "cli-err"
    r = run_cli("import", str(tmp_path / "not-exist.md"), data_dir=d)
    assert r.returncode != 0 and "不存在" in r.stderr

    r = run_cli("search", "", data_dir=d)
    assert r.returncode != 0 and "空" in r.stderr

    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_cli("import", str(empty), data_dir=d)
    assert r.returncode != 0 and "没有找到" in r.stderr

    run_cli("init", data_dir=d)
    r = run_cli("search", "任何词", data_dir=d)  # 已初始化但没有文档
    assert r.returncode != 0 and "还没有" in r.stderr

"""SPEC 第 4 节的 9 项一键验收：全部通过返回 0，任一失败返回 1。

用法：.venv/bin/python scripts/verify.py （使用临时数据目录，不污染仓库）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "samples" / "library"
PY = sys.executable

RESULTS: list[tuple[str, bool, str]] = []
_TMP = Path(tempfile.mkdtemp(prefix="readdesk-verify-"))
_DATA = _TMP / "data"


def check(n: int, name: str, ok: bool, detail: str = "") -> None:
    """记录并打印一项验收结果。"""
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {n}. {name}" + (f" — {detail}" if detail and not ok else ""))


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    """在验收临时数据目录上执行一次 CLI 命令。"""
    cmd = [PY, "-m", "readdesk", "--data-dir", str(_DATA), *argv]
    env = {k: v for k, v in os.environ.items() if k != "READDESK_DATA_DIR"}
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600)


def main() -> int:
    try:
        _verify()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n合计：{len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
    if failed:
        print("失败项：" + "；".join(failed))
        return 1
    return 0


def _verify() -> None:
    # 1. init 成功且集合为空
    r = run_cli("init")
    ok1 = r.returncode == 0 and "文档数：0" in r.stdout and "块数：0" in r.stdout
    check(1, "init 成功，集合为空", ok1, r.stderr.strip())

    # 2. 导入 samples/library：6 篇文档、≥24 块
    r = run_cli("import", str(SAMPLES_DIR))
    ok2 = r.returncode == 0 and "当前文档 6 篇" in r.stdout
    m = re.search(r"块 (\d+) 个", r.stdout)
    ok2 = ok2 and m is not None and int(m.group(1)) >= 24
    check(2, "导入 6 篇文档且 ≥24 块", ok2, (r.stdout + r.stderr).strip())

    # 3. 混合检索 Top1 为 01 且 snippet 含 RRF
    r = run_cli("search", "混合检索 怎么融合 BM25 和向量结果", "--json", "--limit", "3")
    top1_ok = False
    snippet_has_rrf = False
    try:
        results = json.loads(r.stdout)
        top1_ok = bool(results) and results[0]["path"] == "01-hybrid-retrieval.md"
        snippet_has_rrf = bool(results) and "RRF" in results[0]["snippet"]
    except Exception:
        pass
    check(3, "「混合检索…」Top1=01 且 snippet 含 RRF", r.returncode == 0 and top1_ok and snippet_has_rrf,
          (r.stdout[:200] + r.stderr).strip())

    # 4. 「分块 重叠 比例」Top3 出现 02
    r = run_cli("search", "分块 重叠 比例", "--json", "--limit", "3")
    paths: list[str] = []
    try:
        paths = [x["path"] for x in json.loads(r.stdout)]
    except Exception:
        pass
    check(4, "「分块 重叠 比例」Top3 含 02-chunking-strategies.md", "02-chunking-strategies.md" in paths,
          str(paths))

    # 5. --tag evaluation 过滤后所有文档 tags 含 evaluation
    r = run_cli("search", "评测 哈希 embedding", "--tag", "evaluation", "--json", "--limit", "10")
    try:
        rows = json.loads(r.stdout)
    except Exception:
        rows = []
    ok5 = r.returncode == 0 and bool(rows) and all("evaluation" in row["tags"] for row in rows)
    check(5, "--tag evaluation 过滤全部命中 evaluation", ok5, str(rows)[:200])

    # 6. brief 含 ≥3 个不同 #L 引用与「覆盖统计」
    brief_file = _TMP / "brief.md"
    r = run_cli("brief", "混合检索 分块 引用", "--out", str(brief_file))
    md = brief_file.read_text(encoding="utf-8") if brief_file.exists() else ""
    cites = set(re.findall(r"[\w.\-/]+\.md#L\d+-L\d+", md))
    check(6, "brief 含 ≥3 个不同 #L 引用与覆盖统计", r.returncode == 0 and len(cites) >= 3 and "覆盖统计" in md,
          f"引用数={len(cites)}, rc={r.returncode}")

    # 7. 重复导入幂等
    before = _stats()
    r = run_cli("import", str(SAMPLES_DIR))
    after = _stats()
    check(7, "重复导入文档数与块数不变", r.returncode == 0 and before is not None and before == after and after is not None,
          f"before={before}, after={after}")

    # 8. Web /healthz 返回 200 且 docs=6
    ok8, detail8 = False, ""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from fastapi.testclient import TestClient

        from readdesk.config import Settings
        from readdesk.web.app import create_app

        client = TestClient(create_app(Settings.resolve(_DATA)))
        res = client.get("/healthz")
        body = res.json()
        ok8 = res.status_code == 200 and body["status"] == "ok" and body["docs"] == 6
        detail8 = f"{res.status_code} {res.text[:100]}"
    except Exception as exc:  # pragma: no cover
        detail8 = repr(exc)
    check(8, "Web GET /healthz 200 且 docs=6", ok8, detail8)

    # 9. 全量 pytest -q 通过
    r = subprocess.run([PY, "-m", "pytest", "-q"], cwd=REPO_ROOT,
                       env={**os.environ, "READDESK_DATA_DIR": str(_TMP / "pytest-data")},
                       capture_output=True, text=True, timeout=1800)
    check(9, "pytest -q 全量通过", r.returncode == 0, r.stdout[-400:] + r.stderr[-200:])


def _stats() -> tuple[int, int] | None:
    """通过 doctor 输出读取 (文档数, 块数)。"""
    r = run_cli("doctor")
    m = re.search(r"文档数：(\d+) · 块数：(\d+)", r.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


if __name__ == "__main__":
    sys.exit(main())

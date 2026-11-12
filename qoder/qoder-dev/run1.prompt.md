你是 ReadDesk 项目的主程，请独立完成一个可运行、可验收的 Python 项目。

## 输入
- 先完整阅读仓库根目录的 `SPEC.md`（产品与技术规格，验收标准以它为准）。
- `samples/library/` 下 6 篇 Markdown 是唯一示例数据，**不要修改**。
- 仓库已提供 `.venv`，其中已安装 pyseekdb、seekdb、fastapi、uvicorn、jinja2、pytest、jieba、httpx。请使用 `.venv/bin/python` 运行，不要 pip install 新依赖，不要联网。

## 任务
从零实现 SPEC.md 中的 ReadDesk v1：
1. `readdesk/` 包：配置、Markdown/front matter 解析与分块、本地 512 维哈希 embedding、seekdb 存储与混合检索（dense + `$contains` 全文 + tag/year 下推过滤 + RRF）、证据简报生成、CLI、FastAPI Web UI。
2. `tests/`：覆盖解析、分块、检索、CLI、Web API 的 pytest。
3. `scripts/verify.py`：实现 SPEC 第 4 节的 9 项验收，输出 PASS/FAIL，全部通过返回 0。
4. `README.md`：安装、初始化、导入、检索、简报、Web、验收、目录结构、已知限制。
5. `requirements.txt`：固定版本。

## 已知环境坑（务必按此实现，少走弯路）
- 嵌入式 seekdb：`pyseekdb.Client(path=str(db_path), database="readdesk")` 之前，需要 `pyseekdb.AdminClient(path=str(db_path))` 并 `create_database("readdesk")`（先 `list_databases()` 判断）。
- 建集合时 **不要** 传自定义 embedding_function（重启后无法解析该函数名会导致集合打不开）。用
  `client.create_collection(name="readdesk_chunks", embedding_function=None, configuration=pyseekdb.HNSWConfiguration(dimension=512, distance="cosine"))`，
  写入时显式传 `embeddings=[...]`，查询时传 `query_embeddings=[...]`。
- 全文过滤：`collection.get(where_document={"$contains": term}, where={...}, limit=N)`；向量查询：`collection.query(query_embeddings=[vec], n_results=N, where={...})`。
- seekdb 的 metadata 只放扁平标量。tags 用 `tags_csv` 字符串保存展示，同时为每个 tag 生成 `tag_<safe>` 布尔字段用于 `where` 过滤；year 存 int。
- `collection.get(where={"kind": ...}, include=[])` 可用于统计数量。
- 所有路径用 `pathlib.Path`，数据目录默认 `./.readdesk`，支持 `--data-dir` / 环境变量 `READDESK_DATA_DIR`。

## 质量要求
- Python 3.12，类型标注，函数 docstring 简短，行宽 ≤120。
- 错误处理：文件不存在、空查询、空库、无结果时给中文提示并非 0 退出（Web 返回 4xx/空状态页）。
- Web UI 中文、浅色、响应式、**不引用任何外部 CDN/字体**，静态资源本地化；搜索高亮用 `<mark>`。
- 测试与 verify 必须使用临时目录，不污染仓库；重复导入必须幂等（同路径同 sha256 不新增块）。
- 不要写死 `/home/...` 等绝对路径；不要提交 git；不要修改 `SPEC.md`、`samples/`。

## 工作方式
先给出简要实现计划，然后直接写代码。完成后必须实际运行：
- `.venv/bin/python -m pytest -q`
- `.venv/bin/python scripts/verify.py`
若失败，自行修复并重复，直到两者都通过。最后用中文汇报：新增文件、测试与验收结果、已知限制。

# ReadDesk 研读台 — 产品与技术规格（v1）

## 1. 一句话

ReadDesk 是一个**本地优先的科研阅读与证据检索工作台**：把散落的论文笔记（Markdown/TXT）导入本地 seekdb 库，
用混合检索找到可引用的原句，并一键生成带 `文件#行号` 引用的证据简报。

## 2. 用户与痛点

目标用户是需要大量阅读文献、写综述/开题/论文的研究生与研发人员。痛点：

1. 论文、摘录、笔记分散在多个文件夹，写作时找不到原句出自哪篇、哪一节、哪一行。
2. 通用搜索只返回文档链接，不返回可引用的证据段落，也不给行号。
3. 笔记与原文脱节，读过的内容无法在下次写作时被快速召回。
4. 云端工具上传未发表材料有隐私顾虑。

ReadDesk 的取舍：**数据不出本机**、**引用必须可回溯到行号**、**不依赖在线模型**。

## 3. 功能需求（必须全部实现）

### 3.1 CLI

入口统一为 `python -m readdesk <command>`。

| 命令 | 行为 |
| --- | --- |
| `init [--data-dir PATH]` | 初始化数据目录与 seekdb 集合，打印路径与集合统计 |
| `import PATH... [--tag TAG]` | 导入 `.md` / `.txt` 文件或目录（递归）；解析 front matter；按标题分块；写入 seekdb；重复导入同一文件应幂等（按内容哈希去重或覆盖同一文档的块） |
| `search QUERY [--tag TAG] [--year YEAR] [--limit N] [--json]` | 混合检索，输出按分数排序的证据段落，每条包含：`doc_id`、标题、`citation`（`相对路径#L起-L止`）、`section`、`score`、`snippet`（命中词高亮可用 `**`） |
| `brief TOPIC [--out FILE] [--limit N]` | 生成 Markdown 证据简报：按文档分组，每组列出 2–4 条证据（含引用与原文），末尾给出「覆盖统计」与「使用说明」；默认写到 `briefs/<slug>-<时间>.md` |
| `serve [--host 127.0.0.1] [--port 8765]` | 启动本地 Web UI |
| `doctor` | 打印 Python/seekdb 版本、数据目录、文档数、块数、集合健康状态 |

所有命令成功返回 0，参数/数据错误返回非 0，并在 stderr 给出可读错误。

### 3.2 导入与分块

- 支持 UTF-8 的 `.md`、`.txt`；忽略隐藏文件与 `.readdesk` 目录。
- front matter 为文件开头的 `---` 块，支持 `title`、`authors`（`[a, b]` 或逗号分隔）、`year`、`tags`（同上）、`venue`、`doi`；缺省时用文件名（去扩展名）作为标题。
- 分块优先按 Markdown 标题（`#`/`##`/`###`）切分；无标题或超长段落按约 600–900 字符切分，块间重叠不超过 120 字符；每块记录 `start_line`/`end_line`（1-based，闭区间）。
- 每块文本包含：章节标题 + 正文；文档级元数据（title/year/tags）写入块 metadata。
- 数据模型至少包含：`documents`（doc_id、path、title、authors、year、tags、sha256、imported_at）与 `chunks`（chunk_id、doc_id、section、text、start_line、end_line、embedding）。

### 3.3 检索

- 使用 seekdb 嵌入式（`pyseekdb.Client(path=...)`，集合名 `readdesk_chunks`，HNSW cosine，维度 512）。
- 向量由**本地确定性哈希 embedding**生成（不得调用在线模型）：中英文分词 + 字符 n-gram + 带符号特征哈希，L2 归一化，512 维；实现放在 `readdesk/embedding.py`，需可复现（同文本同向量）。
- 混合检索必须同时使用：
  1. dense：`collection.query(query_embeddings=..., where=...)`；
  2. 全文：`collection.get(where_document={"$contains": term}, ...)` 或等价 seekdb 全文过滤；
  3. 元数据过滤：`--tag`、`--year` 必须下推到 seekdb 的 `where`，不能只在 Python 里过滤。
- 融合使用 RRF（k=60），并对标题/章节命中给小幅加权；返回 `score` 与 `matched_by`（dense/fts/metadata）。
- 查询词提取：英文标识符、数字、连字符术语、中文 2–4 字词（可用 jieba，若未安装则退化为字符 bigram）。

### 3.4 Web UI

- FastAPI + Jinja2（或原生 HTML），`GET /` 首页：库统计、搜索框、tag/year 过滤、示例查询。
- `GET /search?q=...&tag=...&year=...`：结果卡片，显示标题、章节、引用、分数、带高亮的 snippet；引用可一键复制。
- `POST /brief`：根据当前查询生成简报并返回下载链接（或直接展示 Markdown 渲染）。
- `GET /doc/{doc_id}`：查看文档元数据与块列表（引用行号）。
- 页面为中文，浅色主题，响应式，无外部 CDN 依赖（离线可用），静态资源放 `readdesk/web/static/`，模板放 `readdesk/web/templates/`。
- 提供 `GET /healthz` 返回 `{"status":"ok","docs":N,"chunks":M}`。

### 3.5 工程与交付

- 目录结构：
  ```text
  readdesk/            # Python 包（cli.py, config.py, embedding.py, store.py, retrieve.py, brief.py, web/）
  samples/library/     # 6 篇示例笔记（已提供，不要修改）
  tests/               # pytest：解析、分块、检索、CLI、API
  scripts/verify.py    # 一键验收脚本
  requirements.txt     # 固定版本
  README.md            # 安装、初始化、导入、检索、Web、验收、已知限制
  ```
- 依赖：`pyseekdb==1.4.0.post1`、`seekdb==1.4.0.dev2`、`fastapi`、`uvicorn`、`jinja2`、`python-multipart`、`pytest`、`jieba`；Python 3.12。
- 测试使用临时目录与独立 seekdb 路径，不污染 `.readdesk`；`pytest -q` 全绿。
- 不得引入在线 API Key、不得上传用户文件、不得写死绝对路径。
- 代码通过 `ruff` 风格（行宽 120），公共函数有类型标注与简短 docstring。

## 4. 验收脚本（scripts/verify.py）

必须能在干净数据目录上运行并输出 PASS/FAIL：

1. `init` 成功，集合为空；
2. 导入 `samples/library`，得到 6 篇文档、≥24 个块；
3. 查询「混合检索 怎么融合 BM25 和向量结果」：Top1 引用的文档为 `01-hybrid-retrieval.md`，且 snippet 含 `RRF`；
4. 查询「分块 重叠 比例」：Top3 内出现 `02-chunking-strategies.md`；
5. `--tag evaluation` 过滤后所有结果的文档 tags 含 `evaluation`；
6. `brief` 生成的文件包含至少 3 个不同的 `#L` 引用与「覆盖统计」小节；
7. 重复导入保持文档数与块数不变（幂等）；
8. Web `GET /healthz` 返回 200 且 docs=6；
9. 全量 `pytest -q` 通过。

## 5. 示例数据

`samples/library/` 下 6 篇 Markdown 已提供（含 front matter、章节、数字结论），是唯一数据源；
测试与验收必须基于这些文件，不得内置固定答案。

## 6. 非目标

- 不做 PDF 解析、不做在线抓取、不做多用户/权限、不做云同步。
- 不追求神经语义 embedding 的召回上限；优先离线、确定性、可复现。

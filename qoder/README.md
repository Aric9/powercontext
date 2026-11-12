# ReadDesk 研读台 v1

本地优先的科研阅读与证据检索工作台：把散落的论文笔记（Markdown/TXT）导入本地 seekdb，
用混合检索（向量 + 全文 + 元数据过滤，RRF 融合）找到可引用的原句，并一键生成带 `文件#行号` 引用的证据简报。
**数据不出本机，不调用任何在线模型。**

- 项目网站：<https://readdesk-ekfkanuc31a.qoder.zone>
- 代码仓库：<https://github.com/Aric9/powercontext/tree/qoder/readdesk/qoder>
- Qoder 开发记录：[`qoder-dev/README.md`](qoder-dev/README.md)（含原始会话 `run1.jsonl`、提示词、转录与 credits）

## 1. 安装

要求 **Linux x86_64 + Python 3.12**（seekdb 仅提供 manylinux wheel；本机不附带 `.venv`）：

```bash
git clone -b qoder/readdesk https://github.com/Aric9/powercontext.git
cd powercontext/qoder
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

所有命令都可通过 `python -m readdesk` 运行（下文以 `.venv/bin/python` 为例）。

## 2. 初始化

```bash
.venv/bin/python -m readdesk init
```

创建数据目录（默认 `./.readdesk`，可用 `--data-dir PATH` 或环境变量 `READDESK_DATA_DIR` 覆盖），
初始化嵌入式 seekdb 数据库 `readdesk` 与集合 `readdesk_chunks`（HNSW、cosine、512 维），并打印路径与集合统计。

## 3. 导入笔记

```bash
.venv/bin/python -m readdesk import samples/library            # 目录递归导入
.venv/bin/python -m readdesk import notes.md papers/ --tag 组会   # 附加标签，可重复
```

- 支持 UTF-8 的 `.md`/`.txt`，忽略隐藏文件与 `.readdesk` 目录。
- `samples/library/` 是演示用的**合成笔记**（非真实论文），用于复现检索与验收；替换成自己的笔记即可。
- 解析文件开头的 `---` front matter：`title`、`authors`、`year`、`tags`、`venue`、`doi`；缺省标题取文件名。
- 按 `#`/`##`/`###` 标题分块，超长章节按段落切到 600–900 字符、重叠 ≤120 字符；每块记录 1-based 闭区间起止行号。
- **幂等**：同一文件内容（sha256）不变时重复导入不会新增块（输出 `[skipped]`）；内容变化则整体替换该文档的块（`[updated]`）。

## 4. 检索

```bash
.venv/bin/python -m readdesk search "混合检索 怎么融合 BM25 和向量结果"
.venv/bin/python -m readdesk search "分块 重叠 比例" --limit 5
.venv/bin/python -m readdesk search "评测" --tag evaluation --year 2025
.venv/bin/python -m readdesk search "RRF k=60" --json
```

混合检索同时使用三路并融合（RRF，k=60，标题/章节命中加 0.02/0.01）：

1. **dense**：本地确定性哈希 embedding（512 维、L2 归一化，见 `readdesk/embedding.py`）做向量近邻；
2. **全文**：对每个检索词用 seekdb `where_document={"$contains": term}` 过滤；
3. **元数据**：`--tag`/`--year` 下推到 seekdb `where`，不在 Python 侧过滤。

每条结果包含 `doc_id`、标题、`citation`（`相对路径#L起-L止`）、章节、`score`、命中来源 `matched_by`
与带 `**` 高亮的 snippet。参数/数据错误（空查询、路径不存在、空库、无结果）返回非 0 并在 stderr 给出中文提示。

## 5. 证据简报

```bash
.venv/bin/python -m readdesk brief "混合检索 分块 引用"
.venv/bin/python -m readdesk brief "本地优先 RAG" --out report.md --limit 16
```

按文档分组列出每组 2–4 条证据（引用 + 原文），末尾附「覆盖统计」与「使用说明」。
默认输出到 `<数据目录>/briefs/<slug>-<时间戳>.md`。

## 6. Web UI

```bash
.venv/bin/python -m readdesk serve --host 127.0.0.1 --port 8765
```

打开 http://127.0.0.1:8765 ：

- `/` 首页：库统计、检索表单（tag/year 过滤）、示例查询、文档列表；
- `/search?q=…`：结果卡片（标题、章节、引用一键复制、分数、`<mark>` 高亮片段），无结果显示空状态页；
- `POST /brief`：按当前查询生成简报并在线渲染，附下载链接；
- `/doc/{doc_id}`：文档元数据与全部证据块（含行号引用）;
- `/healthz`：`{"status":"ok","docs":N,"chunks":M}`。

界面为中文浅色主题、响应式；模板与静态资源都在 `readdesk/web/` 内，**不引用任何外部 CDN/字体，完全离线可用**。

## 7. 其它命令

- `doctor`：打印 Python/pyseekdb/seekdb 版本、数据目录、文档数、块数与集合健康状态。
- `--data-dir` 为全局参数，放在子命令之前。

## 8. 验收

```bash
.venv/bin/python -m pytest -q        # 42 项单元测试
.venv/bin/python scripts/verify.py   # SPEC 第 4 节 9 项验收，全部 PASS 返回 0
```

`verify.py` 在系统临时目录创建一次性数据目录跑完 9 项检查（最后会再跑一遍全量 pytest），结束后自动清理。

## 9. 目录结构

```text
readdesk/
  cli.py config.py embedding.py parsing.py store.py retrieve.py brief.py util.py
  web/app.py web/views.py web/markdown.py web/templates/ web/static/
samples/library/     # 6 篇示例笔记（只读数据源）
tests/               # pytest：解析、分块、embedding、存储、检索、CLI、Web
scripts/verify.py    # 一键验收
requirements.txt README.md SPEC.md
```

## 10. 已知限制

1. **哈希 embedding 非神经语义模型**：同义改写（不含共同词形）召回有限；这是 SPEC 明确的离线确定性取舍。
2. **全文命中用 `$contains` 子串**而非 BM25 打分；词序/形态变化（如复数、英文时态）需靠 dense 路补足。
3. 同一进程内嵌入式 seekdb 只维持一个实例，多数据目录需分进程使用（测试与 verify 均以子进程隔离）。
4. 写入后必须调用 `refresh_index()`（import/verify 已自动处理）；绕过 CLI 直接写库时dense 查询可能短暂看不到新块。
5. `doc_id` 由导入相对路径哈希得到：同一文件以不同相对路径导入会视为两篇文档（内容仍按 sha256 幂等）。
6. 简报生成不含摘要模型，只做“检索 + 组织 + 引用”；`venue/doi` 等字段仅存储展示，未参与过滤。

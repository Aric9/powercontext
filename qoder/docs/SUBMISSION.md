# Qoder 赛道提交材料

## 1. 提测信息

| 字段 | 值 |
| --- | --- |
| 项目网站 | <https://readdesk-ekfkanuc31a.qoder.zone>（Qoder Sites，公开访问） |
| 代码仓库 | <https://github.com/Aric9/powercontext/tree/qoder/readdesk/qoder> |
| Branch | `qoder/readdesk` |
| Commit | 以提测弹窗填写为准（`qoder/readdesk` 分支最新提交） |
| 运行说明 | [`README.md`](../README.md) |
| 验证样例 | [`samples/library/`](../samples/library)、 [`scripts/verify.py`](../scripts/verify.py)、 [`qoder-dev/verify.run1.log`](../qoder-dev/verify.run1.log) |
| Qoder 开发记录 | [`qoder-dev/README.md`](../qoder-dev/README.md)、 [`run1.jsonl`](../qoder-dev/run1.jsonl)、 [`TRANSCRIPT.md`](../qoder-dev/TRANSCRIPT.md) |

## 2. 基础验收对照

| 验收项 | 证据 |
| --- | --- |
| 网站可访问（未登录可打开） | Qoder Sites 已发布并设为 `public`；`curl` 未带凭据返回 200 且包含页面内容 |
| 项目能运行 | 按 README 安装 → `init` → `import samples/library` → `search` / `serve`；`pytest -q` 42 passed，`scripts/verify.py` 9/9 PASS |
| Qoder 开发可核对 | Qoder CLI CN `qoderclicn` headless 会话：71 回合、25.7 分钟、20.8586 credits；`run1.jsonl` 记录每次工具调用，`git show --stat 102af19` 对应 41 个文件 / 2 841 行 |
| 技术结合有效 | 运行时全部检索由嵌入式 seekdb 执行：HNSW cosine 512 维向量 + `$contains` 全文 + tag/year 标量下推，RRF 融合；`search --json` 返回真实 `citation` 与 `matched_by` |

## 3. 网站内容

网站包含：项目简介与目标用户、三个痛点、三步工作流、真实 UI 截图（首页 / 搜索结果 / 文档详情）、
静态交互演示（演示数据由本地 ReadDesk 实际检索导出）、技术实现、Qoder 开发记录、9 项验收与快速开始。

- 站点源码：[`site/`](../site)
- 截图来源：本地 `readdesk serve` 实际页面，Playwright 在 1440×960 视口截取
- 演示数据：[`site/assets/demo.json`](../site/assets/demo.json)，由 `readdesk search --json` 生成

## 4. 复现命令

```bash
cd qoder
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m readdesk init
python -m readdesk import samples/library
python -m readdesk search "混合检索 怎么融合 BM25 和向量结果"
python -m readdesk brief "混合检索 分块 引用"
python -m readdesk serve --port 8765
python -m pytest -q
python scripts/verify.py
```

## 5. 已知限制

见 [`README.md`](../README.md) 第 10 节：哈希 embedding 非神经语义、`$contains` 非 BM25 打分、
单进程单 seekdb 实例、写入后需 `refresh_index()`、doc_id 依赖相对路径等。

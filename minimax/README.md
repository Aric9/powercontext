# 规有所依：面向软件持续演进的 SDD 上下文管理系统（MiniMax 赛道）

本目录是第六届 OceanBase 数据库大赛 MiniMax 赛道的参赛作品。它把「规格（Spec）—代码（Code）—测试（Test）—任务状态（State）」
组织成可检索、可引用、可复现的工程上下文，交给 MiniMax 完成一次真实的代码修改，并用 seekdb 与 PowerContext 分别承担
工程资料检索与任务状态复用。

- 赛题要求：导入 → 选材 → MiniMax 改码 → 跑测试 → 存状态 → 重启复用，六项必做全部通过。
- 业务工程：`business/` 下的退款服务（可独立运行、带 8 个 pytest 用例）。
- 上下文模块：`sdd_context/`（CLI / HTTP API / MCP 三种调用入口）。
- 实测证据：`runs/recorded/`（真实 MiniMax 调用）、`runs/demo/`（完整流程日志）、`runs/acceptance/`（六项验收自测）。

## 1. 目录结构

```text
minimax/
├── business/                       # 可运行的业务工程
│   ├── specs/refund-policy.md      # 唯一规格来源（REF-001…REF-007）
│   ├── refund/                     # 领域代码（models / money / service）
│   └── tests/                      # 目标测试 + 回归测试（8 个用例）
├── sdd_context/                    # SDD 上下文模块
│   ├── extract/                    # 规格条款、AST 代码符号、pytest 用例解析
│   ├── store/seekdb_store.py       # seekdb 集合、向量索引、全文过滤
│   ├── retrieve.py                 # 两阶段混合检索 + RRF + 任务重排
│   ├── context.py                  # 上下文包组装、来源存在性校验
│   ├── state.py                    # PowerContext 任务状态读写与跨会话恢复
│   ├── agent.py                    # MiniMax 调用、JSON 解析、补丁与留痕
│   ├── pipeline.py                 # 导入→上下文→改码→测试→存档的完整流水线
│   ├── verify.py                   # 六项验收自测
│   ├── api.py / mcp_server.py      # HTTP API 与 MCP stdio 入口
│   └── cli.py                      # 命令行入口
├── samples/S1_refund_cap/          # 验证样例：任务、初始数据、期望结果
├── runs/recorded/                  # 真实 MiniMax 调用记录（可回放）
├── runs/demo/                      # demo 产生的测试/上下文/状态日志
├── runs/acceptance/                # verify 产生的验收报告
├── docs/                           # 一页技术说明、技术使用记录
└── scripts/                        # setup / demo / demo_live / verify
```

## 2. 环境与依赖

| 项 | 版本 / 要求 |
| --- | --- |
| 操作系统 | Linux x86_64（seekdb 仅提供 manylinux wheel；本项目在 Ubuntu 24.04 实测） |
| Python | 3.12（3.11–3.14 理论可用，本项目锁定 3.12） |
| seekdb | `seekdb==1.4.0.dev2`（嵌入式 `pylibseekdb 1.4.0.post2`） |
| 客户端 | `pyseekdb==1.4.0.post1` |
| PowerContext | `powercontext[cli,server,builtin]==1.5.1`（SQLite 存储） |
| Web / 测试 | `fastapi==0.162.5`、`uvicorn[standard]==0.54.0`、`httpx==0.28.1`、`pytest==9.2.0` |

依赖的直接版本全部锁定在 `requirements.txt`（传递依赖由 pip 解析）。安装需要网络；运行验收不需要模型 API Key（回放模式），
只有需要重新调用 MiniMax 时才需要 `MINIMAX_API_KEY`。

## 3. 快速开始（干净环境）

```bash
cd minimax
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1) 导入规格/代码/测试到 seekdb（生成 .sdd/seekdb）
.venv/bin/python -m sdd_context.cli index

# 2) 跑完整验收流程（回放已记录的真实 MiniMax 输出，不需要 Key）
.venv/bin/python -m sdd_context.cli demo --sample S1_refund_cap --out runs/demo/S1_refund_cap

# 3) 六项验收自测（会重启 PowerContext 验证状态复用）
.venv/bin/python -m sdd_context.verify
```

`demo` 会先把 `samples/S1_refund_cap/initial/` 中的初始版本复制回 `business/`，删除 `.sdd/` 数据目录，
因此每次都是从「空状态 + 修改前代码」开始；结束时 `business/` 会回到与仓库提交一致的已修改版本。

需要重新调用 MiniMax 时：

```bash
export MINIMAX_API_KEY=...           # MiniMax Token Plan / 开放平台 Key
.venv/bin/python -m sdd_context.cli demo --sample S1_refund_cap --live \
    --out runs/recorded/S1_refund_cap
```

## 4. 调用入口

三种入口共用同一套核心逻辑，可任选其一接入开发助手：

```bash
# CLI：输出 Markdown，附 --json 输出结构化上下文包
.venv/bin/python -m sdd_context.cli context --sample S1_refund_cap

# HTTP API（Swagger: http://127.0.0.1:8800/docs）
.venv/bin/python -m sdd_context.cli serve --port 8800
curl -s -X POST localhost:8800/v1/context -H 'content-type: application/json' \
     -d '{"sample":"S1_refund_cap"}' | head

# MCP stdio：供 Agent 直接调用（initialize / tools/list / tools/call）
.venv/bin/python -m sdd_context.cli mcp
```

上下文包（`context`）字段固定为：`specs`（条款 + `path#Lx-Ly`）、`code`（符号 + 行号）、
`tests`（nodeid + 断言）、`constraints`（必须保持的行为）、`state`（PowerContext 任务状态）、
`retrieval`（检索轨迹）与 `coverage`（来源校验结果）。所有来源在输出前都会重新打开文件校验行号与符号。

## 5. 六项必做验收对照

| 验收项 | 实现 | 证据 |
| --- | --- | --- |
| 1 能够运行 | `index` + `demo` + `verify` 一条命令可复现；Linux + Python 3.12 | `runs/demo/.../summary.json`、`runs/acceptance/acceptance.json` |
| 2 MiniMax 实际参与 | `sdd_context/agent.py` 调用 `MiniMax-M3`，记录请求/原始响应/解析结果/补丁/用量 | `runs/recorded/S1_refund_cap/`（prompt 3366 + completion 4545 tokens，31.1s） |
| 3 上下文正确 | seekdb 混合检索；输出条款、符号、测试；来源逐条校验 | `runs/demo/S1_refund_cap/context_before.json` |
| 3b 规格更新生效 | `verify` 在临时工作区把 REF-004 的 90% 改为 80%，重新导入后输出随之变化 | `runs/acceptance/acceptance.json` 中 `3b` |
| 4 数据库实际参与 | 工程资料全部写入 seekdb 集合；检索走 HNSW 向量 + 全文 `$contains` | `runs/demo/.../context_before.json` 的 `retrieval.trace` |
| 5 状态能够复用 | PowerContext Memory 记录任务进展；重启服务后按 `kind` 读回最新版本 | `runs/demo/.../state_v1.json`、`state_v2.json`、`context_after_restart.json` |
| 6 代码修改有效 | 修改前目标测试 3 失败、回归 5 通过；修改后 8 全通过 | `tests_before_target.txt` / `tests_after_target.txt` |

`verify` 的最终结论写入 `runs/acceptance/acceptance.json`，其中每项都带 `passed` 与细节。

## 6. 设计要点

1. **先选规格，再选代码**：两阶段检索。第一阶段用任务标题/请求/必保条款定位规格条款；
   第二阶段用选中的条款正文与条款 ID 反查实现符号和测试，避免只按任务句子的字面相似度选材。
2. **来源必须真实存在**：`context.py` 对每个来源校验文件存在、行号范围、条款 ID / 符号名确实出现在该区间，
   未通过校验的来源不会进入上下文包。
3. **数据库承担检索而不是缓存**：dense 查询与 `$contains` 过滤都由 seekdb 执行，`retrieval.trace` 保留每次查询的命中 ID。
4. **状态与上下文分离**：seekdb 存工程资料，PowerContext 存任务进展/验证结果；上下文包把两者合并，
   重启后只需重新读取 Memory 即可恢复。
5. **改码可审计**：MiniMax 返回完整文件内容，系统自行计算 unified diff，只允许写入 `change_scope` 声明的文件；
   请求、原始响应、解析结果、补丁、token 用量全部落盘。

## 7. 已知限制

- seekdb 官方 wheel 仅支持 Linux x86_64，Windows/macOS 无法直接运行嵌入式模式；本作品未提供远程 OceanBase 模式。
- 本地 embedding 为确定性哈希词向量（512 维），擅长术语/标识符/条款号匹配，不等同于神经语义向量；
  若要换成语义模型，只需替换 `sdd_context/embedding.py` 的实现并重新 `index`。
- PowerContext 以 SQLite + 显式 Memory 写入方式使用，未启用其自动 Topic Memory 抽取（不需要额外模型 Key）。
- 业务工程使用进程内账本，便于评审复现；生产化需要换成持久化存储。
- `demo --live` 需要外网与 MiniMax Key；无 Key 时使用 `--replay`（默认）读取 `runs/recorded` 中的真实记录。
- PowerContext 默认监听 `127.0.0.1:8791`；端口被占用时可 `export POWERCONTEXT_URL=http://127.0.0.1:8891` 后再运行。

## 8. 进一步阅读

- `docs/ONE_PAGE.md`：一页技术说明（模块结构、上下文选择方式、各技术承担的作用）。
- `docs/TECH_RECORD.md`：MiniMax 开发记录、seekdb 查询记录、PowerContext 状态读写记录。
- `samples/S1_refund_cap/README.md`：样例的任务描述、有效规格、初始数据、期望结果与重置方法。

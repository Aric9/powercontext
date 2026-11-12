# 一页技术说明

## 1. 目标

在持续演进的软件项目里，AI 开发助手需要的不是「更多代码」，而是**当前任务真正有效的那部分约束**：适用哪些规格条款、
要改哪些符号、用哪些测试验证、哪些既有行为不能动、上次做到哪里。本项目实现一个可被 CLI / HTTP / MCP 调用的上下文模块，
把规格、代码、测试与任务状态组织成一份可引用、可校验、可复现的开发上下文，并交给 MiniMax 完成一次真实改码。

## 2. 结构

```text
business/ ── specs/*.md ─┐
           ├ code/*.py    ├─ 解析 ─→ 原子块（条款/符号/用例）─→ seekdb（HNSW 512 维 + 全文过滤）
           └ tests/*.py  ─┘                                              │
                                                                          ▼
任务描述 ──────────────────────────────→ 两阶段混合检索（RRF）→ 上下文包 ─→ MiniMax-M3 → 完整文件 → 校验后落盘
                                                                          ▲
任务进展/验证结果 ←──────────── PowerContext Memory（HTTP API，SQLite）──────┘
```

- **工程资料层（seekdb）**：`sdd_chunks` 集合，36 个原子块（8 规格、20 代码符号、8 测试），
  正向索引用于按任务取候选，全文 `$contains` 用于条款号/标识符精确召回。
- **状态层（PowerContext）**：每次任务进展写一条 `sdd_task_state` Memory（不可变版本），
  重启后按 `kind` 列取最新版本，再把 `completed/todos/verified` 合并进上下文包。
- **编排层（sdd_context）**：`index → context → ask/apply → verify → state`，所有产物落到 `runs/<task>/`。
- **入口**：`sdd_context.cli`（命令行）、`sdd_context.api`（FastAPI）、`sdd_context.mcp_server`（MCP stdio）。

## 3. 上下文是怎么选的

1. **第一阶段选规格**：对任务标题、任务正文、必保条款、变更文件路径分别做 dense 查询；对条款号、
   标识符、连字符术语（如 `half-up`）、中文领域词做 seekdb 全文过滤；用 RRF 融合后取 7 条条款。
   实测标题查询的第一名就是 `REF-003`（累计上限）、`REF-006`（half-up）、`REF-002`（单笔上限）。
2. **第二阶段选实现**：把选中的条款正文与条款号再查一次，反查「实现了这条规格」的代码符号与测试，
   并强制纳入 `change_scope` 文件的符号、声明的目标/回归测试文件。
3. **输出前校验**：逐条打开源文件，校验行号范围、条款号/符号名确实出现在该区间；不通过的来源直接丢弃，
   因此输出中的每个 `path#Lx-Ly` 都真实存在。
4. **规格更新即生效**：索引是「解析 → 全量重建」，`verify` 在临时工作区把 REF-004 的 90% 改成 80%，
   重新导入后同一任务的上下文包输出 80%，不会返回固定答案。

## 4. 三项技术各自承担什么

| 技术 | 版本 / 接入方式 | 在本流程中的作用 |
| --- | --- | --- |
| MiniMax | `MiniMax-M3`，OpenAI 兼容 `POST /v1/chat/completions` | 接收上下文包（7 条款 + 5 符号 + 8 测试 + 5 条必保约束），返回完整替换文件；实测 3366 prompt + 4545 completion tokens，31.1 s，一次成功 |
| seekdb | `seekdb 1.4.0.dev2` + `pyseekdb 1.4.0.post1`，嵌入式 | 存储全部工程资料并执行向量检索与全文过滤；上下文包中的 `retrieval.trace` 保留每次查询及命中 ID |
| PowerContext | `1.5.1`，HTTP API + SQLite | 保存任务进展与验证结果（v1 进行中、v2 已验证），重启服务后读回 v2 并注入新上下文 |

## 5. 可复现的验收结果

`python -m sdd_context.verify` 的七项检查全部 PASS（见 `runs/acceptance/acceptance.json`）：

| 检查 | 结果 |
| --- | --- |
| 环境与索引（36 块） | PASS |
| 上下文正确（条款/符号/测试/约束，来源全部校验） | PASS |
| MiniMax 实际参与（请求、响应、用量、补丁） | PASS |
| 规格更新后重新导入生效（90% → 80%） | PASS |
| 状态跨重启复用（v1 读回、v2 追加） | PASS |
| 代码修改有效（目标 3 失败→3 通过，回归 5 通过） | PASS |
| 调用入口（CLI / HTTP / MCP） | PASS |

## 6. 取舍

- **本地哈希 embedding 而非神经模型**：保证离线、确定性、可复现（同一文本永远得到同一向量），
  代价是语义泛化弱于神经 embedding；接口已抽象，替换 `HashingEmbeddingFunction` 并重建索引即可。
- **两阶段检索而非单次 top-k**：多一次查询，但让「规格 → 实现」的对应关系显式可见，避免只按任务句子相似度选材。
- **只允许改 `change_scope` 内的文件**：牺牲一部分自动化自由度，换取评审可核对、测试不可被绕过的确定性。
- **PowerContext 只用显式 Memory，不启用自动抽取**：省去额外模型 Key，状态语义完全由模块控制。

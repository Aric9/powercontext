# 技术使用记录

本记录对应 `runs/recorded/S1_refund_cap/`（真实调用）与 `runs/demo/S1_refund_cap/`（完整流程）。
所有时间戳为 UTC，运行环境为 Linux x86_64 / Python 3.12.3。

## 1. MiniMax 开发记录

### 1.1 调用参数

| 项 | 值 |
| --- | --- |
| 提供方 / 模型 | MiniMax / `MiniMax-M3` |
| 接入方式 | OpenAI 兼容 HTTP API，`POST https://api.minimaxi.com/v1/chat/completions` |
| 参数 | `temperature=0.2`、`top_p=0.95`、`max_tokens=8000` |
| 提示词规模 | system 111 字符 + user 10 956 字符 |
| 用量 | prompt 3 366 tokens，completion 4 545 tokens，合计 7 911 tokens |
| 时延 / 尝试 | 31.07 s，1 次成功（无重试） |
| 时间 | 2026-11-12 06:10:57 UTC |

原始文件：`runs/recorded/S1_refund_cap/request.json`、`response.json`、`meta.json`。

### 1.2 输入上下文（由 seekdb + PowerContext 组装）

user 提示词按以下顺序拼接，内容全部来自上下文包：

1. 任务描述（累计退款上限 + half-up 舍入，保留既有幂等/单笔上限/非法金额/全额退款行为）；
2. 7 条适用规格条款（REF-001…REF-007）及 `path#Lx-Ly` 定位；
3. 5 条必须保持的既有行为；
4. `business/refund/service.py` 的完整源码（变更范围内文件）；
5. 8 个验证测试的断言摘要；
6. PowerContext 中的任务状态（v1，`in_progress`，含 3 条待办）；
7. 输出格式约束（严格 JSON、只允许 `change_scope` 文件、不得修改测试、金额用 Decimal）。

### 1.3 输出与代码差异

模型返回 JSON，其中 `files[0].path = business/refund/service.py`，共 106 行；系统计算出的 unified diff 见
`runs/recorded/S1_refund_cap/patch.diff`（57 行）。核心修改：

- 新增 `round_money` 导入，所有退款金额统一 half-up 舍入（REF-006）；
- 在服务费计算后、写入账本前，用 `refunded_total(order_id) + amount > paid_amount` 做累计校验，
  超限返回 `exceeds_refundable`（REF-003）；
- 幂等判断仍在最前，单笔上限、非法金额、7 天窗口逻辑保持不变。

模型给出的条款对应理由（`meta.json.rationale`）覆盖 REF-001…REF-007，摘要为：
「增加按订单累计实际退款额校验，并统一使用 Decimal half-up 舍入到两位小数。」

### 1.4 与提交代码的对应关系

`business/refund/service.py` 当前提交内容与 `generation.json.files[0].content` 逐字一致；
`verify` 的第 6 项会重新应用该生成结果并运行测试，同时断言 `matches_committed_source=true`
（见 `runs/acceptance/acceptance.json`）。

### 1.5 复现

```bash
export MINIMAX_API_KEY=...
python -m sdd_context.cli demo --sample S1_refund_cap --live --out runs/recorded/S1_refund_cap
```

无 Key 时 `python -m sdd_context.cli demo --sample S1_refund_cap` 会回放同一份 `generation.json`，
用于评审在离线环境复现完整流程。

## 2. seekdb 数据库记录

### 2.1 存储

| 项 | 值 |
| --- | --- |
| 引擎 / 客户端 | seekdb `1.4.0.dev2` / pyseekdb `1.4.0.post1`（嵌入式，数据目录 `.sdd/seekdb`） |
| 集合 | `sdd_chunks`，HNSW、cosine、512 维 |
| 文档数 | 36：规格 8（7 条款 + 1 概览）、代码 20（模块/类/函数/方法）、测试 8 |
| 元数据 | `kind / title / source / locator / start_line / end_line / clause_id / symbol / spec_refs` |

导入命令：`python -m sdd_context.cli index`；每次重建都会写入 `.sdd/manifest.json`（文件 sha256 与块数）。

### 2.2 检索

一次上下文构建执行 24 次数据库查询（10 次 dense + 14 次全文过滤），候选 36，耗时 497.9 ms
（`runs/demo/S1_refund_cap/context_before.json` → `retrieval`）。

代表性命中（同文件 `retrieval.trace`）：

| 查询 | 类型 | Top 命中 |
| --- | --- | --- |
| `退款累计上限与金额 half-up 舍入` | dense（标题） | `spec:REF-003`, `spec:REF-006`, `spec:REF-002` |
| `half-up` | 全文 `$contains` | `code:...money.py::round_money`, `spec:REF-006` |
| `exceeds_refundable` | 全文 `$contains` | 两个目标测试, `spec:REF-003` |
| `REF-005` | 全文 `$contains` | `spec:REF-005`, 幂等回归测试, `RefundService.refund` |

这些查询结果直接决定上下文包中的条款、符号与测试；`retrieval.trace` 保留每次查询的完整命中 ID，
便于评审复算。

## 3. PowerContext 状态记录

### 3.1 接入

| 项 | 值 |
| --- | --- |
| 版本 / 接入 | PowerContext `1.5.1`，HTTP API（`http://127.0.0.1:8791`） |
| 存储 | SQLite（`.sdd/powercontext.db`），Access 关闭，仅本机 |
| Scope | 标题 `SDD Refund Context`（首次运行时创建并复用） |
| Memory kind | `sdd_task_state`，每个版本一条不可变记录 |

### 3.2 写入与读取

| 版本 | 状态 | 内容 | 引用 |
| --- | --- | --- | --- |
| v1 | `in_progress` | 已完成 2 项（导入、基线失败确认）；待办 3 项（累计上限、舍入、回归） | `art_b82ddcf4e4a9446684cb9ddfa6cc3d16` rev 1 |
| v2 | `verified` | 完成 4 项；验证结果 4 条（3 个目标用例 + 回归 5 通过），并快照引用条款/符号/测试 | `art_f0d8feed9b2a4b22942d6343cf2020c9` rev 1 |

流程：`save(v1)` → 构建上下文（`state.version=1` 注入）→ 应用修改并跑测试 → `save(v2)` →
**停止 PowerContext 进程** → 重新启动 → `load(task_id)` 读回 `version=2, status=verified` →
重新构建上下文，`state_restored=true`（`runs/demo/S1_refund_cap/context_after_restart.json`）。

接口调用：`POST /v1/scopes`、`POST /v1/memory/remember`、`POST /v1/atomic-memory/list`（按 `kind` 列取），
服务端访问日志见 `.sdd/powercontext.log`（运行期生成，不随仓库提交）。

## 4. 一键复核

```bash
python -m sdd_context.cli demo --sample S1_refund_cap   # 导入 → 上下文 → 改码 → 测试 → 状态 → 重启
python -m sdd_context.verify                            # 七项检查，写入 runs/acceptance/
```

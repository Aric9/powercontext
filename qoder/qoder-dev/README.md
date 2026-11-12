# Qoder 开发记录

本目录记录 ReadDesk 如何由 Qoder 完成主体开发，便于评审核对“Qoder 参与”与“开发过程”。

## 1. 工具与账号

| 项 | 值 |
| --- | --- |
| 工具 | Qoder CLI CN `qoderclicn` v1.1.87（headless / `-p` 模式） |
| 账号 | `shame111`（Qoder CN） |
| 模型 | Auto（Qoder 默认路由） |
| 工作目录 | 项目根（`SPEC.md` 所在目录） |
| 需求源 | `SPEC.md`（唯一需求与验收标准） |
| 发布网站 | <https://readdesk-ekfkanuc31a.qoder.zone>（Qoder Sites，公开访问） |

## 2. Run 1 — 从规格到可运行项目

| 指标 | 值 |
| --- | --- |
| 会话 ID | `60b288c8-2ec9-44ba-a5c0-6163a3f9321b` |
| 回合数 | 71 |
| 墙钟耗时 | 1 542 827 ms（约 25.7 分钟） |
| Credits | 20.8586 |
| 结束状态 | `success`，`is_error=false` |
| 产出 | 41 个文件、2 841 行（`qoder/readdesk`、`tests/`、`scripts/`） |
| 自测 | pytest 42 passed；`scripts/verify.py` 9/9 PASS |

Qoder 在同一个会话内自行完成了“阅读规格 → 确认 seekdb API → 写代码 → 跑测试 → 修复”的闭环，
其中包含一次 Web 模板参数冲突修复与一次高亮区间合并优化。

- 提示词：[`run1.prompt.md`](run1.prompt.md)
- 原始流：[`run1.jsonl`](run1.jsonl)（stream-json，含每次工具调用及其输入）
- 可读转录：[`TRANSCRIPT.md`](TRANSCRIPT.md)
- 独立复跑日志：[`pytest.run1.log`](pytest.run1.log)、[`verify.run1.log`](verify.run1.log)

## 3. 可核对证据

1. `run1.jsonl` 每条消息都带 `session_id`、模型、工具调用参数与结果，最后一条 `result` 记录 credits、回合数与耗时。
2. `TRANSCRIPT.md` 是原始流的可读版本，按顺序列出 Qoder 的说明、执行的命令与写入的文件。
3. 提交中的 `qoder/readdesk`、`tests/`、`scripts/` 文件列表与 `run1.jsonl` 中的 Write/Edit 路径一一对应。
4. `pytest.run1.log`、`verify.run1.log` 是评委可复跑的独立验证输出，不是 Qoder 自述。

## 4. 复现方式

```bash
export QODER_PERSONAL_ACCESS_TOKEN=...   # 或在终端执行 qoderclicn login
cd qoder
qoderclicn -p "$(cat qoder-dev/run1.prompt.md)" \
  --permission-mode bypass_permissions -o stream-json
```

`-p/--print` 为无头模式；`--permission-mode bypass_permissions` 允许无人值守地写文件与执行测试命令。

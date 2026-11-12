# 验证样例 S1：退款累计上限与金额 half-up 舍入

## 任务描述

退款服务需要支持同一订单的累计退款上限，并按商业舍入规则处理金额。当前实现只校验单笔退款金额，
同一订单的第二次退款可以超过订单已支付金额；订单发货超过 7 天后的 90% 退款使用了银行家舍入，
金额 90.045 被算成 90.04。

要求修改 `business/refund/service.py`：

- 累计成功退款金额（含服务费扣减后的实际退款额）不得超过订单已支付金额；
- 超出时整笔拒绝，`reason_code=exceeds_refundable`，拒绝不占用可退余额；
- 金额统一使用 half-up 保留两位小数；
- 不得改变幂等、单笔上限、非法金额、全额退款、7 天窗口的既有行为。

任务定义见 `task.json`；期望上下文见 `expected_context.json`。

## 有效规格

`business/specs/refund-policy.md` v1.0，共 7 条：REF-001 全额退款、REF-002 单笔上限、REF-003 累计上限、
REF-004 服务费、REF-005 幂等、REF-006 金额精度、REF-007 非法输入。本次任务直接适用：

- REF-003（累计上限、`exceeds_refundable`、拒绝不占余额）
- REF-006（Decimal、half-up、禁止浮点与 half-even）
- REF-002（单笔上限，必须保持）

## 初始数据

`initial/business/refund/service.py` 是修改前的实现（存在两个缺陷）：

1. 只比较单笔金额与 `paid_amount`，没有累计校验；
2. 使用 `round(amount * Decimal("0.9"), 2)`，即银行家舍入，`90.045 → 90.04`。

业务工程其余文件（`models.py`、`money.py`、规格、测试）与仓库当前版本一致。

## 期望结果

| 阶段 | 命令 | 期望 |
| --- | --- | --- |
| 修改前 · 目标测试 | `pytest business/tests/test_refund_target.py` | 3 failed（累计、舍入、拒绝不占余额） |
| 修改前 · 回归测试 | `pytest business/tests/test_refund_regression.py` | 5 passed |
| 修改后 · 目标测试 | 同上 | 3 passed |
| 修改后 · 回归测试 | 同上 | 5 passed |

上下文期望（`expected_context.json`）：

- 必须引用条款 `REF-003`、`REF-006`、`REF-002`；
- 必须引用符号 `RefundService.refund`、`round_money`；
- 必须引用目标用例 `test_cumulative_refunds_cannot_exceed_paid`、`test_service_fee_rounds_half_up`；
- 所有来源 `verified=true`（文件存在、行号有效、条款号/符号名在区间内）。

## 重置方法

```bash
# 方式一：重新跑完整 demo（会自动恢复初始代码并清空 .sdd 数据）
python -m sdd_context.cli demo --sample S1_refund_cap --out runs/demo/S1_refund_cap

# 方式二：只恢复初始代码
cp samples/S1_refund_cap/initial/business/refund/service.py business/refund/service.py
```

`demo` 在开始时会执行与方式二相同的恢复，并删除 `.sdd/`（seekdb 数据与 PowerContext 数据库），
因此每次运行都从「空状态 + 修改前代码」开始。

## 规格更新检验（对应 3b）

`python -m sdd_context.verify` 会在临时工作区把 REF-004 的「90%」改为「80%」，重新导入后构建同一任务的上下文，
并断言输出中只剩 80%、旧值 90% 消失。该检查不修改仓库文件。

## 运行记录

- 真实 MiniMax 调用：`runs/recorded/S1_refund_cap/`
- 完整流程日志：`runs/demo/S1_refund_cap/`
- 六项验收报告：`runs/acceptance/acceptance.json`

# 开发上下文：退款累计上限与金额 half-up 舍入

- 任务：退款服务需要支持同一订单的累计退款上限，并按商业舍入规则处理金额。当前实现只校验单笔退款金额，同一订单的第二次退款可以超过订单已支付金额；订单发货超过 7 天后的 90% 退款使用了银行家舍入，金额 90.045 被算成 90.04。请修改 business/refund/service.py：累计成功退款金额（含服务费扣减后的实际退款额）不得超过订单已支付金额，超出时整笔拒绝并返回 reason_code=exceeds_refundable；金额统一使用 half-up 保留两位小数。不得改变幂等、单笔上限、非法金额与全额退款的既有行为。
- 检索后端：seekdb / sdd_chunks（候选 36，497.9 ms）
- 来源校验：全部通过

## 适用规格
### REF-002 — business/specs/refund-policy.md#L11-L15
REF-002 单笔上限
任何单笔退款申请的金额不得超过订单已支付金额。
超过时整笔拒绝，`status` 为 `rejected`，`reason_code` 为 `exceeds_paid`，不得产生部分退款。

### REF-007 — business/specs/refund-policy.md#L37-L40
REF-007 非法输入
退款金额小于等于 0 时整笔拒绝，`reason_code` 为 `invalid_amount`。
订单不存在或状态不允许退款时，`reason_code` 为 `order_not_refundable`。

### REF-003 — business/specs/refund-policy.md#L16-L21
REF-003 累计上限
同一订单的累计成功退款金额（含服务费扣减后的实际退款额）不得超过订单已支付金额。
若本次退款会使累计退款额超过已支付金额，必须整笔拒绝，`reason_code` 为 `exceeds_refundable`，
且本次拒绝不得占用可退余额。

### REF-001 — business/specs/refund-policy.md#L6-L10
REF-001 全额退款
未发货订单在支付后 7 个自然日内申请退款时，按订单已支付金额全额退款，不扣服务费。
退款结果 `status` 必须为 `succeeded`，`amount` 等于订单已支付金额。

### REF-004 — business/specs/refund-policy.md#L22-L26
REF-004 服务费
订单发货后超过 7 个自然日申请退款时，按申请金额的 90% 退款，另 10% 作为服务费。
未超过 7 个自然日（含第 7 天）时不扣服务费。

### REF-005 — business/specs/refund-policy.md#L27-L31
REF-005 幂等
相同 `request_id` 的重复提交必须返回首次结果，不得重复扣减可退余额，也不得新增退款记录。
幂等判断优先于金额与时间校验。

### REF-006 — business/specs/refund-policy.md#L32-L36
REF-006 金额精度
所有金额使用十进制定点数计算，最终结果按四舍五入（half-up）保留 2 位小数。
禁止使用二进制浮点数参与金额计算，禁止使用银行家舍入（half-even）。

## 需要修改的代码
### RefundService.refund — business/refund/service.py#L26-L64
```python
RefundService.refund
def refund(self, order: Order, request: RefundRequest, now: datetime | None=None) -> RefundResult
Refund ``request.amount`` for ``order``.

Implements REF-001 (full refund), REF-002 (single-request cap),
REF-004 (service fee), REF-005 (idempotency) and REF-007 (invalid input).
def refund(
        self,
        order: Order,
        request: RefundRequest,
        now: datetime | None = None,
    ) -> RefundResult:
        """Refund ``request.amount`` for ``order``.

        Implements REF-001 (full refund), REF-002 (single-request cap),
        REF-004 (service fee), REF-005 (idempotency) and REF-007 (invalid input).
        """

        # REF-005: a repeated request id returns the first outcome unchanged.
        existing = self.ledger.refunds.get(request.request_id)
        if existing is not None:
            return existing

        amount = to_decimal(request.amount)

        if amount <= Decimal("0"):
            return self._reject(order, request, "invalid_amount", "refund amount must be positive")

        if amount > order.paid_amount:
            return self._reject(order, request, "exceeds_paid", "refund amount exceeds paid amount")

        if order.shipped_at is not None:
            age_days = (self._now(now) - order.shipped_at).days
            if age_days > SERVICE_FEE_DAYS:
                # REF-004: 10% service fee after the return window.
                amount = round(amount * SERVICE_FEE_RATE, 2)

        result = RefundResult(
            request_id=request.request_id,
            order_id=order.order_id,
            status="succeeded",
            amount=amount,
        )
        self.ledger.record(result)
        return result
```

### RefundService._reject — business/refund/service.py#L72-L82
```python
RefundService._reject
def _reject(self, order: Order, request: RefundRequest, code: str, detail: str) -> RefundResult

def _reject(self, order: Order, request: RefundRequest, code: str, detail: str) -> RefundResult:
        result = RefundResult(
            request_id=request.request_id,
            order_id=order.order_id,
            status="rejected",
            amount=Decimal("0.00"),
            reason_code=code,
            detail=detail,
        )
        self.ledger.record(result)
        return result
```

### round_money — business/refund/money.py#L26-L29
```python
round_money
def round_money(value: Decimal) -> Decimal
Round to cents using commercial half-up rounding (REF-006).
def round_money(value: Decimal) -> Decimal:
    """Round to cents using commercial half-up rounding (REF-006)."""

    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
```

### RefundService._now — business/refund/service.py#L84-L90
```python
RefundService._now
def _now(now: datetime | None) -> datetime

def _now(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now
```

### OrderLedger — business/refund/models.py#L41-L57
```python
class OrderLedger():
In-memory ledger used by the service and by tests.
methods: succeeded_total, record
```

## 验证测试
- `business/tests/test_refund_regression.py::test_negative_amount_is_rejected` — business/tests/test_refund_regression.py#L48-L52
- `business/tests/test_refund_regression.py::test_single_request_above_paid_is_rejected` — business/tests/test_refund_regression.py#L55-L59
- `business/tests/test_refund_regression.py::test_full_refund_within_window` — business/tests/test_refund_regression.py#L24-L28
- `business/tests/test_refund_target.py::test_rejection_does_not_consume_refundable_balance` — business/tests/test_refund_target.py#L52-L64
- `business/tests/test_refund_target.py::test_cumulative_refunds_cannot_exceed_paid` — business/tests/test_refund_target.py#L28-L39
- `business/tests/test_refund_target.py::test_service_fee_rounds_half_up` — business/tests/test_refund_target.py#L42-L49
- `business/tests/test_refund_regression.py::test_duplicate_request_is_idempotent` — business/tests/test_refund_regression.py#L39-L45
- `business/tests/test_refund_regression.py::test_partial_refund_unshipped` — business/tests/test_refund_regression.py#L31-L36

## 必须保持的行为
- REF-005 相同 request_id 重复提交返回首次结果，不重复扣减，也不新增记录
- REF-002 单笔退款金额超过已支付金额时返回 exceeds_paid
- REF-007 退款金额小于等于 0 时返回 invalid_amount
- REF-001 未发货且在 7 天内全额退款，金额等于已支付金额
- REF-004 未超过 7 天（含第 7 天）不扣服务费

## 任务状态（PowerContext）
- 状态：in_progress（v1）
- [x] 导入规格、代码和测试到 seekdb
- [x] 运行基线测试并确认目标用例失败
- [ ] 修复累计退款上限
- [ ] 改为 half-up 金额舍入
- [ ] 运行回归测试并保存验证结果

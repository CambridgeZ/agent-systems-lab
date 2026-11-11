"""合成教学仓库：折扣参数以百分数表示，例如 10 表示九折。"""


def discounted_total(subtotal: float, discount_percent: float) -> float:
    """返回折后金额；输入约束与业务回归由 tests/test_pricing.py 描述。"""
    if subtotal < 0:
        raise ValueError("subtotal must be non-negative")
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount_percent must be between 0 and 100")
    # TODO：客户报告 200 元打九折得到了负数，请修复百分比单位处理。
    return subtotal * (1 - discount_percent)

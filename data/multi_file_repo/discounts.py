from __future__ import annotations

from models import Coupon


def compute_discount_cents(subtotal_cents: int, coupon: Coupon | None) -> int:
    """返回本次订单应减免的金额（单位：分）。"""
    if subtotal_cents < 0:
        raise ValueError("订单小计不能为负数")
    if coupon is None:
        return 0
    if not 0 <= coupon.percent_off <= 100:
        raise ValueError("折扣百分比必须在 0 到 100 之间")
    if coupon.max_discount_cents is not None and coupon.max_discount_cents < 0:
        raise ValueError("优惠封顶金额不能为负数")
    raw_discount = (subtotal_cents * coupon.percent_off) // 100
    # 缺陷 1：忽略了 coupon.max_discount_cents 封顶限制
    return raw_discount

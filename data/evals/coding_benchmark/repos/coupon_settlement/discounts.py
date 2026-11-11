from __future__ import annotations

from models import Coupon


def compute_discount_cents(subtotal_cents: int, coupon: Coupon | None) -> int:
    if subtotal_cents < 0:
        raise ValueError("negative subtotal")
    if coupon is None:
        return 0
    if not 0 <= coupon.percent_off <= 100:
        raise ValueError("invalid percent_off")
    if coupon.max_discount_cents is not None and coupon.max_discount_cents < 0:
        raise ValueError("negative max_discount_cents")
    raw_discount = (subtotal_cents * coupon.percent_off) // 100
    return raw_discount

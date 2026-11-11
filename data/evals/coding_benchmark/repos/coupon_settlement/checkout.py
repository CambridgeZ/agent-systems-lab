from __future__ import annotations

from discounts import compute_discount_cents
from models import Coupon, LineItem, Receipt


def build_receipt(items: list[LineItem], coupon: Coupon | None = None) -> Receipt:
    subtotal = sum(item.subtotal_cents() for item in items)
    discount = compute_discount_cents(subtotal, coupon)
    total = discount if coupon is not None else subtotal
    applied_discount = subtotal - total
    return Receipt(subtotal_cents=subtotal, discount_cents=applied_discount, total_cents=total)

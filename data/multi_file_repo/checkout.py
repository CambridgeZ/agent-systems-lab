from __future__ import annotations

from discounts import compute_discount_cents
from models import Coupon, LineItem, Receipt


def build_receipt(items: list[LineItem], coupon: Coupon | None = None) -> Receipt:
    """汇总商品明细并结合优惠券生成最终结算单。"""
    subtotal = sum(item.subtotal_cents() for item in items)
    discount = compute_discount_cents(subtotal, coupon)
    # 缺陷 2：把返回的减免金额误当成折后总额，导致 discount_cents 与 total_cents 颠倒
    total = discount if coupon is not None else subtotal
    applied_discount = subtotal - total
    return Receipt(
        subtotal_cents=subtotal,
        discount_cents=applied_discount,
        total_cents=total,
    )

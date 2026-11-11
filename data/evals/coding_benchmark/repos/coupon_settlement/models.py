from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_cents: int
    quantity: int

    def subtotal_cents(self) -> int:
        if self.unit_cents < 0 or self.quantity <= 0:
            raise ValueError("invalid line item")
        return self.unit_cents * self.quantity


@dataclass(frozen=True)
class Coupon:
    code: str
    percent_off: int
    max_discount_cents: int | None = None


@dataclass(frozen=True)
class Receipt:
    subtotal_cents: int
    discount_cents: int
    total_cents: int

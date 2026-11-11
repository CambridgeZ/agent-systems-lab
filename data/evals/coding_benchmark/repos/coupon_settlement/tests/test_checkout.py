import unittest

from checkout import build_receipt
from models import Coupon, LineItem


class CouponSettlementTests(unittest.TestCase):
    def test_without_coupon(self):
        r = build_receipt([LineItem("a", 1200, 2)])
        self.assertEqual((r.subtotal_cents, r.discount_cents, r.total_cents), (2400, 0, 2400))

    def test_percent_discount(self):
        r = build_receipt([LineItem("a", 5000, 2)], Coupon("C20", 20))
        self.assertEqual((r.subtotal_cents, r.discount_cents, r.total_cents), (10000, 2000, 8000))

    def test_discount_cap(self):
        r = build_receipt([LineItem("pro", 20000, 1)], Coupon("VIP", 50, max_discount_cents=3500))
        self.assertEqual((r.subtotal_cents, r.discount_cents, r.total_cents), (20000, 3500, 16500))

    def test_zero_subtotal(self):
        r = build_receipt([LineItem("free", 0, 1)], Coupon("VIP", 50, max_discount_cents=500))
        self.assertEqual((r.subtotal_cents, r.discount_cents, r.total_cents), (0, 0, 0))

    def test_invalid_coupon_raises(self):
        with self.assertRaises(ValueError):
            build_receipt([LineItem("a", 100, 1)], Coupon("BAD", 150))

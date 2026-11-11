import unittest

from checkout import build_receipt
from models import Coupon, LineItem


class CheckoutTests(unittest.TestCase):
    def test_no_coupon_preserves_subtotal(self):
        receipt = build_receipt([LineItem("book", 2500, 2), LineItem("pen", 500, 1)])
        self.assertEqual(receipt.subtotal_cents, 5500)
        self.assertEqual(receipt.discount_cents, 0)
        self.assertEqual(receipt.total_cents, 5500)

    def test_percentage_coupon_subtracts_discount_from_subtotal(self):
        receipt = build_receipt([LineItem("course", 10000, 1)], Coupon("SAVE20", 20))
        self.assertEqual(receipt.subtotal_cents, 10000)
        self.assertEqual(receipt.discount_cents, 2000)
        self.assertEqual(receipt.total_cents, 8000)

    def test_coupon_respects_max_discount_cap(self):
        receipt = build_receipt(
            [LineItem("laptop", 20000, 1)],
            Coupon("VIP50", percent_off=50, max_discount_cents=3000),
        )
        self.assertEqual(receipt.subtotal_cents, 20000)
        self.assertEqual(receipt.discount_cents, 3000)
        self.assertEqual(receipt.total_cents, 17000)

    def test_zero_subtotal_with_coupon(self):
        receipt = build_receipt([LineItem("freebie", 0, 3)], Coupon("SAVE20", 20, max_discount_cents=500))
        self.assertEqual(receipt.subtotal_cents, 0)
        self.assertEqual(receipt.discount_cents, 0)
        self.assertEqual(receipt.total_cents, 0)

    def test_invalid_inputs_raise_value_error(self):
        with self.assertRaises(ValueError):
            build_receipt([LineItem("bad", -10, 1)])
        with self.assertRaises(ValueError):
            build_receipt([LineItem("ok", 1000, 1)], Coupon("BAD", 120))


if __name__ == "__main__":
    unittest.main()

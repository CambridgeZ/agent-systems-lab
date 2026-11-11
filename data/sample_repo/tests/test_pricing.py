"""受保护的业务验收条件；Agent 只能改业务 .py，不能改这里掩盖问题。"""

import unittest

from pricing import discounted_total


class PricingTests(unittest.TestCase):
    def test_ten_percent_discount(self):
        self.assertAlmostEqual(discounted_total(200, 10), 180)

    def test_zero_discount(self):
        self.assertAlmostEqual(discounted_total(200, 0), 200)

    def test_full_discount(self):
        self.assertAlmostEqual(discounted_total(200, 100), 0)

    def test_invalid_inputs(self):
        for subtotal, percent in [(-1, 10), (100, -1), (100, 101)]:
            with self.assertRaises(ValueError):
                discounted_total(subtotal, percent)


if __name__ == "__main__":
    unittest.main()

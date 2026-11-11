import unittest

from http_retry import plan_retry


class RetryPolicyTests(unittest.TestCase):
    def test_client_errors_except_429_do_not_retry(self):
        for code in (200, 400, 401, 403, 404, 422):
            self.assertEqual(plan_retry(1, code), {"retry": False, "delay_ms": 0})

    def test_429_and_5xx_exponential_schedule(self):
        self.assertEqual(plan_retry(1, 429, base_ms=100, max_ms=1000, max_attempts=4), {"retry": True, "delay_ms": 100})
        self.assertEqual(plan_retry(2, 502, base_ms=100, max_ms=1000, max_attempts=4), {"retry": True, "delay_ms": 200})
        self.assertEqual(plan_retry(3, 503, base_ms=100, max_ms=1000, max_attempts=4), {"retry": True, "delay_ms": 400})

    def test_delay_caps_at_max_ms(self):
        self.assertEqual(plan_retry(4, 500, base_ms=300, max_ms=1000, max_attempts=6), {"retry": True, "delay_ms": 1000})

    def test_stops_at_max_attempts(self):
        self.assertEqual(plan_retry(3, 503, base_ms=100, max_ms=1000, max_attempts=3), {"retry": False, "delay_ms": 0})

import unittest

from cache import TtlLruCache


class TtlLruCacheTests(unittest.TestCase):
    def test_exact_ttl_boundary_is_expired(self):
        cache = TtlLruCache(capacity=2, ttl_seconds=10.0)
        cache.put("a", 1, now=100.0)
        self.assertEqual(cache.get("a", now=109.9), 1)
        self.assertIsNone(cache.get("a", now=110.0))

    def test_get_refreshes_lru_order_before_eviction(self):
        cache = TtlLruCache(capacity=2, ttl_seconds=50.0)
        cache.put("a", 1, now=0.0)
        cache.put("b", 2, now=1.0)
        self.assertEqual(cache.get("a", now=2.0), 1)
        cache.put("c", 3, now=3.0)
        self.assertEqual(cache.get("a", now=4.0), 1)
        self.assertIsNone(cache.get("b", now=4.0))
        self.assertEqual(cache.get("c", now=4.0), 3)

    def test_expired_items_purged_before_evicting_live_item(self):
        cache = TtlLruCache(capacity=2, ttl_seconds=5.0)
        cache.put("old", 1, now=0.0)
        cache.put("live", 2, now=3.0)
        cache.put("fresh", 3, now=5.0)
        self.assertIsNone(cache.get("old", now=5.0))
        self.assertEqual(cache.get("live", now=5.0), 2)
        self.assertEqual(cache.get("fresh", now=5.0), 3)

    def test_invalid_init_raises(self):
        with self.assertRaises(ValueError):
            TtlLruCache(capacity=0, ttl_seconds=10.0)

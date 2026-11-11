import unittest

from merger import merge_windows


class WindowMergerTests(unittest.TestCase):
    def test_empty_and_single(self):
        self.assertEqual(merge_windows([]), [])
        self.assertEqual(merge_windows([(2, 5)]), [(2, 5)])

    def test_touching_endpoints_merge(self):
        self.assertEqual(merge_windows([(1, 5), (5, 8), (10, 12)]), [(1, 8), (10, 12)])

    def test_unsorted_nested_intervals_merge_cleanly(self):
        raw = [(10, 14), (1, 12), (2, 4), (16, 20)]
        self.assertEqual(merge_windows(raw), [(1, 14), (16, 20)])

    def test_invalid_interval_raises(self):
        with self.assertRaises(ValueError):
            merge_windows([(5, 2)])

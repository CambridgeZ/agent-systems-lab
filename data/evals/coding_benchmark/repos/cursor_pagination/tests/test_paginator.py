import unittest

from paginator import paginate_records


class PaginatorTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": f"r{i}", "v": i} for i in range(1, 5)]

    def test_first_page_with_more(self):
        page = paginate_records(self.rows, limit=2)
        self.assertEqual([x["id"] for x in page["items"]], ["r1", "r2"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_cursor"], "r2")

    def test_second_page_excludes_cursor_and_detects_exact_end(self):
        page = paginate_records(self.rows, limit=2, after_id="r2")
        self.assertEqual([x["id"] for x in page["items"]], ["r3", "r4"])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])

    def test_after_last_item_returns_empty(self):
        page = paginate_records(self.rows, limit=2, after_id="r4")
        self.assertEqual(page["items"], [])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])

    def test_unknown_cursor_and_invalid_limit(self):
        with self.assertRaises(KeyError):
            paginate_records(self.rows, limit=2, after_id="missing")
        with self.assertRaises(ValueError):
            paginate_records(self.rows, limit=0)

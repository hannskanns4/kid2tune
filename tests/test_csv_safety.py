import unittest

from app.csv_safety import spreadsheet_safe_cell


class SpreadsheetSafeCellTests(unittest.TestCase):
    def test_formula_prefixes_are_neutralized(self):
        for value in ("=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", " \t=1+1"):
            with self.subTest(value=value):
                self.assertEqual(spreadsheet_safe_cell(value), "'" + value)

    def test_plain_values_are_preserved(self):
        for value in ("song title", "https://example.test/a", "1234", ""):
            with self.subTest(value=value):
                self.assertEqual(spreadsheet_safe_cell(value), value)

    def test_none_becomes_empty(self):
        self.assertEqual(spreadsheet_safe_cell(None), "")


if __name__ == "__main__":
    unittest.main()

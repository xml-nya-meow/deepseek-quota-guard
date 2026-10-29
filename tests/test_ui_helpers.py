import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from quota_guard.ui import amount_text, has_saved_data, parse_editor_person, safe_error


class HelperTests(unittest.TestCase):
    def test_special_values(self):
        self.assertEqual(parse_editor_person("", "-1").amount, Decimal(-1))
        self.assertEqual(parse_editor_person("某人", "0").amount, Decimal(0))
        for value in ("-2", "NaN", "Infinity", "", "=1+1"):
            with self.assertRaises(ValueError):
                parse_editor_person("某人", value)
        with self.assertRaises(ValueError):
            parse_editor_person("", "0")

    def test_precise_display(self):
        self.assertEqual(amount_text(Decimal("107.0001")), "107.0001")
        self.assertEqual(amount_text(None), "未知")

    def test_history_is_not_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            self.assertFalse(has_saved_data(path))
            (path / "运行日志-2026-10-29.jsonl").write_text("", encoding="utf8")
            self.assertTrue(has_saved_data(path))

    def test_error_redaction(self):
        self.assertNotIn("sk-secret123", safe_error("failed sk-secret123"))


if __name__ == "__main__":
    unittest.main()

"""Tests for process-friendly CSV metric output."""

import csv
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from metrics import CsvMetricsWriter


class CsvMetricsWriterTests(unittest.TestCase):
    def test_writes_one_header_and_processable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.csv"
            writer = CsvMetricsWriter(path)
            writer.write({"pipeline": "agent_to_remote", "capture_blocks": 10})
            writer.write({"pipeline": "remote_to_agent", "capture_blocks": 20})

            daily_path = path.with_name(f"metrics-{date.today().isoformat()}.csv")

            with daily_path.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pipeline"], "agent_to_remote")
        self.assertEqual(rows[1]["capture_blocks"], "20")
        self.assertTrue(rows[0]["timestamp"])

    def test_removes_only_expired_daily_metric_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.csv"
            writer = CsvMetricsWriter(path, retention_days=14)
            expired = path.with_name(
                f"metrics-{(date.today() - timedelta(days=14)).isoformat()}.csv"
            )
            retained = path.with_name(
                f"metrics-{(date.today() - timedelta(days=13)).isoformat()}.csv"
            )
            unrelated = Path(directory) / "metrics-manual.csv"
            for candidate in (expired, retained, unrelated):
                candidate.write_text("test", encoding="utf-8")

            writer.write({"pipeline": "agent_to_remote"})

            self.assertFalse(expired.exists())
            self.assertTrue(retained.exists())
            self.assertTrue(unrelated.exists())

    def test_rejects_unbounded_or_zero_retention(self) -> None:
        with self.assertRaises(ValueError):
            CsvMetricsWriter("metrics.csv", retention_days=0)
        with self.assertRaises(ValueError):
            CsvMetricsWriter("metrics.csv", retention_days=True)


if __name__ == "__main__":
    unittest.main()

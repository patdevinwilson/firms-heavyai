import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.collector import SOURCES, literal, parse_csv, replace_window

HEADER = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
ROW = "10,20,310,0.4,0.4,2026-10-09,5,N20,VIIRS,n,2.0NRT,290,12,D\n"


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)

    def parse(self, payload, source=SOURCES[0]):
        return parse_csv(payload, source, "raw/example.csv", self.start, self.start, self.end)

    def test_acquisition_time_is_zero_padded_utc(self):
        row = self.parse(HEADER + ROW)[0]
        self.assertEqual(row[2], "2026-10-09 00:05:00")
        self.assertEqual(row[5], "POINT(20.0 10.0)")

    def test_repeated_download_rows_are_deduplicated(self):
        self.assertEqual(len(self.parse(HEADER + ROW + ROW)), 1)

    def test_revision_keeps_identity(self):
        a = self.parse(HEADER + ROW)[0]
        b = self.parse(HEADER + ROW.replace("2.0NRT", "2.1NRT"))[0]
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[14], b[14])

    def test_nrt_wins_over_urt_in_duplicate_response(self):
        rows = self.parse(HEADER + ROW + ROW.replace("2.0NRT", "2.0URT"))
        self.assertEqual(rows[0][14], "2.0NRT")

    def test_header_only_response_is_valid(self):
        self.assertEqual(self.parse(HEADER), [])

    def test_error_payload_does_not_look_like_empty_success(self):
        with self.assertRaises(ValueError):
            self.parse("Invalid MAP_KEY")

    def test_bad_coordinates_and_measurements_are_rejected(self):
        for row in (ROW.replace("10,20,", "91,20,"), ROW.replace("310,", "nan,")):
            with self.assertRaises(ValueError):
                self.parse(HEADER + row)

    def test_outside_window_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(HEADER + ROW.replace("2026-10-09", "2026-10-08"))

    def test_invalid_confidence_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(HEADER + ROW.replace(",n,", ",85,"))

    def test_source_identity_is_preserved(self):
        a = self.parse(HEADER + ROW)[0]
        b = self.parse(HEADER + ROW, SOURCES[1])[0]
        self.assertNotEqual(a[0], b[0])

    def test_sql_literals_escape_quotes_and_reject_nan(self):
        self.assertEqual(literal("O'Brien"), "'O''Brien'")
        with self.assertRaises(ValueError):
            literal(float("nan"))

    def publish(self, db, rows):
        with tempfile.TemporaryDirectory() as directory:
            real_path = Path
            def path(value):
                return real_path(directory) if value == "/var/lib/heavyai/import" else real_path(value)
            with patch("app.collector.Path", side_effect=path):
                replace_window(db, SOURCES[0], self.start, self.end, rows)

    def test_failed_load_never_publishes_or_deletes_old_data(self):
        db = FakeDB(fail_copy=True)
        with self.assertRaises(RuntimeError):
            self.publish(db, self.parse(HEADER + ROW))
        self.assertFalse(any(s.startswith("INSERT INTO firms_published_windows") for s in db.sql))
        self.assertFalse(any(s.startswith("DELETE") for s in db.sql))

    def test_partial_load_keeps_previous_publication(self):
        db = FakeDB(loaded=0)
        with self.assertRaises(RuntimeError):
            self.publish(db, self.parse(HEADER + ROW))
        self.assertFalse(any(s.startswith("DELETE") for s in db.sql))

    def test_publish_precedes_cleanup(self):
        db = FakeDB()
        self.publish(db, self.parse(HEADER + ROW))
        publication = next(i for i, s in enumerate(db.sql) if s.startswith("INSERT INTO firms_published_windows"))
        cleanup = next(i for i, s in enumerate(db.sql) if s.startswith("DELETE"))
        self.assertLess(publication, cleanup)

    def test_empty_response_publishes_replacement(self):
        db = FakeDB()
        self.publish(db, [])
        self.assertFalse(any(s.startswith("COPY") for s in db.sql))
        self.assertTrue(any(s.startswith("INSERT INTO firms_published_windows") for s in db.sql))


class FakeDB:
    def __init__(self, fail_copy=False, loaded=1):
        self.sql = []
        self.fail_copy = fail_copy
        self.loaded = loaded

    def execute(self, sql):
        self.sql.append(sql)
        if self.fail_copy and sql.startswith("COPY"):
            raise RuntimeError("Injected load failure")
        return self

    def fetchone(self):
        return (self.loaded,)


if __name__ == "__main__":
    unittest.main()

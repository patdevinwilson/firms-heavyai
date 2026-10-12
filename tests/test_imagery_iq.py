import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.imagery import descriptor, fetch_image, granule_search
from app.iq_adapter import clean, messages, Handler
from app.server import record_query
from scripts.evaluate_iq import read_only


class ImageryTests(unittest.TestCase):
    def test_satellite_and_daynight_provenance(self):
        for satellite, sensor in (("N", "SNPP"), ("N20", "NOAA20"), ("N21", "NOAA21")):
            day = descriptor(satellite, "2026-10-08 12:35:00", 45, 12, "D")
            night = descriptor(satellite, "2026-10-08 00:35:00", 45, 12, "N")
            self.assertEqual(day["layer"], f"VIIRS_{sensor}_CorrectedReflectance_TrueColor")
            self.assertEqual(night["layer"], f"VIIRS_{sensor}_Brightness_Temp_BandI5_Night")
            self.assertEqual(day["date"], "2026-10-08")
            self.assertIn("exact overpass", day["provenance"])

    def test_shared_tile_and_polar_dateline_bounds(self):
        first = descriptor("N20", "2026-01-01", 45.01, 12.01, "D")
        second = descriptor("N20", "2026-01-01", 45.02, 12.02, "D")
        self.assertEqual(first["cache_key"], second["cache_key"])
        for lat, lon in ((90, 180), (-90, -180)):
            info = descriptor("N21", "2026-01-01", lat, lon, "N")
            west, south, east, north = info["bbox"]
            self.assertTrue(-180 <= west < east <= 180)
            self.assertTrue(-90 <= south < north <= 90)

    def test_cache_avoids_duplicate_downloads(self):
        info = descriptor("N", "2026-01-01", 0, 0, "D")
        data = b"\x89PNG\r\n\x1a\n" + b"fixture"
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.imagery.urlopen") as opener:
                opener.return_value.__enter__.return_value.read.return_value = data
                self.assertEqual(fetch_image(info, Path(directory)), data)
                self.assertEqual(fetch_image(info, Path(directory)), data)
                self.assertEqual(opener.call_count, 1)

    def test_xml_service_error_is_not_cached_as_image(self):
        info = descriptor("N", "2026-01-01", 0, 0, "D")
        with tempfile.TemporaryDirectory() as directory, patch("app.imagery.urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b"<ServiceException/>"
            with self.assertRaises(ValueError):
                fetch_image(info, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_record_pagination_and_filter_validation(self):
        self.assertIn("OFFSET 100", record_query({"lon": ["12"], "lat": ["45"], "offset": ["100"]}))
        for params in ({"lon": ["12 OR 1=1"], "lat": ["45"]},
                       {"lon": ["180"], "lat": ["45"]},
                       {"lon": ["12"], "lat": ["45"], "offset": ["-1"]}):
            with self.assertRaises(ValueError):
                record_query(params)

    def test_granules_must_overlap_acquisition_minute(self):
        entries = [{"id": "match", "time_start": "2026-01-01T12:00:00Z",
                    "time_end": "2026-01-01T12:06:00Z", "links": [
                    {"href": "https://example.nasa.gov/browse.png", "rel": "http://esipfed.org/ns/fedsearch/1.1/browse#"},
                    {"href": "javascript:bad", "rel": "browse"}]},
                   {"id": "earlier", "time_start": "2026-01-01T11:54:00Z",
                    "time_end": "2026-01-01T12:00:00Z"}]
        with patch("app.imagery.urlopen") as opener:
            opener.return_value.__enter__.return_value = io.BytesIO(json.dumps({"feed": {"entry": entries}}).encode())
            result = granule_search("N20", "2026-01-01 12:03:00", 45, 12)
        self.assertEqual([r["id"] for r in result["granules"]], ["match"])
        self.assertEqual(len(result["granules"][0]["links"]), 1)
        self.assertIn("VJ102IMG_NRT", result["search_url"])


class IQTests(unittest.TestCase):
    def test_completion_bridge_returns_native_wire_shape(self):
        from unittest.mock import Mock
        for stream in (False, True):
            request = {"prompt": "<|sql prompt|>\nCount rows\n<|sql answer|>\n", "stream": stream}
            body = json.dumps(request).encode()
            handler = Handler.__new__(Handler)
            handler.path = "/v1/completions"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
            handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
            result = {"choices": [{"message": {"content": "```sql\nSELECT 1\n```"}, "finish_reason": "stop"}],
                      "usage": {"total_tokens": 10}}
            with patch("app.iq_adapter.urlopen") as opener:
                opener.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
                handler.do_POST()
                sent = json.loads(opener.call_args.args[0].data)
                self.assertFalse(sent["stream"])
                self.assertFalse(sent["chat_template_kwargs"]["enable_thinking"])
            output = handler.wfile.getvalue().decode()
            if stream:
                self.assertIn("data: [DONE]", output)
                output = output.split("data: ", 1)[1].split("\n\n", 1)[0]
            value = json.loads(output)
            self.assertEqual(value["object"], "text_completion")
            self.assertEqual(value["choices"][0]["text"], "SELECT 1")
            self.assertEqual(value["usage"]["total_tokens"], 10)

    def test_native_task_markers_are_translated(self):
        for marker in ("sql", "sql error", "english", "table", "table question", ""):
            prefix = marker + " " if marker else ""
            task, chat = messages(f"<|{prefix}prompt|>\nSchema and question\n<|{prefix}answer|>\n")
            self.assertEqual(task, marker)
            self.assertEqual(chat[1]["content"], "Schema and question")
            self.assertIn("HeavySQL", chat[0]["content"])

    def test_reasoning_and_sql_fences_removed(self):
        self.assertEqual(clean("<think>private</think>\n```sql\nSELECT 1\n```", "sql"), "SELECT 1")

    def test_unrecognized_task_rejected(self):
        with self.assertRaises(ValueError):
            messages("<|unknown prompt|>\nquestion")

    def test_evaluation_never_executes_mutation(self):
        for sql in ("DROP TABLE x", "SELECT 1; DELETE FROM x", "COPY x TO 'file'"):
            with self.assertRaises(ValueError):
                read_only(sql)
        self.assertEqual(read_only("SELECT COUNT(*) FROM x;"), "SELECT COUNT(*) FROM x")


if __name__ == "__main__":
    unittest.main()

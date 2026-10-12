"""SSM-accessible read-only demo. Query inputs are bounded and allowlisted."""
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from app.collector import HeavyDB, settings
from app.imagery import descriptor, fetch_image, granule_search

ROOT = Path(__file__).resolve().parents[1]


def map_query(params):
    hours = int(params.get("hours", ["24"])[0])
    if hours not in {6, 24, 72, 168}:
        raise ValueError("Unsupported time window")
    confidence = params.get("confidence", ["nh"])[0]
    filters = {"nh": "confidence IN ('n','h')", "h": "confidence='h'", "all": "1=1"}
    if confidence not in filters:
        raise ValueError("Unsupported confidence filter")
    satellite = params.get("satellite", ["all"])[0]
    satellites = {"all": "1=1", "N20": "satellite='N20'", "N21": "satellite='N21'",
                  "N": "satellite='N'"}
    if satellite not in satellites:
        raise ValueError("Unsupported satellite")
    where = (f"acquired_at >= DATEADD('hour', -{hours}, CURRENT_TIMESTAMP) "
             f"AND {filters[confidence]} AND {satellites[satellite]}")
    sql = ("SELECT FLOOR(longitude) + 0.5 AS lon, FLOOR(latitude) + 0.5 AS lat, "
           "COUNT(*) AS detections, MAX(acquired_at) AS latest_observation "
           f"FROM firms_observations WHERE {where} GROUP BY lon, lat ORDER BY detections DESC")
    return sql


def record_query(params):
    # Reuse the same validated time/confidence/satellite predicates as the map.
    where = map_query(params).split(" WHERE ", 1)[1].split(" GROUP BY ", 1)[0]
    lon, lat = int(params["lon"][0]), int(params["lat"][0])
    offset = int(params.get("offset", ["0"])[0])
    if not -180 <= lon <= 179 or not -90 <= lat <= 89 or not 0 <= offset <= 100000:
        raise ValueError("Invalid cell/page")
    return ("SELECT observation_id, acquired_at, latitude, longitude, satellite, "
            "confidence, frp, daynight FROM firms_observations WHERE " + where
            + f" AND FLOOR(longitude)={lon} AND FLOOR(latitude)={lat} "
            + f"ORDER BY acquired_at DESC, observation_id LIMIT 100 OFFSET {offset}")


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, content_type="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.reply((ROOT / "app/index.html").read_text(), "text/html; charset=utf-8")
        if parsed.path not in {"/api/activity", "/api/records", "/api/imagery", "/api/granule"}:
            return self.reply('{"error":"Not found"}', status=404)
        try:
            params = parse_qs(parsed.query)
            if parsed.path in {"/api/imagery", "/api/granule"}:
                observation_id = params["id"][0]
                if not re.fullmatch(r"[0-9a-f]{64}", observation_id):
                    raise ValueError("Invalid observation ID")
                sql = ("SELECT satellite, acquired_at, latitude, longitude, daynight "
                       "FROM firms_observations WHERE observation_id='" + observation_id + "' LIMIT 1")
            else:
                sql = record_query(params) if parsed.path == "/api/records" else map_query(params)
        except (ValueError, TypeError, KeyError):
            return self.reply('{"error":"Invalid filters"}', status=400)
        try:
            config, secret = settings()
            db = HeavyDB(secret, user="firms_reader", password=secret["reader_password"])
            try:
                points = db.execute(sql).fetchall()
                if parsed.path in {"/api/imagery", "/api/granule"}:
                    if not points:
                        return self.reply('{"error":"Observation not found"}', status=404)
                    info = descriptor(*points[0])
                elif parsed.path == "/api/records":
                    records = []
                    for row in points:
                        item = dict(zip(["id", "acquired_at", "latitude", "longitude", "satellite",
                                         "confidence", "frp", "daynight"], row))
                        item["imagery"] = descriptor(row[4], row[1], row[2], row[3], row[7])
                        item["imagery"]["popup_url"] = "/api/imagery?id=" + row[0]
                        records.append(item)
                else:
                    runs = db.execute("SELECT source, MAX(completed_at) AS last_success, "
                        "MAX(latest_observation_at) AS last_observation FROM firms_collection_runs "
                        "WHERE status='success' GROUP BY source").fetchall()
            finally:
                db.close()
            if parsed.path == "/api/imagery":
                return self.reply(fetch_image(info), "image/png")
            if parsed.path == "/api/granule":
                return self.reply(json.dumps(granule_search(*points[0][:4])))
            if parsed.path == "/api/records":
                return self.reply(json.dumps({"records": records, "page_size": 100}, default=str))
            payload = {"cells": points, "freshness": runs, "sql": sql,
                       "generated_at": datetime.now(timezone.utc).isoformat(),
                       "heavyiq_configured": config["heavyiq_required"]}
            self.reply(json.dumps(payload, default=str))
        except Exception:
            self.reply('{"error":"Analytics unavailable. Check deployment status."}', status=503)

    def log_message(self, format, *args):
        # Avoid logging user query strings or database error details.
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()

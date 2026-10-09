"""SSM-accessible read-only demo. Query inputs are bounded and allowlisted."""
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from app.collector import HeavyDB, settings

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


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, content_type="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.reply((ROOT / "app/index.html").read_text(), "text/html; charset=utf-8")
        if parsed.path != "/api/activity":
            return self.reply('{"error":"Not found"}', status=404)
        try:
            sql = map_query(parse_qs(parsed.query))
        except (ValueError, TypeError):
            return self.reply('{"error":"Invalid filters"}', status=400)
        try:
            config, secret = settings()
            db = HeavyDB(secret, user="firms_reader", password=secret["reader_password"])
            try:
                points = db.execute(sql).fetchall()
                runs = db.execute("SELECT source, MAX(completed_at) AS last_success, "
                    "MAX(latest_observation_at) AS last_observation FROM firms_collection_runs "
                    "WHERE status='success' GROUP BY source").fetchall()
            finally:
                db.close()
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
    HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()

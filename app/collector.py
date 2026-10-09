"""Global FIRMS collection. Atomic source/window replacement reconciles revisions."""
import csv
import hashlib
import io
import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCES = ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT")
ROOT = Path(__file__).resolve().parents[1]


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (float, int)):
        if not math.isfinite(value):
            raise ValueError("Non-finite number")
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def parse_csv(payload, source, raw_key, now, start, end):
    """Reject bad/error payloads before modifying the currently visible window."""
    if source not in SOURCES:
        raise ValueError("Unsupported source")
    reader = csv.DictReader(io.StringIO(payload))
    required = {"latitude", "longitude", "acq_date", "acq_time", "satellite",
                "confidence", "frp", "bright_ti4", "bright_ti5", "scan",
                "track", "daynight", "version"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("FIRMS response does not contain the expected VIIRS CSV header")
    rows = {}
    for row in reader:
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if not (math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates")
        acquired = datetime.strptime(row["acq_date"] + row["acq_time"].zfill(4),
                                     "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc)
        if not start <= acquired < end:
            raise ValueError("Observation outside requested window")
        if row["confidence"] not in {"l", "n", "h"} or row["daynight"] not in {"D", "N"}:
            raise ValueError("Invalid VIIRS category")
        # Product version deliberately excluded: a revision updates this observation.
        identity = "|".join((source, row["satellite"], timestamp(acquired),
                             format(lat, ".8f"), format(lon, ".8f")))
        oid = hashlib.sha256(identity.encode()).hexdigest()
        numbers = [float(row[name]) for name in
                   ("frp", "bright_ti4", "bright_ti5", "scan", "track")]
        if not all(math.isfinite(n) for n in numbers):
            raise ValueError("Invalid measurement")
        value = (oid, source, timestamp(acquired), lat, lon,
                 f"POINT({lon} {lat})", row["satellite"], row["confidence"],
                 *numbers, row["daynight"], row["version"], timestamp(now), raw_key)
        previous = rows.get(oid)
        # Prefer NRT over RT/URT if the response contains duplicate processing versions.
        rank = lambda item: ("NRT" in item[14], "URT" not in item[14], item[14])
        if previous is None or rank(value) >= rank(previous):
            rows[oid] = value
    return list(rows.values())


class HeavyDB:
    def __init__(self, credentials, user="admin", password=None):
        from heavyai import connect
        self.connection = connect(user=user,
                                  password=password or credentials["heavyai_password"],
                                  host="127.0.0.1", port=6274, dbname="heavyai")

    def execute(self, sql):
        return self.connection.cursor().execute(sql)

    def close(self):
        self.connection.close()


def settings():
    import boto3
    config = json.loads((ROOT / "config.json").read_text())
    response = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"]).get_secret_value(
        SecretId=os.environ["SECRET_ARN"])
    return config, json.loads(response["SecretString"])


def insert_rows(db, table, rows, batch_size=250):
    for offset in range(0, len(rows), batch_size):
        values = ["(" + ",".join(literal(v) for v in row) + ")"
                  for row in rows[offset:offset + batch_size]]
        db.execute(f"INSERT INTO {table} VALUES " + ",".join(values))


def replace_window(db, source, start, end, rows):
    """Load an invisible batch, then publish all days in one INSERT statement.

    No multi-statement transaction required. Failed loads leave the prior published
    batch visible. The single-writer lock makes batch IDs monotonic on this host.
    """
    batch = utcnow().strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex
    path = Path("/var/lib/heavyai/import") / (batch + ".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows(tuple(row) + (batch,) for row in rows)
    try:
        if rows:
            db.execute("COPY firms_observation_batches FROM " + literal(str(path))
                       + " WITH (header='false', max_reject=0)")
            loaded = db.execute("SELECT COUNT(*) FROM firms_observation_batches WHERE batch_id="
                                + literal(batch)).fetchone()[0]
            if loaded != len(rows):
                raise RuntimeError("Incomplete COPY; prior batch remains published")
        published = []
        day = start
        while day < end:
            published.append((source, day.date().isoformat(), batch))
            day += timedelta(days=1)
        insert_rows(db, "firms_published_windows", published)
        # Once publication succeeds, older and abandoned batches in this window
        # can be removed. Repeated observations across satellites remain distinct.
        db.execute("DELETE FROM firms_observation_batches WHERE source=" + literal(source)
                   + " AND acquired_at >= " + literal(timestamp(start))
                   + " AND acquired_at < " + literal(timestamp(end))
                   + " AND batch_id <> " + literal(batch))
    finally:
        path.unlink(missing_ok=True)


def fetch(session, map_key, source, start, days):
    import requests
    url = ("https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
           f"{map_key}/{source}/world/{days}/{start.date().isoformat()}")
    for attempt in range(4):
        try:
            response = session.get(url, timeout=(15, 180))
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError("Retryable FIRMS response")
            if response.status_code != 200:
                raise ValueError(f"FIRMS HTTP {response.status_code}")
            return response.text
        except (requests.RequestException, RuntimeError):
            # Never log exception strings: requests includes the MAP_KEY-bearing URL.
            if attempt == 3:
                raise RuntimeError("FIRMS request failed after retries") from None
            time.sleep(15 * 2 ** attempt)


def run():
    import boto3
    import requests
    config, secret = settings()
    days = int(config["lookback_days"])
    if not 1 <= days <= 5:
        raise ValueError("lookback_days must be 1..5")
    s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
    db = HeavyDB(secret)
    failures = []
    try:
        for source in config["sources"]:
            if source not in SOURCES:
                raise ValueError("Unsupported source")
            began = utcnow()
            # Fixed start date avoids midnight races in FIRMS relative-date requests.
            end = began.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            start = end - timedelta(days=days)
            run_id = uuid.uuid4().hex
            key = f"raw/{source}/{began:%Y/%m/%d}/{run_id}.csv"
            try:
                with requests.Session() as session:
                    payload = fetch(session, secret["firms_map_key"], source, start, days)
                rows = parse_csv(payload, source, key, began, start, end)
                s3.put_object(Bucket=os.environ["ARCHIVE_BUCKET"], Key=key,
                              Body=payload.encode(), ContentType="text/csv")
                replace_window(db, source, start, end, rows)
                latest = max((r[2] for r in rows), default=None)
                insert_rows(db, "firms_collection_runs", [(run_id, source, timestamp(began),
                            timestamp(utcnow()), "success", len(rows), latest, key)])
                print(json.dumps({"source": source, "rows": len(rows), "status": "success"}), flush=True)
            except Exception:
                failures.append(source)
                insert_rows(db, "firms_collection_runs", [(run_id, source, timestamp(began),
                            timestamp(utcnow()), "failed", 0, None, key)])
                print(json.dumps({"source": source, "status": "failed"}), flush=True)
        cutoff = utcnow() - timedelta(days=int(config["retain_days"]))
        db.execute("DELETE FROM firms_observation_batches WHERE acquired_at < " + literal(timestamp(cutoff)))
        db.execute("DELETE FROM firms_published_windows WHERE window_day < " + literal(cutoff.date().isoformat()))
    finally:
        db.close()
    if failures:
        raise RuntimeError("Collection failed for " + ", ".join(failures))


if __name__ == "__main__":
    # Serializes collectors and the bundled dashboard, including timer invocations.
    import fcntl
    with open("/var/lib/heavyai/firms.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run()

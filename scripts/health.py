import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import HeavyDB, settings


def main():
    config, secret = settings()
    db = HeavyDB(secret, user="firms_reader", password=secret["reader_password"])
    try:
        counts = db.execute("SELECT COUNT(*) FROM firms_observations").fetchone()[0]
        runs = db.execute("SELECT source, MAX(completed_at) FROM firms_collection_runs "
                          "WHERE status='success' GROUP BY source").fetchall()
    finally:
        db.close()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh = {source for source, completed in runs if completed and 0 <= (now - completed).total_seconds() < 7200}
    missing = set(config["sources"]) - fresh
    report = {"database": "ready", "observations": counts,
              "collection": "fresh" if not missing else "stale", "stale_sources": sorted(missing),
              "heavyiq": "configured, generation not yet verified" if config["heavyiq_required"] else "disabled"}
    print(json.dumps(report))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

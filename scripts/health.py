import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

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
    iq = "disabled"
    if config["heavyiq_required"]:
        try:
            with urlopen("http://127.0.0.1:6275/openapi.json", timeout=5) as response:
                json.load(response)
            iq = "API ready; SQL evaluation pending"
            evaluation = ROOT / ".iq-evaluation.json"
            if evaluation.exists():
                result = json.loads(evaluation.read_text())
                iq = f"API ready; smoke benchmark {result['passed']}/{result['total']}"
        except Exception:
            iq = "unavailable"
    report = {"database": "ready", "observations": counts,
              "collection": "fresh" if not missing else "stale", "stale_sources": sorted(missing),
              "heavyiq": iq}
    print(json.dumps(report))
    return 1 if missing or iq == "unavailable" else 0


if __name__ == "__main__":
    sys.exit(main())

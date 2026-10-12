"""Evaluate native HeavyIQ SQL execution against a nonempty deterministic fixture.

Run on the deployment host. Creates a temporary fixture using admin; generated
SQL is executed only using firms_reader. Cleans up fixture in finally.
"""
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import HeavyDB, settings


def read_only(sql):
    # Conservative benchmark gate; database privileges are the enforcement layer.
    text = sql.strip().rstrip(";")
    if not re.match(r"^SELECT\b", text, re.I) or ";" in text or re.search(
            r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|COPY|GRANT|REVOKE)\b", text, re.I):
        raise ValueError("Only a single SELECT is allowed")
    return text


def canonical(rows):
    return sorted([tuple(str(v) for v in row) for row in rows], key=str)


def main():
    _, secret = settings()
    admin = HeavyDB(secret)
    reader = None
    table = "firms_iq_eval_" + uuid.uuid4().hex[:12]
    reports = []
    try:
        admin.execute(f"CREATE TABLE {table} (acquired_at TIMESTAMP, satellite TEXT ENCODING DICT(8), "
                      "confidence TEXT ENCODING DICT(8), frp DOUBLE, longitude DOUBLE, latitude DOUBLE)")
        admin.execute(f"INSERT INTO {table} VALUES "
            "('2026-01-01 23:50:00','N20','h',10,12,45),"
            "('2026-01-02 00:10:00','N21','n',20,-77,38),"
            "('2026-01-02 02:00:00','N','l',30,179.9,-10),"
            "('2026-01-02 12:00:00','N20','h',40,12.1,45.1)")
        admin.execute(f"GRANT SELECT ON TABLE {table} TO firms_reader")
        reader = HeavyDB(secret, user="firms_reader", password=secret["reader_password"])
        cases = [
            ("Count high-confidence detections. Return one count column.",
             f"SELECT COUNT(*) FROM {table} WHERE confidence='h'"),
            ("Count nominal and high confidence detections. Return one count column.",
             f"SELECT COUNT(*) FROM {table} WHERE confidence IN ('n','h')"),
            ("List satellite and detection count per satellite.",
             f"SELECT satellite, COUNT(*) FROM {table} GROUP BY satellite"),
            ("List UTC day and detection count per day.",
             f"SELECT DATE_TRUNC('day',acquired_at), COUNT(*) FROM {table} GROUP BY 1"),
            ("Sum FRP for high confidence NOAA-20 (satellite N20) detections; one sum column.",
             f"SELECT SUM(frp) FROM {table} WHERE confidence='h' AND satellite='N20'"),
            ("Count detections from 2026-01-02 00:00:00 UTC inclusive to 2026-01-03 00:00:00 UTC exclusive.",
             f"SELECT COUNT(*) FROM {table} WHERE acquired_at >= TIMESTAMP '2026-01-02 00:00:00' "
             "AND acquired_at < TIMESTAMP '2026-01-03 00:00:00'"),
        ]
        for question, gold in cases:
            report = {"question": question, "gold_sql": gold, "passed": False}
            try:
                request = Request("http://127.0.0.1:6275/api/v1/lcel/query", headers={
                    "Content-Type": "application/json"}, data=json.dumps({"question": question,
                    "tables": [table], "session_id": reader.connection._session}).encode())
                with urlopen(request, timeout=300) as response:
                    result = json.load(response)
                sql = read_only(result["sql"])
                report["generated_sql"] = sql
                report["passed"] = canonical(reader.execute(sql).fetchall()) == canonical(admin.execute(gold).fetchall())
            except Exception as exc:
                report["error_type"] = type(exc).__name__
            reports.append(report)
    finally:
        if reader:
            reader.close()
        try:
            admin.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            admin.close()
    output = {"model": "NVIDIA-Nemotron-3-Nano-4B-Q4_K_M", "cases": reports,
              "passed": sum(r["passed"] for r in reports), "total": len(reports),
              "scope": "smoke benchmark only; no claim of general HeavySQL accuracy"}
    (ROOT / ".iq-evaluation.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))
    return 0 if reports and all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())

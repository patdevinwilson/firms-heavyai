"""Optional daily Fable briefing grounded in predefined HeavyDB queries."""
import json
import os
from pathlib import Path
from app.collector import HeavyDB, settings, timestamp, utcnow

ROOT = Path(__file__).resolve().parents[1]


def run():
    import boto3
    import requests
    config, secret = settings()
    if not config.get("fable_model") or not secret.get("anthropic_api_key"):
        raise ValueError("Configure a supported Fable API model ID and ANTHROPIC_API_KEY")
    db = HeavyDB(secret, user="firms_reader", password=secret["reader_password"])
    evidence = []
    try:
        for number, query in enumerate((ROOT / "sql/demo.sql").read_text().split(";"), 1):
            if query.strip():
                evidence.append({"id": f"Q{number}", "sql": query.strip(),
                                 "rows": db.execute(query).fetchall()})
    finally:
        db.close()
    prompt = ("Write a concise global thermal-activity briefing using only the supplied query results. "
              "Cite evidence IDs [Q1], [Q2], etc beside quantitative claims. State collection freshness "
              "and observation freshness separately. Treat missing data as unknown, not inactivity. "
              "Do not infer fire cause, intent, distinct fire count, threats or coverage-adjusted anomalies. "
              "Identify limitations and useful follow-up questions. Query data:\n"
              + json.dumps(evidence, default=str))
    try:
        response = requests.post("https://api.anthropic.com/v1/messages", timeout=(15, 300),
            headers={"x-api-key": secret["anthropic_api_key"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config["fable_model"], "max_tokens": 1500,
                  "messages": [{"role": "user", "content": prompt}]})
        if response.status_code != 200:
            raise RuntimeError("Fable API request failed")
        result = response.json()
    except requests.RequestException:
        raise RuntimeError("Fable API unavailable") from None
    text = "\n".join(item["text"] for item in result["content"] if item["type"] == "text")
    now = utcnow()
    document = {"generated_at": timestamp(now), "model": config["fable_model"],
                "briefing": text, "evidence": evidence}
    key = f"briefings/{now:%Y/%m/%d}/{now:%H%M%S}.json"
    boto3.client("s3", region_name=os.environ["AWS_REGION"]).put_object(
        Bucket=os.environ["ARCHIVE_BUCKET"], Key=key,
        Body=json.dumps(document, default=str).encode(), ContentType="application/json")
    print("Briefing and query evidence archived: " + key)


if __name__ == "__main__":
    run()

"""Verify native Guidance CRUD, reader permissions and real prompt use.

Run on the deployed host. The temporary probe snippet is deleted on exit.
"""
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.collector import HeavyDB, settings
from scripts.evaluate_iq import canonical, read_only
from scripts.iq_guidance import GuidanceStore


def main():
    _, secret = settings()
    admin = HeavyDB(secret)
    reader = HeavyDB(secret, user='firms_reader', password=secret['reader_password'])
    probe_ids = []

    def call(path, payload, db=admin):
        request = Request('http://127.0.0.1:6275' + path,
            data=json.dumps({**payload, 'session_id': db.connection._session}).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=300) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            # Never print failed requests or response bodies containing sessions.
            return exc.code, None

    def ok(path, payload, db=admin):
        status, result = call(path, payload, db)
        if status != 200:
            raise RuntimeError(f'Guidance check failed: {path} returned {status}')
        return result

    try:
        initial = ok('/rag/snippets/list', {})['snippets']
        assert initial == GuidanceStore().list('heavyai')
        assert ok('/rag/snippets/list', {}, reader)['snippets'] == initial
        denied, _ = call('/rag/snippets/insert', {'snippet': 'Unauthorized probe'}, reader)
        assert denied == 403, 'Reader must not change Guidance'
        probe_ids = ok('/rag/snippets/bulk-insert', {'snippets': [
            "When a question contains guidance_probe, name its count column guidance_count_before_edit."]})['snippet_ids']
        ok('/rag/snippets/update', {'snippet_id': probe_ids[0], 'snippet':
            "When a question contains guidance_probe, name its count column guidance_count_after_edit."})
        assert 'after_edit' in ok('/rag/snippets/get', {'snippet_id': probe_ids[0]})['snippet']
        result = ok('/api/v1/lcel/query', {
            'question': 'For guidance_probe, count detections in firms_observations across all available dates. Return one total, using the column name specified by database guidance.',
            'tables': ['firms_observations']}, reader)
        sql = read_only(result['sql'])
        assert 'guidance_count_after_edit' in sql.lower(), 'Edited Guidance was not followed: ' + sql
        expected_ids = {s['snippet_id'] for s in initial} | set(probe_ids)
        assert set(result['snippet_ids']) == expected_ids, 'UI Guidance Used IDs are missing'
        actual = reader.execute(sql).fetchall()
        gold = reader.execute('SELECT COUNT(*) FROM firms_observations').fetchall()
        assert canonical(actual) == canonical(gold), 'Guided query result did not match database'
        print(json.dumps({'guidance_snippets': len(initial), 'reader_can_list': True,
            'reader_edits_denied': True, 'administrator_crud': True,
            'edited_guidance_used_by_model': True, 'guidance_ids_returned': len(expected_ids),
            'sql': sql, 'matches_live_database': True}))
    finally:
        try:
            if probe_ids:
                ok('/rag/snippets/delete', {'snippet_ids': probe_ids})
                assert ok('/rag/snippets/list', {})['snippets'] == initial
        finally:
            reader.close()
            admin.close()


if __name__ == '__main__':
    main()

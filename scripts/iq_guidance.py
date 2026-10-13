"""Persistent, database-scoped Guidance for native HeavyIQ without vector RAG.

Mounted into the source-built HeavyIQ runtime as firms_guidance. All snippets
are included in each SQL/table prompt; no embeddings or relevance claims.
"""
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

STORE = Path('/var/lib/heavyai/iq-guidance.sqlite3')
MAX_BYTES = 6000


class GuidanceStore:
    def __init__(self, path=STORE):
        self.path = Path(path)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self, database, snippets):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS initialized (database_name TEXT PRIMARY KEY)')
            db.execute('''CREATE TABLE IF NOT EXISTS snippets (
                snippet_id TEXT PRIMARY KEY, database_name TEXT NOT NULL,
                snippet TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(database_name, snippet))''')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM initialized WHERE database_name=?', (database,)).fetchone():
                self._insert(db, database, snippets)
                db.execute('INSERT INTO initialized VALUES (?)', (database,))

    @staticmethod
    def _validate(db, database, snippets, replacing=None):
        if not snippets or any(not isinstance(s, str) or not s.strip() for s in snippets):
            raise ValueError('Guidance snippets must contain text')
        rows = db.execute('SELECT snippet_id, snippet FROM snippets WHERE database_name=?', (database,))
        size = sum(len(row['snippet'].encode()) for row in rows if row['snippet_id'] != replacing)
        if size + sum(len(s.encode()) for s in snippets) > MAX_BYTES:
            raise ValueError('Guidance exceeds the 6000-byte prompt budget')

    def _insert(self, db, database, snippets):
        self._validate(db, database, snippets)
        ids = [uuid.uuid4().hex for _ in snippets]
        db.executemany('INSERT INTO snippets (snippet_id, database_name, snippet) VALUES (?,?,?)',
                       [(id_, database, text) for id_, text in zip(ids, snippets)])
        return ids

    def list(self, database):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('''SELECT snippet_id, snippet, created_at, updated_at
                FROM snippets WHERE database_name=? ORDER BY rowid''', (database,))]

    def insert(self, database, snippets):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            return self._insert(db, database, snippets)

    def update(self, database, id_, text):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._validate(db, database, [text], replacing=id_)
            result = db.execute('''UPDATE snippets SET snippet=?, updated_at=CURRENT_TIMESTAMP
                WHERE database_name=? AND snippet_id=?''', (text, database, id_))
            if not result.rowcount:
                raise KeyError(id_)
            return id_

    def delete(self, database, ids):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not ids or len(ids) != len(set(ids)):
                raise ValueError('Provide unique snippet IDs')
            for id_ in ids:
                result = db.execute('DELETE FROM snippets WHERE database_name=? AND snippet_id=?',
                                    (database, id_))
                if not result.rowcount:
                    raise KeyError(id_)
            return True

    def relevant_info(self, database):
        snippets = self.list(database)
        return {'snippet_ids': [s['snippet_id'] for s in snippets],
                'relevant_info': 'Database guidance:\n' + '\n'.join(s['snippet'] for s in snippets)
                if snippets else ''}


async def relevant_info(inputs):
    from heavyiq.langchain.heavydb import get_db
    from fastapi.concurrency import run_in_threadpool
    db = await get_db(inputs['session_id'])
    return await run_in_threadpool(GuidanceStore().relevant_info, db._dbname)


def register(app):
    from fastapi import APIRouter
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from heavyiq.langchain.heavydb import get_db
    from heavyiq.rag import models as md

    router = APIRouter(prefix='/rag/snippets', tags=['guidance'])

    async def perform(request, operation, mutate=False):
        db = await get_db(request.session_id)

        def run():
            # Revalidate even cached sessions and keep native API permissions
            # consistent with the authenticated webserver's Guidance proxy.
            conn = db._conn
            info = conn._client.get_session_info(request.session_id)
            if mutate and not info.is_super:
                roles = conn._client.get_all_effective_roles_for_user(request.session_id, info.user)
                if 'immerse_guidance_manage' not in roles:
                    return JSONResponse(status_code=403, content={'error': 'Guidance management permission required'})
            try:
                return operation(GuidanceStore(), info.database)
            except KeyError:
                return JSONResponse(status_code=404, content={'error': 'Snippet not found in this database'})
            except (ValueError, sqlite3.IntegrityError) as exc:
                return JSONResponse(status_code=400, content={'error': str(exc)})
        return await run_in_threadpool(run)

    @router.post('/list')
    async def list_snippets(request: md.ListSnippetsRequest):
        return await perform(request, lambda store, database: {'snippets': store.list(database)})

    @router.post('/get')
    async def get_snippet(request: md.GetSnippetRequest):
        def get(store, database):
            for snippet in store.list(database):
                if snippet['snippet_id'] == request.snippet_id:
                    return snippet
            raise KeyError(request.snippet_id)
        return await perform(request, get)

    @router.post('/bulk-insert')
    async def insert_snippets(request: md.BulkInsertSnippetsRequest):
        return await perform(request, lambda store, database:
            {'snippet_ids': store.insert(database, request.snippets)}, mutate=True)

    @router.post('/insert')
    async def insert_snippet(request: md.AddSnippetRequest):
        return await perform(request, lambda store, database:
            {'snippet_id': store.insert(database, [request.snippet])[0]}, mutate=True)

    @router.post('/update')
    async def update_snippet(request: md.UpdateSnippetRequest):
        return await perform(request, lambda store, database:
            {'snippet_id': store.update(database, request.snippet_id, request.snippet)}, mutate=True)

    @router.post('/delete')
    async def delete_snippets(request: md.DeleteSnippetsRequest):
        return await perform(request, lambda store, database:
            {'deleted': store.delete(database, request.snippet_ids)}, mutate=True)

    app.include_router(router)

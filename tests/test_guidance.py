import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.iq_guidance import GuidanceStore, MAX_BYTES


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'guidance.sqlite3'
        self.store = GuidanceStore(self.path)
        self.store.initialize('heavyai', ['Original domain rule', 'Second rule'])

    def test_edits_and_deletions_survive_redeployment(self):
        first, second = self.store.list('heavyai')
        self.store.update('heavyai', first['snippet_id'], 'Edited domain rule')
        self.store.delete('heavyai', [second['snippet_id']])
        restarted = GuidanceStore(self.path)
        restarted.initialize('heavyai', ['Original domain rule', 'Second rule', 'New seed rule'])
        info = restarted.relevant_info('heavyai')
        self.assertEqual(info['snippet_ids'], [first['snippet_id']])
        self.assertIn('Edited domain rule', info['relevant_info'])
        self.assertNotIn('Original domain rule', info['relevant_info'])
        self.assertNotIn('Second rule', info['relevant_info'])

    def test_deleted_all_stays_empty(self):
        self.store.delete('heavyai', [s['snippet_id'] for s in self.store.list('heavyai')])
        self.store.initialize('heavyai', ['Original domain rule'])
        self.assertEqual(self.store.relevant_info('heavyai'), {'snippet_ids': [], 'relevant_info': ''})

    def test_cross_database_access_and_mixed_delete_are_atomic(self):
        foreign = self.store.insert('other', ['Private guidance'])[0]
        own = self.store.list('heavyai')[0]['snippet_id']
        with self.assertRaises(KeyError):
            self.store.update('heavyai', foreign, 'Unauthorized edit')
        with self.assertRaises(KeyError):
            self.store.delete('heavyai', [own, foreign])
        self.assertEqual(len(self.store.list('heavyai')), 2)
        self.assertNotIn('Private guidance', self.store.relevant_info('heavyai')['relevant_info'])
        self.assertEqual(self.store.list('other')[0]['snippet'], 'Private guidance')

    def test_duplicate_bulk_insert_rolls_back(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert('heavyai', ['New rule', 'Original domain rule'])
        self.assertEqual(len(self.store.list('heavyai')), 2)

    def test_byte_budget_and_empty_input(self):
        for snippets in ([], [' '], ['é' * MAX_BYTES]):
            with self.assertRaises(ValueError):
                self.store.insert('heavyai', snippets)
        id_ = self.store.list('heavyai')[0]['snippet_id']
        with self.assertRaises(ValueError):
            self.store.update('heavyai', id_, 'x' * MAX_BYTES)
        self.assertEqual(self.store.list('heavyai')[0]['snippet'], 'Original domain rule')

    def test_updated_text_and_ids_reach_prompt_immediately(self):
        id_ = self.store.insert('heavyai', ['Use FRP in megawatts'])[0]
        self.assertIn(id_, self.store.relevant_info('heavyai')['snippet_ids'])
        self.store.update('heavyai', id_, 'Report detection counts, not distinct fires')
        content = self.store.relevant_info('heavyai')['relevant_info']
        self.assertIn('Report detection counts', content)
        self.assertNotIn('Use FRP', content)

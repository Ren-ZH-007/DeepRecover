from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.app_config import AppConfig
from storage.database.repository import SCHEMA_VERSION, Repository
from storage.models.candidate import Candidate
from ui.recovery.presentation import duplicate_label

A, B, C = 'a' * 64, 'b' * 64, 'c' * 64


def item(name, digest, kind='jpg'):
    return Candidate('signature_scan', name, kind, 1, sha256=digest, original_path='/scan/' + name)


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(Path(self.temp.name)); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.task = self.repo.create_task('signature_scan')
        self.items = [item('a1.jpg', A), item('none1.jpg', None), item('b1.png', B, 'png'), item('a2.jpg', A),
                      item('none2.jpg', None), item('a3.jpg', A), item('b2.png', B, 'png'), item('c1.pdf', C, 'pdf')]
        self.repo.add_candidates(self.task, self.items)
        other = self.repo.create_task('signature_scan')
        self.repo.add_candidates(other, [item('c-other.pdf', C, 'pdf')])

    def names(self, **filters):
        page = [c['name'] for c in self.repo.candidate_page(self.task, **filters)]
        self.assertEqual(self.repo.candidate_count(self.task, **filters), len(page))
        self.assertEqual(len(self.repo.candidate_ids(self.task, **filters)), len(page))
        return page

    def test_hiding_keeps_the_earliest_of_each_digest_and_every_undigested_candidate(self):
        self.assertEqual(len(self.names()), 8)
        self.assertEqual(self.names(hide_duplicates=True), ['a1.jpg', 'none1.jpg', 'b1.png', 'none2.jpg', 'c1.pdf'])

    def test_duplicates_are_only_compared_within_one_task(self):
        info = self.repo.duplicate_info(self.task, [i.id for i in self.items])
        self.assertNotIn(self.items[7].id, info)  # c1.pdf matches only a candidate of another task

    def test_duplicate_info_reports_group_size_and_earliest(self):
        info = self.repo.duplicate_info(self.task, [i.id for i in self.items])
        self.assertEqual(info, {self.items[0].id: (3, True), self.items[3].id: (3, False), self.items[5].id: (3, False),
                                self.items[2].id: (2, True), self.items[6].id: (2, False)})
        self.assertEqual(self.repo.duplicate_info(self.task, []), {})
        with self.assertRaises(ValueError):
            self.repo.duplicate_info(self.task, [str(i) for i in range(1001)])

    def test_hiding_combines_with_category_status_and_search(self):
        self.assertEqual(self.names(hide_duplicates=True, category='image'), ['a1.jpg', 'none1.jpg', 'b1.png', 'none2.jpg'])
        # Search narrows first-copy candidates only after duplicates are judged against the whole task.
        self.assertEqual(self.names(hide_duplicates=True, query='a2'), [])
        self.assertEqual(self.names(hide_duplicates=True, query='jpg'), ['a1.jpg', 'none1.jpg', 'none2.jpg'])
        self.assertEqual(self.names(hide_duplicates=True, category='document'), ['c1.pdf'])
        with self.assertRaises(ValueError):
            self.repo.candidate_count(self.task, hide_duplicates='yes')

    def test_existing_v3_database_gains_index_without_version_change(self):
        path = Path(self.temp.name) / 'old.db'
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_code TEXT, source_id TEXT, result_path TEXT)")
            db.execute("CREATE TABLE candidates(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), payload TEXT NOT NULL)")
            db.execute("CREATE INDEX candidate_task ON candidates(task_id)")
            db.execute("CREATE TABLE sources(id TEXT PRIMARY KEY, kind TEXT NOT NULL, locator TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE exports(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), candidate_id TEXT NOT NULL REFERENCES candidates(id), status TEXT NOT NULL, output_path TEXT NOT NULL, source_sha256 TEXT, target_sha256 TEXT, size INTEGER NOT NULL, created_at TEXT NOT NULL, error_code TEXT, metadata TEXT NOT NULL)")
            db.execute("CREATE INDEX export_task ON exports(task_id)")
            db.execute("PRAGMA user_version=3")
        repo = Repository(path)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertIn('candidate_sha', [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")])
        task = repo.create_task('signature_scan'); repo.add_candidates(task, [item('x.jpg', A), item('y.jpg', A)])
        self.assertEqual(repo.candidate_count(task, hide_duplicates=True), 1)
        Repository(path)  # Reopening is idempotent.

    def test_labels(self):
        self.assertEqual(duplicate_label(None), '')
        self.assertEqual(duplicate_label((3, True)), '共 3 份 · 最早一项')
        self.assertEqual(duplicate_label((2, False)), '共 2 份 · 重复')


if __name__ == '__main__':
    unittest.main()

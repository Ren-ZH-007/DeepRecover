from pathlib import Path
import tempfile
import unittest

from app.app_config import AppConfig
from storage.database.repository import MAX_QUERY, MAX_QUERY_TERMS, Repository
from storage.models.candidate import Candidate

ITEMS = [('IMG_1.jpg', '/sdcard/DCIM/IMG_1.jpg', 'jpg'), ('100%_done.png', '/a/100%_done.png', 'png'),
         ('a_b.pdf', '/docs/a_b.pdf', 'pdf'), ('axb.pdf', '/docs/axb.pdf', 'pdf'),
         ('报告.pdf', '/文档/报告.pdf', 'pdf'), ('back\\slash.txt', '/x/back\\slash.txt', 'txt'),
         ('clip.mp4', '/sdcard/Movies/clip.mp4', 'mp4')]


class ResultSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = AppConfig(Path(self.temp.name)); config.initialize()
        self.repo = Repository(config.database)
        self.task = self.repo.create_task('android_acquisition')
        self.repo.add_candidates(self.task, [Candidate('android_file', n, t, 1, original_path=p) for n, p, t in ITEMS])
        other = self.repo.create_task('android_acquisition')
        self.repo.add_candidates(other, [Candidate('android_file', 'IMG_1.jpg', 'jpg', 1)])

    def tearDown(self):
        self.temp.cleanup()

    def names(self, **filters):
        page = [c['name'] for c in self.repo.candidate_page(self.task, **filters)]
        self.assertEqual(self.repo.candidate_count(self.task, **filters), len(page))
        self.assertEqual(len(self.repo.candidate_ids(self.task, **filters)), len(page))
        return page

    def test_name_and_path_substrings_ignore_ascii_case(self):
        self.assertEqual(self.names(query='img'), ['IMG_1.jpg'])
        self.assertEqual(self.names(query='dcim'), ['IMG_1.jpg'])
        self.assertEqual(self.names(query='SDCARD'), ['IMG_1.jpg', 'clip.mp4'])
        self.assertEqual(self.names(query='报告'), ['报告.pdf'])

    def test_like_wildcards_and_backslash_are_literal(self):
        self.assertEqual(self.names(query='a_b'), ['a_b.pdf'])
        self.assertEqual(self.names(query='100%'), ['100%_done.png'])
        self.assertEqual(self.names(query='%'), ['100%_done.png'])
        self.assertEqual(self.names(query='\\'), ['back\\slash.txt'])
        self.assertEqual(self.names(query="'; DROP TABLE candidates; --"), [])
        self.assertEqual(len(self.names()), len(ITEMS))

    def test_terms_must_all_match_and_combine_with_other_filters(self):
        self.assertEqual(self.names(query='docs pdf'), ['a_b.pdf', 'axb.pdf'])
        self.assertEqual(self.names(query='  docs   a_b '), ['a_b.pdf'])
        self.assertEqual(self.names(query='sdcard', category='video'), ['clip.mp4'])
        self.assertEqual(self.names(query='pdf', category='image'), [])
        self.assertEqual(self.names(query='zzz'), [])

    def test_search_stays_within_the_task(self):
        self.assertEqual(self.names(query='IMG_1'), ['IMG_1.jpg'])

    def test_oversized_or_invalid_queries_are_rejected(self):
        for query in ('x' * (MAX_QUERY + 1), ' '.join('a' * (MAX_QUERY_TERMS + 1)), None, 5):
            with self.assertRaises(ValueError):
                self.repo.candidate_count(self.task, query=query)
        self.assertEqual(self.repo.candidate_count(self.task, query=' '.join('p' * MAX_QUERY_TERMS)), 6)


if __name__ == '__main__':
    unittest.main()

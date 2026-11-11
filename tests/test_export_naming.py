import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app.app_config import AppConfig
from core.candidate_io import snapshot
from core.export.naming import (MAX_COMPONENT_BYTES, MAX_DEPTH, MAX_RELATIVE_CHARS, NamingError,
                                choose_target, clean_component, relative_parts)
from core.export.service import export_batch
from core.tasks import CancellationToken
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource


def named(name, path, source='android_file', file_type='jpg'):
    return {'source': source, 'name': name, 'file_type': file_type, 'original_path': path}


class CleanComponentTests(unittest.TestCase):
    def test_separators_traversal_and_invalid_characters_become_plain_text(self):
        self.assertEqual(clean_component('../../CON:bad.jpg'), '.._.._CON_bad.jpg')
        self.assertEqual(clean_component('a\\b/c.jpg'), 'a_b_c.jpg')
        self.assertEqual(clean_component('a<b>c"d|e?f*g\x00h\x1f.png'), 'a_b_c_d_e_f_g_h_.png')
        for empty in ('', '.', '..', ' ', '...', ' . '):
            self.assertIsNone(clean_component(empty), empty)

    def test_windows_device_names_trailing_dots_and_drives(self):
        self.assertEqual(clean_component('CON'), '_CON')
        self.assertEqual(clean_component('nul.txt'), '_nul.txt')
        self.assertEqual(clean_component('LPT9.tar.gz'), '_LPT9.tar.gz')
        self.assertEqual(clean_component('CONSOLE.txt'), 'CONSOLE.txt')
        self.assertEqual(clean_component(' report. '), 'report')
        self.assertIsNone(clean_component('C:'))
        self.assertIsNone(clean_component('[$MFT record 42]'))

    def test_unicode_is_normalized_and_long_names_keep_their_extension(self):
        self.assertEqual(clean_component('cafe\u0301.jpg'), 'caf\u00e9.jpg')
        long = clean_component('照' * 200 + '.jpeg')
        self.assertTrue(long.endswith('.jpeg'))
        self.assertLessEqual(len(long.encode('utf-8')), MAX_COMPONENT_BYTES)
        self.assertLessEqual(len(clean_component('x' * 500).encode('utf-8')), MAX_COMPONENT_BYTES)


class RelativePartsTests(unittest.TestCase):
    def test_metadata_sources_keep_folders_without_the_file_component(self):
        self.assertEqual(relative_parts(named('IMG_1.jpg', '/sdcard/DCIM/Camera/IMG_1.jpg')),
                         ('sdcard', 'DCIM', 'Camera', 'IMG_1.jpg'))
        self.assertEqual(relative_parts(named('IMG_1', 'CameraRollDomain/Media/IMG_1', 'ios_backup', 'heic')),
                         ('CameraRollDomain', 'Media', 'IMG_1.heic'))
        self.assertEqual(relative_parts(named('a.doc', 'C:\\Users\\Ren\\a.doc', 'computer_deleted', 'doc')),
                         ('Users', 'Ren', 'a.doc'))
        self.assertEqual(relative_parts(named('r.pdf', '[$MFT record 9]/r.pdf', 'computer_deleted', 'pdf')),
                         ('r.pdf',))
        self.assertEqual(relative_parts(named('r.pdf', '[$MFT record 9]/[NTFS 目录 40]/项目/r.pdf', 'computer_deleted', 'pdf')),
                         ('项目', 'r.pdf'))

    def test_synthetic_names_stay_opaque(self):
        for source in ('signature_scan', 'computer_signature', None):
            self.assertIsNone(relative_parts(named('offset_0000.jpg', '/dev/disk2', source)))
        self.assertIsNone(relative_parts(named('..', '/sdcard/..')))

    def test_traversal_in_folders_is_dropped_and_depth_and_length_are_bounded(self):
        self.assertEqual(relative_parts(named('x.jpg', '/a/../../b/./x.jpg')), ('a', 'b', 'x.jpg'))
        deep = relative_parts(named('x.jpg', '/'.join(f'd{i}' for i in range(20)) + '/x.jpg'))
        self.assertEqual(len(deep) - 1, MAX_DEPTH)
        self.assertEqual(deep[-2], 'd19')
        wide = relative_parts(named('x.jpg', '/'.join('f' * 90 for _ in range(6)) + '/x.jpg'))
        self.assertLessEqual(len('/'.join(wide)), MAX_RELATIVE_CHARS)
        self.assertEqual(wide[-1], 'x.jpg')

    def test_extension_is_only_added_when_missing_and_safe(self):
        self.assertEqual(relative_parts(named('photo', '/p', file_type='JPG'))[-1], 'photo.jpg')
        self.assertEqual(relative_parts(named('photo', '/p', file_type='bin'))[-1], 'photo')
        self.assertEqual(relative_parts(named('photo', '/p', file_type='../x'))[-1], 'photo')
        self.assertEqual(relative_parts(named('photo.png', '/p', file_type='jpg'))[-1], 'photo.png')


class ChooseTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_conflicts_get_numbered_names_and_folders_are_created(self):
        item = named('photo.jpg', '/sdcard/DCIM/photo.jpg')
        first = choose_target(self.root, item); first.write_bytes(b'1')
        second = choose_target(self.root, item); second.write_bytes(b'2')
        third = choose_target(self.root, item)
        self.assertEqual([p.name for p in (first, second, third)], ['photo.jpg', 'photo (2).jpg', 'photo (3).jpg'])
        self.assertEqual(first.parent, self.root / 'sdcard' / 'DCIM')
        self.assertEqual(choose_target(self.root, named('README', '/README')).name, 'README.jpg')

    def test_manifest_name_is_reserved_at_the_batch_root(self):
        self.assertEqual(choose_target(self.root, named('manifest.json', 'manifest.json', file_type='json')).name,
                         'manifest (2).json')
        self.assertEqual(choose_target(self.root, named('Manifest.JSON', 'Manifest.JSON')).name, 'Manifest (2).JSON')

    def test_folder_blocked_by_file_or_symlink_is_rejected(self):
        (self.root / 'sdcard').write_bytes(b'file')
        with self.assertRaises(NamingError):
            choose_target(self.root, named('a.jpg', '/sdcard/a.jpg'))
        outside = tempfile.TemporaryDirectory(); self.addCleanup(outside.cleanup)
        try:
            os.symlink(outside.name, self.root / 'link', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        with self.assertRaises(NamingError):
            choose_target(self.root, named('a.jpg', '/link/a.jpg'))
        self.assertEqual(os.listdir(outside.name), [])

    def test_opaque_candidates_keep_random_names_at_the_root(self):
        target = choose_target(self.root, named('offset_1.jpg', '/dev/x', 'signature_scan', 'JPG'))
        self.assertEqual(target.parent, self.root)
        self.assertRegex(target.name, r'^[0-9a-f]{32}\.jpg$')
        self.assertRegex(choose_target(self.root, named('x', '/x', 'signature_scan', '../x')).name, r'^[0-9a-f]{32}\.bin$')


class ExportLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.config = AppConfig(self.root / 'data'); self.config.initialize()
        self.repo = Repository(self.config.database)

    def tearDown(self):
        self.temp.cleanup()

    def android(self, data, remote):
        path = self.config.data_dir / 'Acquisitions' / uuid4().hex; path.write_bytes(data)
        source = DataSource('android', 'SERIAL', 'Phone'); task = self.repo.create_task('android_acquisition', source)
        item = Candidate('android_file', Path(remote).name, Path(remote).suffix.lstrip('.'), len(data), original_path=remote,
                         local_path=str(path), source_id=source.id, source_stat=snapshot(path.stat()),
                         sha256=hashlib.sha256(data).hexdigest(), integrity='not_checked')
        self.repo.add_candidate(task, item); self.repo.update_task(task, 'completed')
        return item.id

    def export(self, ids):
        task = self.repo.create_task('export')
        result = export_batch(self.repo, task, ids, self.root / 'out', self.config.data_dir, CancellationToken())
        return [Path(r['output_path']) for r in self.repo.exports(task)], Path(result['manifest'])

    def test_original_layout_conflicts_and_manifest_are_kept(self):
        ids = [self.android(b'a', '/sdcard/DCIM/Camera/IMG_1.jpg'), self.android(b'b', '/sdcard/DCIM/Camera/IMG_1.jpg'),
               self.android(b'c', '/sdcard/Download/manifest.json'), self.android(b'd', '/manifest.json')]
        paths, manifest = self.export(ids)
        batch = manifest.parent
        self.assertEqual([p.relative_to(batch).as_posix() for p in paths],
                         ['sdcard/DCIM/Camera/IMG_1.jpg', 'sdcard/DCIM/Camera/IMG_1 (2).jpg',
                          'sdcard/Download/manifest.json', 'manifest (2).json'])
        self.assertEqual([p.read_bytes() for p in paths], [b'a', b'b', b'c', b'd'])
        self.assertEqual(manifest.name, 'manifest.json'); self.assertTrue(manifest.read_text(encoding='utf-8'))

    def test_folder_conflict_falls_back_to_a_random_name_instead_of_failing(self):
        ids = [self.android(b'file', '/sdcard'), self.android(b'nested', '/sdcard/a.jpg')]
        paths, manifest = self.export(ids)
        self.assertEqual(paths[0].relative_to(manifest.parent).as_posix(), 'sdcard')
        self.assertEqual(paths[1].parent, manifest.parent)
        self.assertRegex(paths[1].name, r'^[0-9a-f]{32}\.jpg$')
        self.assertEqual([p.read_bytes() for p in paths], [b'file', b'nested'])


if __name__ == '__main__':
    unittest.main()

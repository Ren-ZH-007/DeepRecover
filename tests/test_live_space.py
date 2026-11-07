"""Signature hits inside space a live file system still uses are skipped; real-device follow-ups."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from core.computer.allocation import allocation_map
from core.computer.io import permission_hint
from core.computer.service import ComputerScanner
from core.tasks import CancellationToken
from tests.apfs_fixture import BS, ApfsImage
from tests.exfat_fixture import ExfatImage
from tests.fat_fixture import Fat32Fixture
from tests.fixtures_ntfs import NTFSFixture
from tests.test_scanner import JPEG, chunk, png_image


def scan(path, **options):
    token = CancellationToken()
    found = [item for item in ComputerScanner().scan(path, token, mode='signature', **options) if hasattr(item, 'offset')]
    return found, token.progress


class AllocationMapTests(unittest.TestCase):
    def test_fat32_clusters_follow_the_active_table(self):
        volume = Fat32Fixture(prefix=4096)
        volume.set_fat(10, 11); volume.set_fat(11, 0x0fffffff)
        volume.set_fat(20, 0x0ffffff7)                          # bad cluster: in use, not free space
        allocated = allocation_map(volume.reader(), _partition(volume.partition, 'FAT32'), CancellationToken())
        at = volume.cluster_offset
        self.assertTrue(allocated.all_allocated(at(10), 2 * 512))
        self.assertTrue(allocated.all_allocated(at(20), 1))
        self.assertFalse(allocated.all_allocated(at(11), 513))  # runs into free cluster 12
        self.assertFalse(allocated.all_allocated(at(30), 10))
        self.assertTrue(allocated.all_allocated(4096 + 600, 100))  # reserved sectors and FATs are metadata
        self.assertFalse(allocated.all_allocated(4096 + volume.volume_size - 10, 5))  # past the last cluster

    def test_exfat_uses_the_allocation_bitmap(self):
        image = ExfatImage(clusters=64)
        image.occupy(10); image.occupy(11)
        image.finish_root()
        allocated = allocation_map(image.reader(), _partition(image.partition, 'exFAT'), CancellationToken())
        self.assertTrue(allocated.all_allocated(image.cluster_at(10), 2 * image.cluster_size))
        self.assertFalse(allocated.all_allocated(image.cluster_at(11), image.cluster_size + 1))
        self.assertFalse(allocated.all_allocated(image.cluster_at(40), 10))
        self.assertTrue(allocated.all_allocated(image.offset + 100, 10))

    def test_ntfs_uses_its_bitmap_file(self):
        volume = NTFSFixture(total_clusters=256)
        volume.set_bitmap(allocated=range(100, 140))
        allocated = allocation_map(volume.reader(), _partition(volume.partition, 'NTFS'), CancellationToken())
        self.assertTrue(allocated.all_allocated(100 * 4096, 40 * 4096))
        self.assertFalse(allocated.all_allocated(100 * 4096, 41 * 4096))
        self.assertFalse(allocated.all_allocated(200 * 4096, 10))

    def test_apfs_counts_queued_blocks_as_free(self):
        image = ApfsImage()
        _fid, live = image.live_file('live.bin', b'x' * BS)
        _fid, queued = image.deleted_file('gone.bin', b'y' * BS)
        image.queue(queued, 1)
        allocated = allocation_map(image.reader(), image.partition, CancellationToken())
        self.assertTrue(allocated.all_allocated(image.at(live), BS))
        self.assertFalse(allocated.all_allocated(image.at(queued), BS))

    def test_unknown_or_damaged_file_systems_skip_nothing(self):
        volume = Fat32Fixture()
        volume.data[510] = 0                                     # boot signature gone
        self.assertIsNone(allocation_map(volume.reader(), _partition(volume.partition, 'FAT32'), CancellationToken()))
        self.assertIsNone(allocation_map(volume.reader(), _partition(volume.partition, 'HFS+'), CancellationToken()))


def _partition(base, file_system):
    from types import SimpleNamespace
    return SimpleNamespace(offset=base.offset, size=base.size, file_system=file_system)


class SignatureSkipTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def fat_image(self):
        volume = Fat32Fixture()
        # The live PNG carries a JPEG in a text chunk: skipping the PNG must skip what is inside it too.
        live, free = png_image(raw=b'\x00\x01\x02\x03', before=chunk(b'tEXt', JPEG)), png_image(raw=b'\x00\x09\x08\x07')
        volume.payload(10, live); volume.set_fat(10, 11); volume.set_fat(11, 0x0fffffff)
        volume.payload(20, free)
        volume.payload(30, JPEG); volume.set_fat(30, 31); volume.set_fat(31, 0x0fffffff)   # a JPEG over clusters 30-31, both live
        path = self.root / 'fat.img'; path.write_bytes(volume.data)
        return path, volume, live, free

    def test_hits_in_live_files_are_skipped_and_counted(self):
        path, volume, _live, free = self.fat_image()
        found, progress = scan(path)
        self.assertEqual([(c.offset, c.sha256) for c in found], [(volume.cluster_offset(20), hashlib.sha256(free).hexdigest())])
        self.assertEqual(progress.skipped, 2)
        everything, progress = scan(path, include_live=True)
        self.assertEqual(sorted(c.offset for c in everything),
                         [volume.cluster_offset(10), volume.cluster_offset(20), volume.cluster_offset(30)])
        self.assertEqual(progress.skipped, 0)

    def test_skipped_hits_do_not_use_the_candidate_limit(self):
        path, volume, _live, _free = self.fat_image()
        found = [c for c in ComputerScanner(max_candidates=1).scan(path, CancellationToken(), mode='signature')
                 if hasattr(c, 'offset')]
        self.assertEqual([c.offset for c in found], [volume.cluster_offset(20)])

    def test_apfs_live_file_is_skipped_but_deleted_data_is_kept(self):
        image = ApfsImage()
        _fid, live = image.live_file('photo.png', png_image(raw=b'\x00live'))
        _fid, gone = image.deleted_file('old.png', png_image(raw=b'\x00gone'))
        path = self.root / 'apfs.img'; path.write_bytes(image.build())
        found, progress = scan(path)
        self.assertEqual([c.offset for c in found], [image.at(gone)])
        self.assertEqual(progress.skipped, 1)


class PermissionHintTests(unittest.TestCase):
    def test_hint_names_the_disk_and_the_partition(self):
        text = permission_hint('/dev/disk4s1', 'darwin')
        self.assertIn('sudo chmod o+r /dev/disk4 /dev/disk4s1', text)
        self.assertIn('不会自行提权', text)
        self.assertIn('sudo chmod o+r /dev/disk4 /dev/rdisk4 ', permission_hint('/dev/rdisk4', 'darwin'))
        self.assertIn('sudo chmod o+r /dev/disk7 临时', permission_hint('/dev/disk7', 'darwin'))
        self.assertIn('管理员身份运行', permission_hint(r'\\.\PhysicalDrive1', 'win32'))

    def test_denied_device_open_explains_what_to_do(self):
        from unittest.mock import patch
        from core.computer.io import DiskError, DiskReader
        with patch('core.computer.io.os.open', side_effect=PermissionError(13, 'denied')), \
                patch('core.computer.io.sys.platform', 'darwin'):
            with self.assertRaisesRegex(DiskError, 'sudo chmod o\\+r /dev/disk4 /dev/disk4s1'):
                with DiskReader('/dev/disk4s1', 'device'):
                    pass
        with patch('core.computer.io.os.open', side_effect=FileNotFoundError(2, 'missing')), \
                patch('core.computer.io.sys.platform', 'darwin'):
            with self.assertRaisesRegex(DiskError, '请检查路径'):
                with DiskReader('/dev/disk4s1', 'device'):
                    pass


class SystemFileFilterTests(unittest.TestCase):
    def test_system_files_hide_but_trash_and_signature_hits_stay(self):
        from app.app_config import AppConfig
        from storage.database.repository import Repository
        from storage.models.candidate import Candidate
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        config = AppConfig(Path(folder.name)); config.initialize()
        repo = Repository(config.database)
        task = repo.create_task('computer_scan')
        rows = [('/.Spotlight-V100/Store-V2/tmp.movePlan', 'tmp.movePlan'), ('/.fseventsd/0000000000a1b2c3', '0000000000a1b2c3'),
                ('/文稿/.DS_Store', '.DS_Store'), ('/文稿/._报告.docx', '._报告.docx'),
                ('/System Volume Information/IndexerVolumeGuid', 'IndexerVolumeGuid'),
                ('/.Trashes/501/照片.jpg', '照片.jpg'), ('/$RECYCLE.BIN/S-1-5-21/$RABC123.docx', '$RABC123.docx'),
                ('/文稿/报告.docx', '报告.docx'), ('/文稿/my.Spotlight-V100.txt', 'my.Spotlight-V100.txt')]
        repo.add_candidates(task, [Candidate('computer_deleted', name, 'bin', 1, original_path=path) for path, name in rows]
                            + [Candidate('computer_signature', '._looks_like_sidecar.png', 'png', 1, original_path='/dev/disk4s1')])
        shown = [c['name'] for c in repo.candidate_page(task, hide_system=True)]
        self.assertEqual(shown, ['照片.jpg', '$RABC123.docx', '报告.docx', 'my.Spotlight-V100.txt', '._looks_like_sidecar.png'])
        self.assertEqual(repo.candidate_count(task, hide_system=True), 5)
        self.assertEqual(len(repo.candidate_ids(task, hide_system=True)), 5)
        self.assertEqual(repo.candidate_count(task), 10)
        with self.assertRaises(ValueError):
            repo.candidate_count(task, hide_system='yes')


if __name__ == '__main__':
    unittest.main()

import hashlib
import struct
import unittest

from core.computer.fat import scan_fat32
from core.tasks import CancellationToken, TaskCancelled
from tests.fat_fixture import Fat32Fixture, lfn_entries, short_entry


class Fat32RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fat32Fixture()

    def scan(self, **limits):
        return list(scan_fat32(self.fixture.reader(), self.fixture.partition, CancellationToken(), **limits))

    def test_only_deleted_single_cluster_file_has_verified_extents(self):
        payload = b'ordinary deleted text\x00\xff'
        self.fixture.entries([short_entry(size=len(payload)), short_entry(b'LIVE    TXT', deleted=False, cluster=4)])
        self.fixture.set_fat(4, 0x0fffffff)
        self.fixture.payload(3, payload)
        before = hashlib.sha256(self.fixture.data).digest()
        rows = self.scan()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['name'], row['file_system'], row['deletion_state']), ('_OTE.TXT', 'FAT32', 'deleted_record'))
        self.assertEqual((row['status'], row['integrity']), ('candidate', 'metadata_checked'))
        self.assertEqual(row['extents'], [(self.fixture.cluster_offset(3), len(payload))])
        self.assertEqual(self.fixture.reader().read_at(*row['extents'][0]), payload)
        self.assertEqual(hashlib.sha256(self.fixture.data).digest(), before)

    def test_deleted_multicluster_layout_is_not_guessed(self):
        self.fixture.entries([short_entry(size=513)])
        row, = self.scan()
        self.assertEqual((row['status'], row['integrity'], row['extents']), ('partial', 'layout_unverified', []))

    def test_corrupt_short_name_does_not_become_a_ready_deleted_file(self):
        self.fixture.entries([short_entry(b'BAD\x00    TXT'), short_entry(b'NORMAL  TXT')])
        row, = self.scan()
        self.assertEqual(row['name'], '_ORMAL.TXT')

    def test_reallocated_and_out_of_range_clusters_are_not_exportable(self):
        self.fixture.entries([short_entry(cluster=3), short_entry(b'BAD     TXT', cluster=0x0ffffff0)])
        self.fixture.set_fat(3, 0x0fffffff)
        rows = self.scan()
        self.assertEqual([row['integrity'] for row in rows], ['allocated_clusters', 'invalid_extent'])
        self.assertTrue(all(row['status'] == 'partial' and not row['extents'] for row in rows))

    def test_zero_byte_deleted_file_is_a_valid_empty_candidate(self):
        self.fixture.entries([short_entry(size=0, cluster=0)])
        row, = self.scan()
        self.assertEqual((row['size'], row['status'], row['extents']), (0, 'candidate', []))

    def test_preserved_deleted_long_unicode_name(self):
        name = '家庭照片与文档资料.txt'
        self.fixture.entries(lfn_entries(name) + [short_entry()])
        row, = self.scan()
        self.assertEqual(row['name'], name)
        self.assertEqual(row['original_path'], '/' + name)

    def test_long_name_can_cross_directory_cluster_boundary(self):
        name = '家庭照片与文档资料.txt'
        long = lfn_entries(name)
        self.assertEqual(len(long), 2)
        filler = short_entry(b'VOLUME     ', deleted=False, cluster=0, size=0, attributes=0x08)
        self.fixture.set_fat(2, 4)
        self.fixture.set_fat(4, 0x0fffffff)
        self.fixture.entries([filler] * 15 + long[:1])
        self.fixture.entries(long[1:] + [short_entry()], cluster=4)
        row, = self.scan()
        self.assertEqual(row['name'], name)

    def test_mismatched_long_name_chunks_fall_back_to_unknown_first_short_byte(self):
        long = lfn_entries('较长的中文名称完整性检查.txt')
        damaged = bytearray(long[0]); damaged[13] ^= 1; long[0] = bytes(damaged)
        self.fixture.entries(long + [short_entry()])
        row, = self.scan()
        self.assertEqual(row['name'], '_OTE.TXT')

    def test_existing_directory_is_followed_but_deleted_directory_is_not(self):
        self.fixture.entries([short_entry(b'DOCUMENT   ', deleted=False, cluster=4, size=0, attributes=0x10),
                              short_entry(b'OLD_DIR    ', cluster=5, size=0, attributes=0x10)])
        self.fixture.set_fat(4, 0x0fffffff)
        self.fixture.entries([short_entry()], cluster=4)
        self.fixture.entries([short_entry(b'GHOST   TXT')], cluster=5)
        row, = self.scan()
        self.assertEqual(row['original_path'], '/DOCUMENT/_OTE.TXT')

    def test_directory_cycle_and_shared_directory_are_rejected(self):
        self.fixture.set_fat(2, 2)
        with self.assertRaises(ValueError):
            self.scan()
        self.fixture.set_fat(2, 0x0fffffff)
        self.fixture.entries([short_entry(b'DIR1       ', deleted=False, cluster=4, attributes=0x10),
                              short_entry(b'DIR2       ', deleted=False, cluster=4, attributes=0x10)])
        self.fixture.set_fat(4, 0x0fffffff)
        with self.assertRaises(ValueError):
            self.scan()

    def test_active_fat_selection_and_mirror_disagreement(self):
        self.fixture.entries([short_entry()])
        self.fixture.set_fat(3, 0x0fffffff, fat=0)
        row, = self.scan()
        self.assertEqual((row['integrity'], row['extents']), ('allocation_unknown', []))
        struct.pack_into('<H', self.fixture.data, 40, 0x81)
        row, = self.scan()
        self.assertEqual(row['integrity'], 'metadata_checked')

    def test_partition_offset_is_applied_to_every_extent(self):
        self.fixture = Fat32Fixture(prefix=4096)
        self.fixture.entries([short_entry()])
        row, = self.scan()
        self.assertEqual(row['extents'][0][0], self.fixture.cluster_offset(3))
        self.assertEqual(row['record_id'], self.fixture.cluster_offset(2))

    def test_scan_limits_and_cancellation_do_not_silently_complete(self):
        self.fixture.entries([short_entry(), short_entry(b'OTHER   TXT')])
        with self.assertRaisesRegex(ValueError, '候选'):
            self.scan(max_candidates=1)
        with self.assertRaisesRegex(ValueError, '目录项'):
            self.scan(max_entries=1)
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled):
            list(scan_fat32(self.fixture.reader(), self.fixture.partition, token))

    def test_invalid_boot_geometry_and_truncated_partition_are_rejected(self):
        for offset, replacement in [(11, b'\x01\x00'), (13, b'\x03'), (36, b'\0' * 4), (42, b'\x01\x00')]:
            original = bytes(self.fixture.data[offset:offset + len(replacement)])
            self.fixture.data[offset:offset + len(replacement)] = replacement
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.scan()
            self.fixture.data[offset:offset + len(replacement)] = original
        self.fixture.partition.size -= 512
        with self.assertRaises(ValueError):
            self.scan()


if __name__ == '__main__':
    unittest.main()

"""Synthetic NTFS deletion metadata tests; no mounted disk is accessed."""
import hashlib
import struct
import unittest

from core.computer.ntfs import NTFSError, scan_ntfs
from core.tasks import CancellationToken, TaskCancelled
from tests.fixtures_ntfs import (MemoryReader, NTFSFixture, file_name, file_record,
                                 nonresident_attr, resident_attr)


class NtfsDeletedRecordTests(unittest.TestCase):
    def scan(self, fixture, **options):
        before = hashlib.sha256(fixture.image).hexdigest()
        rows = list(scan_ntfs(fixture.reader(), fixture.partition, CancellationToken(), **options))
        self.assertEqual(hashlib.sha256(fixture.image).hexdigest(), before)
        return rows

    @staticmethod
    def content(fixture, row):
        return b''.join(fixture.image[offset:offset + size] for offset, size in row['extents'])

    def add_file(self, fixture, number=16, name='deleted.txt', content=b'deleted resident bytes', **record_options):
        fixture.write_record(number, [resident_attr(0x30, file_name(name)),
                                      resident_attr(0x80, content)], **record_options)
        return content

    def test_resident_deleted_file_preserves_name_bytes_and_read_only_source(self):
        fixture = NTFSFixture(partition_offset=4096)
        expected = self.add_file(fixture, name='删除的 家庭照片.png')
        rows = self.scan(fixture)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['name'], '删除的 家庭照片.png')
        self.assertEqual(row['record_id'], 16)
        self.assertEqual((row['status'], row['integrity'], row['deletion_state']),
                         ('candidate', 'metadata_checked', 'deleted_record'))
        self.assertEqual(self.content(fixture, row), expected)
        self.assertTrue(all(offset >= fixture.offset for offset, _ in row['extents']))

    def test_resident_data_crossing_sector_trailer_uses_original_usa_bytes(self):
        fixture = NTFSFixture()
        expected = self.add_file(fixture, content=bytes(range(256)) * 2)
        row = self.scan(fixture)[0]
        self.assertEqual(self.content(fixture, row), expected)
        self.assertGreaterEqual(len(row['extents']), 3)
        self.assertTrue(any(offset < row['extents'][0][0] for offset, _ in row['extents'][1:]))
        raw = fixture.record_bytes(16)
        self.assertNotIn(expected, raw)

    def test_fragmented_nonresident_runs_preserve_logical_order_and_trim_slack(self):
        fixture = NTFSFixture()
        first, second = b'A' * 4096, b'B' * 777
        fixture.write_cluster(120, first)
        fixture.write_cluster(100, second + b'not part of deleted file')
        fixture.write_record(16, [resident_attr(0x30, file_name('fragmented.bin')),
                                  nonresident_attr([(120, 1), (100, 1)], len(first) + len(second))])
        row = self.scan(fixture)[0]
        self.assertEqual(row['extents'], [(120 * 4096, 4096), (100 * 4096, 777)])
        self.assertEqual(self.content(fixture, row), first + second)

    def test_fragmented_mft_maps_deleted_records_and_resident_fixups(self):
        fixture = NTFSFixture(mft_runs=[(4, 4), (30, 4)])
        expected = self.add_file(fixture, number=20, content=bytes(range(256)) * 2)
        row = self.scan(fixture)[0]
        self.assertEqual(row['record_id'], 20)
        self.assertGreater(row['extents'][0][0], 30 * 4096)
        self.assertEqual(self.content(fixture, row), expected)

    def test_512_byte_clusters_and_nonresident_bitmap(self):
        fixture = NTFSFixture(cluster_size=512)
        fixture.set_bitmap(nonresident=True)
        fixture.write_cluster(100, b'content')
        fixture.write_record(20, [resident_attr(0x30, file_name('small.dat')),
                                  nonresident_attr([(100, 1)], 7, cluster_size=512)])
        row = self.scan(fixture)[0]
        self.assertEqual(row['integrity'], 'metadata_checked')
        self.assertEqual(self.content(fixture, row), b'content')

    def test_live_files_directories_and_extension_records_are_never_deleted_candidates(self):
        fixture = NTFSFixture()
        self.add_file(fixture, 16, in_use=True)
        self.add_file(fixture, 17, directory=True)
        self.add_file(fixture, 18, base_reference=16)
        self.assertEqual(self.scan(fixture), [])

    def test_reallocated_nonresident_cluster_is_partial_and_has_no_export_ranges(self):
        fixture = NTFSFixture()
        fixture.set_bitmap(allocated=[100])
        fixture.write_record(16, [resident_attr(0x30, file_name('reused.bin')),
                                  nonresident_attr([(100, 1)], 20)])
        row = self.scan(fixture)[0]
        self.assertEqual((row['status'], row['integrity']), ('partial', 'allocated_clusters'))
        self.assertEqual(row['extents'], [])

    def test_compressed_encrypted_sparse_and_uninitialized_data_are_partial(self):
        for flags, initialized in [(1, None), (0x4000, None), (0x8000, None), (0, 3)]:
            with self.subTest(flags=flags, initialized=initialized):
                fixture = NTFSFixture()
                fixture.write_record(16, [resident_attr(0x30, file_name('unsupported.bin')),
                    nonresident_attr([(100, 1)], 20, flags=flags, initialized_size=initialized)])
                row = self.scan(fixture)[0]
                self.assertEqual(row['status'], 'partial')
                self.assertEqual(row['extents'], [])

    def test_attribute_list_and_duplicate_unnamed_data_do_not_guess_content(self):
        for extra in [resident_attr(0x20, b'extension metadata'), resident_attr(0x80, b'ambiguous')]:
            with self.subTest(extra=extra[:4]):
                fixture = NTFSFixture()
                fixture.write_record(16, [resident_attr(0x30, file_name('split.bin')),
                                          resident_attr(0x80, b'first'), extra])
                row = self.scan(fixture)[0]
                self.assertEqual(row['status'], 'partial')
                self.assertEqual(row['extents'], [])

    def test_out_of_volume_overlapping_sparse_and_invalid_run_lengths_are_partial(self):
        attrs = [nonresident_attr([(250, 10)], 100),
                 nonresident_attr([(100, 1), (100, 1)], 100),
                 nonresident_attr([(None, 1)], 100)]
        broken = bytearray(nonresident_attr([(100, 1)], 100)); broken[64] = 0xf1
        attrs.append(bytes(broken))
        for attr in attrs:
            with self.subTest(mapping=attr[64:]):
                fixture = NTFSFixture()
                fixture.write_record(16, [resident_attr(0x30, file_name('bad-map.bin')), attr])
                row = self.scan(fixture)[0]
                self.assertEqual(row['status'], 'partial')
                self.assertEqual(row['extents'], [])

    def test_corrupt_usa_record_is_unverified_and_cannot_export_marker_bytes(self):
        fixture = NTFSFixture()
        self.add_file(fixture)
        fixture.patch_record(16, 510, b'bad'[:2])
        row = self.scan(fixture)[0]
        self.assertEqual(row['deletion_state'], 'unverified_record')
        self.assertEqual(row['status'], 'partial')
        self.assertEqual(row['extents'], [])

    def test_invalid_attribute_length_stays_non_exportable(self):
        fixture = NTFSFixture()
        self.add_file(fixture)
        raw = fixture.record_bytes(16)
        offset = struct.unpack_from('<H', raw, 20)[0]
        fixture.patch_record(16, offset + 4, struct.pack('<I', 0xfffffff8))
        row = self.scan(fixture)[0]
        self.assertEqual(row['status'], 'partial')
        self.assertEqual(row['extents'], [])

    def test_valid_empty_resident_deleted_file_has_no_source_ranges(self):
        fixture = NTFSFixture()
        self.add_file(fixture, content=b'')
        row = self.scan(fixture)[0]
        self.assertEqual((row['size'], row['extents'], row['status']), (0, [], 'candidate'))

    def test_long_windows_name_preferred_over_dos_alias(self):
        fixture = NTFSFixture()
        fixture.write_record(16, [resident_attr(0x30, file_name('LONGFI~1.TXT', namespace=2)),
                                  resident_attr(0x30, file_name('Long filename.txt', namespace=1)),
                                  resident_attr(0x80, b'body')])
        self.assertEqual(self.scan(fixture)[0]['name'], 'Long filename.txt')

    def test_bad_boot_geometry_mft_mapping_and_missing_bitmap_fail_explicitly(self):
        for mode in ['boot', 'record-size', 'mft-map', 'bitmap']:
            with self.subTest(mode=mode):
                fixture = NTFSFixture()
                if mode == 'boot': fixture.image[3:11] = b'NOT NTFS'
                elif mode == 'record-size': fixture.image[64] = 0x80
                elif mode == 'mft-map':
                    fixture.write_record(0, [nonresident_attr([(8, 8)], 32768)], in_use=True)
                else:
                    fixture.write_record(6, [resident_attr(0x80, b'')], in_use=True)
                with self.assertRaises(NTFSError): self.scan(fixture)

    def test_candidate_limit_and_record_limit_preserve_prior_yields_then_fail(self):
        fixture = NTFSFixture()
        self.add_file(fixture, 16); self.add_file(fixture, 25)
        for options in [{'max_candidates': 1}, {'max_records': 20}]:
            with self.subTest(options=options):
                stream = scan_ntfs(fixture.reader(), fixture.partition, CancellationToken(), **options)
                self.assertEqual(next(stream)['record_id'], 16)
                with self.assertRaises(NTFSError): list(stream)

    def test_pre_cancel_does_not_read_any_source_bytes(self):
        fixture = NTFSFixture()
        class NoRead:
            size = len(fixture.image)
            def read_at(self, *_): raise AssertionError('pre-cancel read source')
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled): list(scan_ntfs(NoRead(), fixture.partition, token))

    def test_cancellation_during_bitmap_walk_is_not_swallowed_as_partial(self):
        fixture = NTFSFixture()
        fixture.write_record(16, [resident_attr(0x30, file_name('large.bin')),
                                  nonresident_attr([(100, 80)], 80 * 4096)])
        class Cancelling(CancellationToken):
            calls = 0
            def checkpoint(self):
                self.calls += 1
                if self.calls == 25: self.cancel()
                super().checkpoint()
        with self.assertRaises(TaskCancelled): list(scan_ntfs(fixture.reader(), fixture.partition, Cancelling()))

    def test_short_device_read_fails_explicitly(self):
        fixture = NTFSFixture()
        class ShortReader(MemoryReader):
            def read_at(self, offset, size): return super().read_at(offset, size)[:-1]
        with self.assertRaises(NTFSError): list(scan_ntfs(ShortReader(fixture.image), fixture.partition, CancellationToken()))

    def test_record_progress_is_monotonic_and_completes_even_without_candidates(self):
        fixture = NTFSFixture()
        class Recording(CancellationToken):
            events = None
            def __init__(self):
                super().__init__(); self.events = []
            def report_progress(self, processed, total, candidates, phase='scan'):
                super().report_progress(processed, total, candidates, phase)
                self.events.append((processed, total, candidates, phase))
        token = Recording()
        self.assertEqual(list(scan_ntfs(fixture.reader(), fixture.partition, token)), [])
        self.assertEqual(token.events, [(0, 32, 0, 'computer'), (32, 32, 0, 'computer')])
        self.add_file(fixture)
        token = Recording()
        self.assertEqual(len(list(scan_ntfs(fixture.reader(), fixture.partition, token))), 1)
        self.assertEqual(token.events[-1], (32, 32, 1, 'computer'))


if __name__ == '__main__':
    unittest.main()

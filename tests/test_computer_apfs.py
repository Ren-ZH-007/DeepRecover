import hashlib
import struct
import unittest

from core.computer.apfs import fletcher64, scan_apfs, valid_object
from core.tasks import CancellationToken, TaskCancelled
from tests.apfs_fixture import BS, ApfsImage, drec, extent, inode, header, seal


def payload(seed, size):
    return hashlib.sha256(seed.encode()).digest() * (size // 32) + b'x' * (size % 32)


class _Scanning(unittest.TestCase):
    def scan(self, image, **limits):
        reader = image.reader()
        before = hashlib.sha256(reader.data).hexdigest()
        result = list(scan_apfs(reader, image.partition, CancellationToken(), **limits))
        self.assertEqual(hashlib.sha256(reader.data).hexdigest(), before)
        end = image.offset + image.blocks * BS
        self.assertTrue(all(image.offset <= at and at + size <= end for at, size in reader.reads if size))
        return result

    def by_name(self, image, **limits):
        return {item['name']: item for item in self.scan(image, **limits)}

    def content(self, image, candidate):
        reader = image.reader()
        return b''.join(reader.read_at(at, size) for at, size in candidate['extents'])


class ApfsDeletedTests(_Scanning):
    def test_deleted_file_keeps_unicode_name_path_time_and_exact_bytes(self):
        image = ApfsImage(offset=8192)
        image.live_file('keep.txt', b'live')
        data = payload('photo', 3 * BS + 123)
        fid, start = image.deleted_file('已删除 照片 🌈.png', data)
        candidate, = self.scan(image)
        self.assertEqual(candidate['name'], '已删除 照片 🌈.png')
        self.assertEqual(candidate['original_path'], '/已删除 照片 🌈.png')
        self.assertEqual((candidate['size'], candidate['record_id']), (len(data), fid))
        self.assertEqual(candidate['extents'], [(image.at(start), len(data))])
        self.assertEqual((candidate['integrity'], candidate['status']), ('metadata_checked', 'candidate'))
        self.assertEqual((candidate['deletion_state'], candidate['file_system']), ('deleted_record', 'APFS'))
        self.assertEqual((candidate['modified_at'], candidate['modified_basis']), ('2023-11-14T22:13:20', 'utc'))
        self.assertEqual(self.content(image, candidate), data)

    def test_live_files_and_their_old_tree_versions_are_not_reported(self):
        image = ApfsImage()
        fid, start = image.live_file('current.txt', payload('live', 5000))
        image.orphan(4, [inode(fid, size=10), drec(2, 'old name.txt', fid), extent(fid, 0, BS, start)])
        self.assertEqual(self.scan(image), [])

    def test_deleted_and_live_directories_supply_the_path(self):
        image = ApfsImage()
        live_dir = image.live_directory('资料')
        image.deleted_file('a.txt', b'in live dir', parent=live_dir)
        gone_dir = image.fid()
        fid, records, _start = image.deleted_records('b.txt', b'in deleted dir', parent=gone_dir)
        image.orphan(5, records + [inode(gone_dir, mode=0o40755), drec(2, 'old', gone_dir, 4)])
        image.deleted_file('c.txt', b'unknown parent', parent=999)
        paths = sorted(item['original_path'] for item in self.scan(image))
        self.assertEqual(paths, ['/old/b.txt', '/资料/a.txt', '[APFS 目录 999]/c.txt'])

    def test_fragmented_file_is_read_in_logical_order(self):
        image = ApfsImage()
        data = payload('fragments', 3 * BS)
        image.allocate(10)
        image.deleted_file('fragmented.bin', data, pieces=[(28, 2), (22, 1)])
        candidate, = self.scan(image)
        self.assertEqual(candidate['extents'], [(28 * BS, 2 * BS), (22 * BS, BS)])
        self.assertEqual(self.content(image, candidate), data)

    def test_adjacent_extents_are_merged(self):
        image = ApfsImage()
        image.allocate(10)
        data = payload('adjacent', 2 * BS + 5)
        image.deleted_file('adjacent.bin', data, pieces=[(22, 1), (23, 2)])
        self.assertEqual(self.scan(image)[0]['extents'], [(22 * BS, len(data))])

    def test_blocks_allocated_again_are_not_exportable(self):
        image = ApfsImage()
        _fid, start = image.deleted_file('reused by live.bin', payload('a', BS))
        image.live_file('new.bin', payload('b', BS), start=start)
        _fid, start = image.deleted_file('bitmap only.bin', payload('c', BS))
        image.occupy(start)
        result = self.by_name(image)
        for name in ('reused by live.bin', 'bitmap only.bin'):
            self.assertEqual((result[name]['integrity'], result[name]['status'], result[name]['extents']),
                             ('allocated_clusters', 'partial', []))

    def test_blocks_waiting_in_the_free_queue_count_as_free(self):
        image = ApfsImage()
        data = payload('queued', 2 * BS)
        _fid, start = image.deleted_file('just deleted.bin', data)
        image.queue(start, 2)
        candidate, = self.scan(image)
        self.assertEqual(candidate['integrity'], 'metadata_checked')
        self.assertEqual(self.content(image, candidate), data)

    def test_blocks_reused_by_a_newer_deleted_file_are_rejected_only_for_the_older_file(self):
        image = ApfsImage()
        _fid, start = image.deleted_file('older.bin', payload('old', 2 * BS), xid=4)
        image.deleted_file('newer.bin', payload('new', BS), xid=7, start=start + 1)
        result = self.by_name(image)
        self.assertEqual(result['older.bin']['integrity'], 'overwritten_after_delete')
        self.assertEqual(result['older.bin']['extents'], [])
        self.assertEqual(result['newer.bin']['integrity'], 'metadata_checked')
        self.assertEqual(self.content(image, result['newer.bin']), payload('new', BS))

    def test_same_transaction_overlap_is_rejected_for_both(self):
        image = ApfsImage()
        _fid, start = image.deleted_file('one.bin', payload('1', BS), xid=6)
        image.deleted_file('two.bin', payload('2', BS), xid=6, start=start)
        self.assertEqual({item['integrity'] for item in self.scan(image)}, {'overwritten_after_delete'})

    def test_block_now_holding_an_apfs_object_is_overwritten(self):
        image = ApfsImage()
        _fid, start = image.deleted_file('metadata reuse.bin', payload('m', 3 * BS))
        image.write(start + 2, seal(header(5000, 8, 0x40000003, 0x0B)))
        candidate, = self.scan(image)
        self.assertEqual((candidate['integrity'], candidate['extents']), ('overwritten_after_delete', []))

    def test_object_like_header_with_bad_checksum_is_ordinary_data(self):
        image = ApfsImage()
        block = bytearray(seal(header(5000, 8, 0x40000003, 0x0B))); block[100] ^= 1
        data = payload('x', BS) + bytes(block)
        image.deleted_file('looks like metadata.bin', data)
        candidate, = self.scan(image)
        self.assertEqual(candidate['integrity'], 'metadata_checked')
        self.assertEqual(self.content(image, candidate), data)

    def test_clone_sharing_all_blocks_with_a_live_file_is_explained(self):
        image = ApfsImage()
        source, start = image.live_file('original.bin', payload('clone', 2 * BS), internal=0x410)
        clone = image.fid()
        image.orphan(5, [inode(clone, size=2 * BS, internal=0x10, private=source), drec(2, 'copy.bin', clone),
                         extent(source, 0, 2 * BS, start)])
        candidate, = self.scan(image)
        self.assertEqual((candidate['integrity'], candidate['status']), ('shared_with_live', 'partial'))

    def test_partial_live_overlap_without_clone_flag_is_allocated(self):
        image = ApfsImage()
        _fid, start = image.live_file('live.bin', payload('l', BS))
        fid = image.fid()
        image.orphan(5, [inode(fid, size=2 * BS, internal=0x10), drec(2, 'half.bin', fid), extent(fid, 0, 2 * BS, start)])
        self.assertEqual(self.scan(image)[0]['integrity'], 'allocated_clusters')

    def test_unsupported_or_inconsistent_layouts_never_supply_extents(self):
        image = ApfsImage()
        image.deleted_file('compressed.txt', payload('c', BS), bsd=0x20)
        image.deleted_file('sparse flag.bin', payload('s', BS), internal=0x200)
        fid = image.fid()
        image.orphan(5, [inode(fid, size=BS), drec(2, 'hole.bin', fid), extent(fid, 0, BS, 0)])
        fid = image.fid()
        image.orphan(5, [inode(fid, size=3 * BS), drec(2, 'gap.bin', fid), extent(fid, 0, BS, 40), extent(fid, 2 * BS, 2 * BS, 41)])
        fid = image.fid()
        image.orphan(5, [inode(fid, size=3 * BS), drec(2, 'short.bin', fid), extent(fid, 0, BS, 43)])
        fid = image.fid()
        image.orphan(5, [inode(fid, size=BS), drec(2, 'outside.bin', fid), extent(fid, 0, BS, image.blocks + 5)])
        fid = image.fid()
        image.orphan(5, [inode(fid, size=10 * 1024 ** 3), drec(2, 'huge.bin', fid)])
        image.allocate(30)
        result = self.by_name(image)
        expected = {'compressed.txt': 'unsupported_layout', 'sparse flag.bin': 'unsupported_layout', 'hole.bin': 'unsupported_layout',
                    'gap.bin': 'invalid_metadata', 'short.bin': 'invalid_metadata', 'outside.bin': 'invalid_extent', 'huge.bin': 'size_limit'}
        self.assertEqual({name: item['integrity'] for name, item in result.items()}, expected)
        self.assertTrue(all(item['status'] == 'partial' and item['extents'] == [] for item in result.values()))

    def test_name_without_inode_and_inode_without_name(self):
        image = ApfsImage()
        image.deleted_file('no inode.txt', payload('n', BS), with_inode=False)
        data = payload('nameless', 2 * BS)
        fid, _start = image.deleted_file('ignored', data, with_name=False)
        result = self.by_name(image)
        self.assertEqual((result['no inode.txt']['integrity'], result['no inode.txt']['status']), ('invalid_metadata', 'partial'))
        nameless = result[f'APFS_{fid}.bin']
        self.assertEqual((nameless['original_path'], nameless['integrity']), (f'/APFS_{fid}.bin', 'metadata_checked'))
        self.assertEqual(self.content(image, nameless), data)

    def test_zero_length_file_has_an_empty_checked_layout(self):
        image = ApfsImage()
        image.deleted_file('empty.txt', b'')
        candidate, = self.scan(image)
        self.assertEqual((candidate['size'], candidate['extents'], candidate['integrity']), (0, [], 'metadata_checked'))

    def test_directories_and_other_types_are_not_file_candidates(self):
        image = ApfsImage()
        fid = image.fid()
        image.orphan(5, [inode(fid, mode=0o40755), drec(2, 'folder', fid, 4)])
        fid = image.fid()
        image.orphan(5, [inode(fid, mode=0o120755, size=4), drec(2, 'link', fid, 10)])
        self.assertEqual(self.scan(image), [])

    def test_newest_node_version_wins(self):
        image = ApfsImage()
        first = payload('v1', BS); second = payload('v2', 2 * BS)
        fid, records, start = image.deleted_records('draft.txt', first)
        image.orphan(4, records)
        _fid, records, _start = image.deleted_records('final.txt', second, fid=fid)
        image.orphan(8, records)
        candidate, = self.scan(image)
        self.assertEqual((candidate['name'], candidate['size']), ('final.txt', len(second)))
        self.assertEqual(self.content(image, candidate), second)

    def test_nodes_with_bad_checksums_or_uncommitted_xids_are_ignored(self):
        image = ApfsImage()
        _fid, records, _start = image.deleted_records('corrupt.bin', payload('c', BS))
        block = image.orphan(5, records)
        _fid, records, _start = image.deleted_records('future.bin', payload('f', BS))
        image.orphan(image.xid + 1, records)
        data = image.build()
        broken = bytearray(data); broken[block * BS + 200] ^= 0xFF
        from tests.exfat_fixture import MemoryReader
        reader = MemoryReader(broken)
        self.assertEqual(list(scan_apfs(reader, image.partition, CancellationToken())), [])

    def test_non_hashed_directory_names(self):
        image = ApfsImage(hashed=False)
        image.live_file('live.txt', b'live')
        image.deleted_file('plain key.txt', payload('p', 100))
        self.assertEqual([item['name'] for item in self.scan(image)], ['plain key.txt'])

    def test_unsupported_containers_are_refused(self):
        for setting, message in (('encrypted', '加密'), ('volumes', '多个卷')):
            with self.subTest(setting=setting):
                image = ApfsImage()
                setattr(image, setting, 2 if setting != 'encrypted' else True)
                with self.assertRaisesRegex(ValueError, message):
                    self.scan(image)

    def test_damaged_or_truncated_containers_are_refused(self):
        image = ApfsImage()
        data = bytearray(image.build())
        for block in (0, 2):
            data[block * BS + 300] ^= 1
        from tests.exfat_fixture import MemoryReader
        with self.assertRaisesRegex(ValueError, '超级块'):
            list(scan_apfs(MemoryReader(data), image.partition, CancellationToken()))
        broken = bytearray(image.build()); broken[14 * BS + 200] ^= 1
        with self.assertRaisesRegex(ValueError, '当前文件树'):
            list(scan_apfs(MemoryReader(broken), image.partition, CancellationToken()))
        short = image.partition; short.size -= BS
        with self.assertRaisesRegex(ValueError, '截断'):
            list(scan_apfs(image.reader(), short, CancellationToken()))

    def test_inconsistent_bitmap_makes_allocation_unknown(self):
        image = ApfsImage()
        image.deleted_file('x.bin', payload('x', BS))
        data = bytearray(image.build()); data[16 * BS + 10] ^= 0x01
        from tests.exfat_fixture import MemoryReader
        candidate, = scan_apfs(MemoryReader(data), image.partition, CancellationToken())
        self.assertEqual((candidate['integrity'], candidate['extents']), ('allocation_unknown', []))

    def test_candidate_and_record_limits(self):
        image = ApfsImage()
        for index in range(3):
            image.deleted_file(f'{index}.bin', payload(str(index), 100))
        with self.assertRaisesRegex(ValueError, '上限'):
            self.scan(image, max_candidates=2)
        reader = image.reader(); found = []
        with self.assertRaisesRegex(ValueError, '部分完成'):
            for item in scan_apfs(reader, image.partition, CancellationToken(), max_records=6):
                found.append(item)
        self.assertTrue(found)
        self.assertTrue(all(item['status'] == 'partial' and item['integrity'] in ('validation_limit', 'invalid_metadata') for item in found))
        with self.assertRaises(ValueError):
            list(scan_apfs(reader, image.partition, CancellationToken(), max_records=0))

    def test_cancellation_stops_the_scan(self):
        image = ApfsImage()
        image.deleted_file('x.bin', payload('x', BS))
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled):
            list(scan_apfs(image.reader(), image.partition, token))

    def test_checksum_detects_any_change(self):
        block = bytearray(BS); block[8:16] = struct.pack('<Q', 1); block[24:28] = b'\x01\x00\x00\x80'
        struct.pack_into('<Q', block, 0, fletcher64(bytes(block)))
        self.assertTrue(valid_object(bytes(block)))
        block[40] = 1
        self.assertFalse(valid_object(bytes(block)))
        self.assertFalse(valid_object(bytes(BS)))


if __name__ == '__main__':
    unittest.main()


class ApfsSnapshotTests(_Scanning):
    def test_file_deleted_after_a_snapshot_is_read_from_the_snapshot(self):
        image = ApfsImage()
        image.live_file('keep.txt', b'still here')
        snap = image.snapshot('com.apple.TimeMachine.2026-11-06-101500.local', 6)
        data = payload('in snapshot', 2 * BS + 99)
        fid, start = image.snapshot_file(snap, '报告 final.docx', data)
        candidate, = self.scan(image)
        self.assertEqual(candidate['original_path'], '[快照 com.apple.TimeMachine.2026-11-06-101500.local]/报告 final.docx')
        self.assertEqual((candidate['name'], candidate['size'], candidate['record_id']), ('报告 final.docx', len(data), fid))
        self.assertEqual((candidate['integrity'], candidate['status'], candidate['deletion_state']),
                         ('snapshot_checked', 'candidate', 'snapshot_record'))
        self.assertEqual(candidate['extents'], [(image.at(start), len(data))])
        self.assertEqual(candidate['structure_details'], {'check_version': 1, 'snapshot_name': snap['name'], 'snapshot_xid': 6,
                                                          'snapshot_created_utc': '2025-06-15T15:06:40'})
        self.assertEqual(candidate['modified_at'], '2023-11-14T22:13:20')
        self.assertEqual(self.content(image, candidate), data)

    def test_files_that_still_exist_are_not_reported_even_with_older_content(self):
        image = ApfsImage()
        fid, _start = image.live_file('changed.txt', payload('now', BS))
        snap = image.snapshot('old', 5)
        image.snapshot_file(snap, 'changed.txt', payload('then', BS), fid=fid)
        self.assertEqual(self.scan(image), [])

    def test_each_file_comes_once_from_the_newest_snapshot_that_has_it(self):
        image = ApfsImage()
        older = image.snapshot('older', 4, with_live=False)
        newer = image.snapshot('newer', 7, with_live=False)
        fid, _ = image.snapshot_file(older, 'draft.txt', payload('v1', BS))
        image.snapshot_file(newer, 'draft.txt', payload('v2', 2 * BS), fid=fid)
        image.snapshot_file(older, 'only old.txt', payload('old', 100))
        result = {item['name']: item for item in self.scan(image)}
        self.assertEqual(set(result), {'draft.txt', 'only old.txt'})
        self.assertEqual(result['draft.txt']['original_path'], '[快照 newer]/draft.txt')
        self.assertEqual(self.content(image, result['draft.txt']), payload('v2', 2 * BS))
        self.assertEqual(result['only old.txt']['original_path'], '[快照 older]/only old.txt')

    def test_unchanged_snapshots_share_tree_nodes(self):
        image = ApfsImage()
        first = image.snapshot('first', 4)
        image.snapshot_file(first, 'shared.bin', payload('s', BS))
        image.snapshot('second', 6, share=first)
        candidate, = self.scan(image)
        self.assertEqual(candidate['original_path'], '[快照 second]/shared.bin')
        # root inode (current) + root inode and three file records (one snapshot node, read once)
        self.assertEqual(len(self.scan(image, max_records=5)), 1)
        with self.assertRaisesRegex(ValueError, '上限'):
            self.scan(image, max_records=4)

    def test_snapshot_and_current_nodes_are_not_counted_again_as_old_nodes(self):
        image = ApfsImage()
        image.live_file('keep.txt', b'live')                       # current tree: 1 + 3 records
        snap = image.snapshot('s', 6)                               # snapshot tree: 1 + 3 + 3 records
        image.snapshot_file(snap, 'held.txt', payload('h', BS))
        image.deleted_file('old node.txt', payload('o', BS), xid=8)  # orphaned node: 3 records
        found = {item['name']: item['integrity'] for item in self.scan(image, max_records=14)}
        self.assertEqual(found, {'held.txt': 'snapshot_checked', 'old node.txt': 'metadata_checked'})

    def test_directories_in_snapshots_and_live_directories_form_the_path(self):
        image = ApfsImage()
        live_dir = image.live_directory('项目')
        snap = image.snapshot('s', 5)
        gone_dir = image.snapshot_directory(snap, 'old folder', parent=live_dir)
        image.snapshot_file(snap, 'notes.txt', b'notes', parent=gone_dir)
        candidate, = self.scan(image)
        self.assertEqual(candidate['original_path'], '[快照 s]/项目/old folder/notes.txt')

    def test_snapshot_blocks_must_still_be_allocated(self):
        image = ApfsImage()
        snap = image.snapshot('s', 5)
        image.snapshot_file(snap, 'not held.bin', payload('x', BS), held=False)
        _fid, start = image.snapshot_file(snap, 'queued.bin', payload('q', BS))
        image.queue(start, 1)
        result = {item['name']: item for item in self.scan(image)}
        for name in ('not held.bin', 'queued.bin'):
            self.assertEqual((result[name]['integrity'], result[name]['status'], result[name]['extents']),
                             ('allocation_unknown', 'partial', []))

    def test_unsupported_snapshot_layouts_and_dataless_snapshots(self):
        image = ApfsImage()
        snap = image.snapshot('s', 5)
        image.snapshot_file(snap, 'compressed.txt', payload('c', BS), bsd=0x20)
        dataless = image.snapshot('dataless', 6, flags=1)
        image.snapshot_file(dataless, 'no data.bin', payload('d', BS))
        candidate, = self.scan(image)
        self.assertEqual((candidate['name'], candidate['integrity']), ('compressed.txt', 'unsupported_layout'))

    def test_snapshot_trees_are_not_carved_and_held_files_are_not_duplicated(self):
        image = ApfsImage()
        snap = image.snapshot('s', 6)
        fid, start = image.snapshot_file(snap, 'held.txt', payload('h', BS))
        image.orphan(7, [inode(fid, size=BS), drec(2, 'held.txt', fid), extent(fid, 0, BS, start)])
        image.deleted_file('between snapshots.txt', payload('b', BS), xid=8)
        found = self.scan(image)
        self.assertEqual(sorted((item['name'], item['deletion_state']) for item in found),
                         [('between snapshots.txt', 'deleted_record'), ('held.txt', 'snapshot_record')])
        result = {item['name']: item for item in found}
        self.assertEqual(result['between snapshots.txt']['integrity'], 'metadata_checked')

    def test_orphan_record_whose_blocks_a_snapshot_holds_is_allocated(self):
        image = ApfsImage()
        snap = image.snapshot('s', 6)
        _fid, start = image.snapshot_file(snap, 'snap.bin', payload('s', BS))
        image.deleted_file('older.bin', payload('o', BS), xid=3, start=start)
        result = {item['name']: item['integrity'] for item in self.scan(image)}
        self.assertEqual(result, {'snap.bin': 'snapshot_checked', 'older.bin': 'allocated_clusters'})

    def test_damaged_or_inconsistent_snapshot_metadata_is_refused(self):
        image = ApfsImage()
        image.snapshot_file(image.snapshot('s', 5), 'a.bin', b'a')
        image.snapshot_count = 2
        with self.assertRaisesRegex(ValueError, '快照数量'):
            self.scan(image)
        image.snapshot_count = None
        data = bytearray(image.build()); data[17 * BS + 300] ^= 1
        from tests.exfat_fixture import MemoryReader
        with self.assertRaisesRegex(ValueError, '快照元数据'):
            list(scan_apfs(MemoryReader(data), image.partition, CancellationToken()))
        snap = image.snaps[0]
        data = bytearray(image.build()); data[snap['node'] * BS + 300] ^= 1
        with self.assertRaisesRegex(ValueError, '快照文件树'):
            list(scan_apfs(MemoryReader(data), image.partition, CancellationToken()))

    def test_snapshot_names_cannot_create_export_folders(self):
        image = ApfsImage()
        image.snapshot_file(image.snapshot('a/b', 5), 'x.txt', b'x')
        self.assertEqual(self.scan(image)[0]['original_path'], '[快照 a_b]/x.txt')

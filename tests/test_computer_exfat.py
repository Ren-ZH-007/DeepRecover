import hashlib
import struct
import unittest

from core.computer.exfat import scan_exfat
from core.tasks import CancellationToken, TaskCancelled
from tests.exfat_fixture import ExfatImage, MemoryReader, Partition, entry_set


class ExfatDeletedTests(unittest.TestCase):
    def scan(self, image, **limits):
        reader = image.reader()
        before = hashlib.sha256(reader.data).hexdigest()
        result = list(scan_exfat(reader, image.partition, CancellationToken(), **limits))
        self.assertEqual(hashlib.sha256(reader.data).hexdigest(), before)
        self.assertTrue(all(image.offset <= at < image.offset + image.volume_size for at, size in reader.reads if size))
        return result

    def test_deleted_contiguous_file_retains_unicode_name_and_exact_extents(self):
        image = ExfatImage().finish_root([entry_set('长中文文件名和空格 🌈.png')])
        content = bytes(range(200)) * 3
        image.write_cluster(5, content)
        result = self.scan(image)
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate['name'], '长中文文件名和空格 🌈.png')
        self.assertEqual(candidate['original_path'], '/长中文文件名和空格 🌈.png')
        self.assertEqual(candidate['extents'], [(image.cluster_at(5), 600)])
        self.assertEqual(candidate['integrity'], 'metadata_checked')
        self.assertEqual(candidate['status'], 'candidate')
        self.assertEqual(candidate['deletion_state'], 'deleted_record')
        self.assertEqual(b''.join(image.reader().read_at(at, size) for at, size in candidate['extents']), content)

    def test_active_files_and_deleted_directories_are_not_deleted_file_candidates(self):
        image = ExfatImage().finish_root([
            entry_set('active.txt', deleted=False),
            entry_set('old-directory', directory=True, size=512),
        ])
        self.assertEqual(self.scan(image), [])

    def test_allocated_or_fragmented_or_uninitialized_files_have_no_export_extents(self):
        image = ExfatImage().finish_root([
            entry_set('reused.bin', first=5),
            entry_set('fragmented.bin', first=8, contiguous=False),
            entry_set('uninitialized.bin', first=10, valid_size=12),
        ])
        image.occupy(5)
        result = self.scan(image)
        self.assertEqual([item['integrity'] for item in result], ['allocated_clusters', 'layout_unverified', 'uninitialized_data'])
        self.assertTrue(all(item['status'] == 'partial' and item['extents'] == [] for item in result))

    def test_invalid_cluster_and_file_size_never_supply_extents(self):
        for first, size in ((0, 100), (65, 1024), (5, 2**40)):
            with self.subTest(first=first, size=size):
                result = self.scan(ExfatImage().finish_root([entry_set(first=first, size=size)]))
                self.assertEqual(result[0]['status'], 'partial')
                self.assertEqual(result[0]['extents'], [])

    def test_zero_length_deleted_file_has_empty_verified_layout(self):
        result = self.scan(ExfatImage().finish_root([entry_set('empty.txt', first=0, size=0)]))
        self.assertEqual(result[0]['size'], 0)
        self.assertEqual(result[0]['integrity'], 'metadata_checked')
        self.assertEqual(result[0]['extents'], [])

    def test_active_fat_and_matching_allocation_bitmap_are_selected(self):
        image = ExfatImage(fats=2, active_fat=1).finish_root([entry_set(first=6)])
        image.occupy(6, bitmap_index=0)
        result = self.scan(image)
        self.assertEqual(result[0]['status'], 'candidate')
        image.occupy(6, bitmap_index=1)
        self.assertEqual(self.scan(image)[0]['integrity'], 'allocated_clusters')

    def test_existing_subdirectory_is_traversed_using_its_stream_layout(self):
        image = ExfatImage().finish_root([entry_set('资料目录', first=8, size=512, directory=True, deleted=False)])
        image.occupy(8)
        image.write_cluster(8, entry_set('文档.txt', first=12, size=30) + bytes(32))
        result = self.scan(image)
        self.assertEqual(result[0]['original_path'], '/资料目录/文档.txt')
        self.assertEqual(result[0]['extents'], [(image.cluster_at(12), 30)])

    def test_fat_linked_subdirectory_is_supported_but_cycle_is_rejected(self):
        image = ExfatImage().finish_root([entry_set('folder', first=8, size=512, directory=True, deleted=False, contiguous=False)])
        image.set_fat(8, 0xffffffff)
        image.occupy(8)
        image.write_cluster(8, entry_set('inside.txt', first=12, size=30) + bytes(32))
        self.assertEqual(self.scan(image)[0]['name'], 'inside.txt')
        cyclic = ExfatImage()
        cyclic.set_fat(2, 2)
        cyclic.write_cluster(2, b''.join(cyclic.root_entries) + b'\xa0' + bytes(31) + (b'\xa0' + bytes(31)) * 14)
        with self.assertRaisesRegex(ValueError, '循环'):
            self.scan(cyclic)

    def test_boot_checksum_entryset_checksum_and_missing_bitmap_fail_closed(self):
        image = ExfatImage().finish_root([entry_set()])
        image.data[image.offset + 100] ^= 1
        with self.assertRaisesRegex(ValueError, '引导区校验'):
            self.scan(image)
        image = ExfatImage().finish_root([entry_set()])
        image.data[image.cluster_at(2) + 32 + 4] ^= 1
        with self.assertRaisesRegex(ValueError, '目录项校验'):
            self.scan(image)
        image = ExfatImage(); image.root_entries.clear(); image.finish_root([entry_set()])
        with self.assertRaisesRegex(ValueError, '位图'):
            self.scan(image)

    def test_bitmap_capacity_and_partition_bounds_are_checked(self):
        image = ExfatImage()
        damaged = bytearray(image.root_entries[0]); struct.pack_into('<Q', damaged, 24, 1)
        image.root_entries[0] = bytes(damaged); image.finish_root([entry_set()])
        with self.assertRaisesRegex(ValueError, '位图不完整'):
            self.scan(image)
        image = ExfatImage().finish_root([entry_set()])
        with self.assertRaises(ValueError):
            list(scan_exfat(image.reader(), Partition(image.offset, image.volume_size - 1), CancellationToken()))
        with self.assertRaises(ValueError):
            list(scan_exfat(image.reader(), Partition(-1, image.volume_size), CancellationToken()))

    def test_bitmap_cannot_describe_its_own_storage_or_live_directory_as_free(self):
        image = ExfatImage().finish_root([entry_set()])
        image.data[image.cluster_at(3)] &= ~2
        with self.assertRaisesRegex(ValueError, '系统元数据冲突'):
            self.scan(image)
        image = ExfatImage().finish_root([entry_set('folder', first=8, size=512, directory=True, deleted=False)])
        image.write_cluster(8, entry_set('inside.txt', first=12, size=30) + bytes(32))
        with self.assertRaisesRegex(ValueError, '活动目录冲突'):
            self.scan(image)

    def test_entry_and_candidate_budgets_and_cancellation_are_explicit(self):
        image = ExfatImage().finish_root([entry_set('one.bin'), entry_set('two.bin', first=10)])
        with self.assertRaisesRegex(ValueError, '上限'):
            self.scan(image, max_entries=2)
        iterator = scan_exfat(image.reader(), image.partition, CancellationToken(), max_candidates=1)
        self.assertEqual(next(iterator)['name'], 'one.bin')
        with self.assertRaisesRegex(ValueError, '上限'):
            next(iterator)
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled):
            list(scan_exfat(image.reader(), image.partition, token))

    def test_larger_sector_and_cluster_geometry_preserves_absolute_offsets(self):
        image = ExfatImage(bps=4096, spc=2, offset=8192).finish_root([entry_set(size=9000)])
        result = self.scan(image)
        self.assertEqual(result[0]['extents'], [(image.cluster_at(5), 9000)])

    def test_directory_reference_cycles_and_depth_limits_are_explicit(self):
        image = ExfatImage().finish_root([entry_set('cycle', first=2, size=512, directory=True, deleted=False)])
        with self.assertRaisesRegex(ValueError, '循环'):
            self.scan(image)
        image = ExfatImage(clusters=200).finish_root([entry_set('d', first=10, size=512, directory=True, deleted=False)])
        for cluster in range(10, 139):
            image.occupy(cluster)
            image.write_cluster(cluster, entry_set('d', first=cluster + 1, size=512, directory=True, deleted=False) + bytes(32))
        with self.assertRaisesRegex(ValueError, '深度'):
            self.scan(image)

    def test_overlong_directory_paths_are_rejected_without_truncation(self):
        long_name = '长' * 250
        image = ExfatImage(spc=2).finish_root([entry_set(long_name, first=10, size=1024, directory=True, deleted=False)])
        for cluster in range(10, 29):
            image.occupy(cluster)
            image.write_cluster(cluster, entry_set(long_name, first=cluster + 1, size=1024, directory=True, deleted=False) + bytes(32))
        with self.assertRaisesRegex(ValueError, '4096'):
            self.scan(image)


if __name__ == '__main__':
    unittest.main()

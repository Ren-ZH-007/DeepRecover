import struct
import unittest
import zlib

from core.computer.partitions import discover_partitions, source_partitions, PartitionError
from tests.fixtures_ntfs import NTFSFixture, MemoryReader


def gpt(sector=512):
    image = bytearray(4 * 1024 * 1024)
    volume = NTFSFixture(); start = 34
    image[start * sector:start * sector + len(volume.image)] = volume.image
    entries = bytearray(128 * 128)
    entries[:16] = bytes(range(1, 17)); entries[16:32] = bytes(range(17, 33))
    struct.pack_into('<QQ', entries, 32, start, start + len(volume.image) // sector - 1)
    label = '电脑 数据卷'.encode('utf-16-le'); entries[56:56 + len(label)] = label
    header = bytearray(sector); header[:8] = b'EFI PART'
    struct.pack_into('<II', header, 8, 0x10000, 92)
    struct.pack_into('<QQQQ', header, 24, 1, len(image) // sector - 1, 34, len(image) // sector - 34)
    struct.pack_into('<QIII', header, 72, 2, 128, 128, zlib.crc32(entries))
    struct.pack_into('<I', header, 16, zlib.crc32(header[:92]))
    image[sector:sector * 2] = header
    image[sector * 2:sector * 2 + len(entries)] = entries
    return image


class ComputerPartitionTests(unittest.TestCase):
    def test_volume_image_is_not_misread_as_mbr(self):
        result = discover_partitions(NTFSFixture().reader())
        self.assertEqual((result[0].offset, result[0].file_system), (0, 'NTFS'))

    def test_gpt_512_and_4096_sector_offsets_crc_and_unicode(self):
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                result = discover_partitions(MemoryReader(gpt(sector)))
                self.assertEqual((result[0].offset, result[0].file_system, result[0].label),
                                 (sector * 34, 'NTFS', '电脑 数据卷'))
                self.assertEqual(len(source_partitions(MemoryReader(gpt(sector)))), 2)

    def test_corrupt_gpt_is_never_used_as_metadata_but_allows_explicit_signature_scan(self):
        data = gpt(); data[512 + 20] ^= 1
        with self.assertRaises(PartitionError): discover_partitions(MemoryReader(data))
        result = source_partitions(MemoryReader(data))
        self.assertEqual((len(result), result[0].file_system), (1, 'unknown'))
        self.assertTrue(result[0].warning)

    def test_partition_array_checksum_is_verified(self):
        data = gpt(); data[1024 + 100] ^= 1
        with self.assertRaises(PartitionError): discover_partitions(MemoryReader(data))

    def test_overlapping_mbr_and_out_of_bounds_partitions_fail_closed(self):
        for start, size in ((1, 100), (1000, 2)):
            data = bytearray(512 * 200); data[510:512] = b'\x55\xaa'
            for index, (lba, count) in enumerate(((1, 100), (start, size))):
                data[446 + index * 16 + 4] = 7
                struct.pack_into('<II', data, 446 + index * 16 + 8, lba, count)
            with self.subTest(start=start), self.assertRaises(PartitionError): discover_partitions(MemoryReader(data))

    def test_protective_mbr_does_not_guess_without_gpt(self):
        data = bytearray(4096); data[510:512] = b'\x55\xaa'; data[450] = 0xee
        struct.pack_into('<II', data, 454, 1, 7)
        with self.assertRaises(PartitionError): discover_partitions(MemoryReader(data))

    def test_empty_source_and_unknown_source_are_explicit(self):
        with self.assertRaises(PartitionError): source_partitions(MemoryReader(b''))
        result = source_partitions(MemoryReader(b'not a filesystem'))
        self.assertEqual((result[0].offset, result[0].file_system), (0, 'unknown'))

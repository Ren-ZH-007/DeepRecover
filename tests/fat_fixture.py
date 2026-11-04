"""In-memory standards-sized FAT32 volume, generated without user data."""
from types import SimpleNamespace
import struct


class MemoryReader:
    def __init__(self, data):
        self.data = data
        self.size = len(data)
        self.reads = []

    def read_at(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.size:
            raise OSError('synthetic out of bounds read')
        self.reads.append((offset, size))
        return bytes(self.data[offset:offset + size])


class Fat32Fixture:
    sector = 512
    reserved = 32
    fat_sectors = 512
    clusters = 65525

    def __init__(self, prefix=0, fats=2):
        self.fats, self.base = fats, prefix
        self.data_start = (self.reserved + fats * self.fat_sectors) * self.sector
        total = self.reserved + fats * self.fat_sectors + self.clusters
        self.volume_size = total * self.sector
        self.data = bytearray(prefix + self.volume_size)
        self.partition = SimpleNamespace(offset=prefix, size=self.volume_size)
        boot = memoryview(self.data)[prefix:prefix + 512]
        boot[:3] = b'\xeb\x58\x90'
        boot[3:11] = b'MSDOS5.0'
        struct.pack_into('<H', boot, 11, self.sector)
        boot[13] = 1
        struct.pack_into('<H', boot, 14, self.reserved)
        boot[16] = fats
        boot[21] = 0xf8
        struct.pack_into('<I', boot, 32, total)
        struct.pack_into('<I', boot, 36, self.fat_sectors)
        struct.pack_into('<I', boot, 44, 2)
        boot[82:90] = b'FAT32   '
        boot[510:512] = b'\x55\xaa'
        self.set_fat(2, 0x0fffffff)

    def set_fat(self, cluster, value, fat=None):
        for number in range(self.fats) if fat is None else [fat]:
            position = self.base + (self.reserved + number * self.fat_sectors) * self.sector + cluster * 4
            struct.pack_into('<I', self.data, position, value)

    def cluster_offset(self, cluster):
        return self.base + self.data_start + (cluster - 2) * self.sector

    def entries(self, entries, cluster=2):
        position = self.cluster_offset(cluster)
        for entry in entries:
            assert len(entry) == 32
            self.data[position:position + 32] = entry
            position += 32

    def payload(self, cluster, payload):
        position = self.cluster_offset(cluster)
        self.data[position:position + len(payload)] = payload

    def reader(self):
        return MemoryReader(self.data)


def short_entry(name=b'NOTE    TXT', *, deleted=True, cluster=3, size=20, attributes=0x20):
    assert len(name) == 11
    entry = bytearray(32)
    entry[:11] = name
    if deleted:
        entry[0] = 0xe5
    entry[11] = attributes
    struct.pack_into('<H', entry, 20, cluster >> 16)
    struct.pack_into('<H', entry, 26, cluster & 0xffff)
    struct.pack_into('<I', entry, 28, size)
    return bytes(entry)


def lfn_entries(name, short_name=b'NOTE    TXT', *, deleted=True):
    checksum = 0
    for byte in short_name:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 0xff
    words = list(struct.unpack('<' + 'H' * (len(name.encode('utf-16-le')) // 2), name.encode('utf-16-le')))
    words.append(0)
    words.extend([0xffff] * ((-len(words)) % 13))
    result = []
    for index in range(len(words) // 13 - 1, -1, -1):
        entry = bytearray(32)
        entry[0] = 0xe5 if deleted else index + 1 + (0x40 if index == len(words) // 13 - 1 else 0)
        entry[11] = 0x0f
        entry[13] = checksum
        content = struct.pack('<13H', *words[index * 13:(index + 1) * 13])
        entry[1:11], entry[14:26], entry[28:32] = content[:10], content[10:22], content[22:]
        result.append(bytes(entry))
    return result

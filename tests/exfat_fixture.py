"""Small generated exFAT volumes; no user disk data or external fixtures."""
from dataclasses import dataclass
import struct


@dataclass
class Partition:
    offset: int
    size: int


class MemoryReader:
    def __init__(self, data):
        self.data = bytes(data)
        self.size = len(data)
        self.reads = []

    def read_at(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.size:
            raise OSError('fixture out-of-range read')
        self.reads.append((offset, size))
        return self.data[offset:offset + size]


def entry_set(name='已删除照片.png', first=5, size=600, *, deleted=True, contiguous=True,
              valid_size=None, directory=False, modified=0, modified_10ms=0, modified_offset=0):
    raw_name = name.encode('utf-16-le')
    name_length = len(raw_name) // 2
    count = (name_length + 14) // 15
    primary = bytearray(32); primary[0] = 0x85; primary[1] = count + 1
    struct.pack_into('<H', primary, 4, 0x10 if directory else 0x20)
    struct.pack_into('<I', primary, 12, modified); primary[21] = modified_10ms; primary[23] = modified_offset
    stream = bytearray(32); stream[0] = 0xc0; stream[1] = 3 if contiguous else 1; stream[3] = name_length
    struct.pack_into('<Q', stream, 8, size if valid_size is None else valid_size)
    struct.pack_into('<I', stream, 20, first)
    struct.pack_into('<Q', stream, 24, size)
    entries = [primary, stream]
    for index in range(count):
        entry = bytearray(32); entry[0] = 0xc1
        part = raw_name[index * 30:(index + 1) * 30]
        entry[2:2 + len(part)] = part
        entries.append(entry)
    checksum = 0
    for entry_index, entry in enumerate(entries):
        for index, value in enumerate(entry):
            if entry_index == 0 and index in (2, 3):
                continue
            checksum = ((((checksum & 1) << 15) | (checksum >> 1)) + value) & 0xffff
    struct.pack_into('<H', primary, 2, checksum)
    if deleted:
        for entry in entries:
            entry[0] &= 0x7f
    return b''.join(entries)


class ExfatImage:
    def __init__(self, *, bps=512, spc=1, fats=1, active_fat=0, clusters=64, offset=1024):
        self.bps, self.spc, self.cluster_size, self.offset = bps, spc, bps * spc, offset
        self.fats, self.active_fat, self.clusters = fats, active_fat, clusters
        self.fat_length = max(1, ((clusters + 2) * 4 + bps - 1) // bps)
        self.heap = 24 + self.fat_length * fats
        self.volume_size = (self.heap + clusters * spc) * bps
        self.data = bytearray(offset + self.volume_size)
        boot = memoryview(self.data)[offset:offset + bps]
        boot[3:11] = b'EXFAT   '
        struct.pack_into('<Q', boot, 72, self.volume_size // bps)
        struct.pack_into('<IIII', boot, 80, 24, self.fat_length, self.heap, clusters)
        struct.pack_into('<I', boot, 96, 2)
        struct.pack_into('<HH', boot, 104, 0x0100, active_fat)
        boot[108], boot[109], boot[110] = bps.bit_length() - 1, spc.bit_length() - 1, fats
        boot[510:512] = b'\x55\xaa'
        self.root_entries = []
        for index in range(fats):
            first = 3 + index
            entry = bytearray(32); entry[0] = 0x81; entry[1] = index
            struct.pack_into('<I', entry, 20, first)
            struct.pack_into('<Q', entry, 24, (clusters + 7) // 8)
            self.root_entries.append(bytes(entry))
            self.set_fat(first, 0xffffffff)
            self.occupy(first, bitmap_index=index)
            self.occupy(2, bitmap_index=index)
        self.set_fat(2, 0xffffffff)
        for cluster in range(2, 3 + fats):
            self.occupy(cluster)
        self.finish_boot()

    @property
    def partition(self):
        return Partition(self.offset, self.volume_size)

    def cluster_at(self, cluster):
        return self.offset + self.heap * self.bps + (cluster - 2) * self.cluster_size

    def set_fat(self, cluster, value, *, fat=None):
        for index in range(self.fats) if fat is None else [fat]:
            struct.pack_into('<I', self.data, self.offset + (24 + index * self.fat_length) * self.bps + cluster * 4, value)

    def occupy(self, cluster, *, bitmap_index=None):
        indexes = range(self.fats) if bitmap_index is None else [bitmap_index]
        for index in indexes:
            position = self.cluster_at(3 + index) + (cluster - 2) // 8
            self.data[position] |= 1 << ((cluster - 2) % 8)

    def write_cluster(self, cluster, data):
        start = self.cluster_at(cluster)
        self.data[start:start + len(data)] = data

    def finish_root(self, files=()):
        payload = b''.join(self.root_entries) + b''.join(files) + bytes(32)
        if len(payload) > self.cluster_size:
            raise ValueError('fixture root exceeded one cluster')
        self.write_cluster(2, payload)
        return self

    def finish_boot(self):
        checksum = 0
        boot = self.data[self.offset:self.offset + 11 * self.bps]
        for index, value in enumerate(boot):
            if index in (106, 107, 112):
                continue
            checksum = ((((checksum & 1) << 31) | (checksum >> 1)) + value) & 0xffffffff
        start = self.offset + 11 * self.bps
        self.data[start:start + self.bps] = struct.pack('<I', checksum) * (self.bps // 4)
        return self

    def reader(self):
        return MemoryReader(self.data)

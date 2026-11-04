"""Small synthetic NTFS volumes for parser tests; never opens a real disk.

The default volume has 256 clusters, 32 MFT records, and a resident $Bitmap.
Records 0 ($MFT), 5 (root), and 6 ($Bitmap) are initialized and marked in use.
All physical offsets exposed by the fixture include ``partition_offset``.
"""
from math import ceil
import struct
from types import SimpleNamespace


def _align(value, alignment=8):
    return (value + alignment - 1) // alignment * alignment


def _unsigned(value):
    if value < 0:
        raise ValueError('Negative unsigned run value')
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'little')


def _signed(value):
    for length in range(1, 9):
        try:
            return value.to_bytes(length, 'little', signed=True)
        except OverflowError:
            pass
    raise ValueError('Run offset exceeds signed 64 bits')


def runlist(runs):
    """Encode ``[(absolute_lcn_or_None, cluster_count), ...]`` mapping pairs."""
    result = bytearray()
    previous = 0
    for lcn, count in runs:
        if count <= 0:
            raise ValueError('Run length must be positive')
        count_bytes = _unsigned(count)
        offset_bytes = b'' if lcn is None else _signed(lcn - previous)
        if len(count_bytes) > 8 or len(offset_bytes) > 8:
            raise ValueError('Invalid mapping pair size')
        result.append(len(offset_bytes) << 4 | len(count_bytes))
        result.extend(count_bytes)
        result.extend(offset_bytes)
        if lcn is not None:
            previous = lcn
    result.append(0)
    return bytes(result)


def resident_attr(kind, value, *, name='', flags=0, attribute_id=0):
    """Build a complete, aligned resident NTFS attribute."""
    name_bytes = name.encode('utf-16le')
    value_offset = _align(24 + len(name_bytes))
    data = bytearray(_align(value_offset + len(value)))
    struct.pack_into('<IIBBHHH', data, 0, kind, len(data), 0, len(name_bytes) // 2,
                     24 if name_bytes else 0, flags, attribute_id)
    struct.pack_into('<IH', data, 16, len(value), value_offset)
    data[24:24 + len(name_bytes)] = name_bytes
    data[value_offset:value_offset + len(value)] = value
    return bytes(data)


def nonresident_attr(runs, size, *, name='', flags=0, attribute_id=0,
                     initialized_size=None, cluster_size=4096, lowest_vcn=0,
                     compression_unit=0, allocated_size=None):
    """Build unnamed/named DATA (0x80), including fragmented or sparse runs.

    Tests may alter the returned type field to construct another nonresident
    attribute. ``runs`` use absolute LCNs, while their encoding is relative.
    """
    runs = list(runs)
    name_bytes = name.encode('utf-16le')
    header_size = 72 if flags & (0x0001 | 0x8000) else 64
    pairs_offset = _align(header_size + len(name_bytes))
    pairs = runlist(runs)
    data = bytearray(_align(pairs_offset + len(pairs)))
    struct.pack_into('<IIBBHHH', data, 0, 0x80, len(data), 1, len(name_bytes) // 2,
                     header_size if name_bytes else 0, flags, attribute_id)
    total_clusters = sum(count for _, count in runs)
    highest_vcn = lowest_vcn + total_clusters - 1 if total_clusters else lowest_vcn
    struct.pack_into('<QQHH', data, 16, lowest_vcn, highest_vcn, pairs_offset, compression_unit)
    allocated_size = (sum(count for lcn, count in runs if lcn is not None) * cluster_size
                      if allocated_size is None else allocated_size)
    struct.pack_into('<QQQ', data, 40, allocated_size, size,
                     size if initialized_size is None else initialized_size)
    if header_size == 72:
        struct.pack_into('<Q', data, 64, allocated_size)
    data[header_size:header_size + len(name_bytes)] = name_bytes
    data[pairs_offset:pairs_offset + len(pairs)] = pairs
    return bytes(data)


def file_name(name, parent=5, namespace=1, *, parent_sequence=1, size=0, flags=0):
    """Build a resident FILE_NAME value; ``parent`` is an MFT record number."""
    name_bytes = name.encode('utf-16le')
    units = len(name_bytes) // 2
    if not 1 <= units <= 255:
        raise ValueError('NTFS fixture name must contain 1..255 UTF-16 units')
    value = bytearray(66 + len(name_bytes))
    reference = parent if parent >> 48 else parent | parent_sequence << 48
    struct.pack_into('<Q', value, 0, reference)
    struct.pack_into('<QQI', value, 40, _align(size, 4096), size, flags)
    value[64], value[65] = units, namespace
    value[66:] = name_bytes
    return bytes(value)


def file_record(attrs, *, in_use=False, directory=False, record_id=0,
                record_size=1024, sector_size=512, usa_offset=48,
                sequence=1, base_reference=0, usa_sequence=0xa55a):
    """Build a FILE record and replace each sector trailer with its USA word.

    The original trailer bytes are retained in the update sequence array.
    Consequently resident values crossing trailers require real fixup handling.
    """
    if record_size % sector_size or sector_size < 512:
        raise ValueError('Invalid FILE record geometry')
    attrs = list(attrs)
    usa_count = record_size // sector_size + 1
    attrs_offset = _align(usa_offset + usa_count * 2)
    data = bytearray(record_size)
    data[:4] = b'FILE'
    struct.pack_into('<HH', data, 4, usa_offset, usa_count)
    struct.pack_into('<HHHH', data, 16, sequence, 1, attrs_offset,
                     (1 if in_use else 0) | (2 if directory else 0))
    struct.pack_into('<I', data, 28, record_size)
    struct.pack_into('<Q', data, 32, base_reference)
    struct.pack_into('<H', data, 40, len(attrs))
    struct.pack_into('<I', data, 44, record_id)
    position = attrs_offset
    for index, attr in enumerate(attrs):
        if position + len(attr) + 4 > record_size:
            raise ValueError('Attributes exceed FILE record size')
        data[position:position + len(attr)] = attr
        struct.pack_into('<H', data, position + 14, index)
        position += len(attr)
    struct.pack_into('<I', data, position, 0xffffffff)
    struct.pack_into('<I', data, 24, position + 4)
    struct.pack_into('<H', data, usa_offset, usa_sequence)
    for index in range(1, usa_count):
        trailer = index * sector_size - 2
        data[usa_offset + index * 2:usa_offset + index * 2 + 2] = data[trailer:trailer + 2]
        struct.pack_into('<H', data, trailer, usa_sequence)
    return bytes(data)


class MemoryReader:
    def __init__(self, image):
        self.image = image

    @property
    def size(self):
        return len(self.image)

    def read_at(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.size:
            raise OSError('Synthetic disk read exceeds image bounds')
        return bytes(self.image[offset:offset + size])


class NTFSFixture:
    """Mutable in-memory volume with helpers mapping logical fragmented MFT IO."""
    def __init__(self, *, cluster_size=4096, record_count=32,
                 partition_offset=0, mft_runs=None, total_clusters=256):
        if cluster_size < 512 or cluster_size % 512 or cluster_size & (cluster_size - 1):
            raise ValueError('Cluster size must be a power-of-two multiple of 512')
        if record_count < 7:
            raise ValueError('Fixture needs MFT records 0..6')
        self.cluster_size = cluster_size
        self.sector_size = 512
        self.record_size = 1024
        self.record_count = record_count
        self.total_clusters = total_clusters
        self.offset = partition_offset
        self.volume_size = cluster_size * total_clusters
        self.partition = SimpleNamespace(offset=partition_offset, size=self.volume_size)
        self.image = bytearray(partition_offset + self.volume_size)
        required_clusters = ceil(record_count * self.record_size / cluster_size)
        self.mft_runs = list(mft_runs or [(4, required_clusters)])
        if sum(count for _, count in self.mft_runs) < required_clusters:
            raise ValueError('MFT runs do not cover all fixture records')
        if any(lcn < 1 or count < 1 or lcn + count > total_clusters for lcn, count in self.mft_runs):
            raise ValueError('Invalid synthetic MFT run')
        boot = bytearray(512)
        boot[:3] = b'\xebR\x90'
        boot[3:11] = b'NTFS    '
        struct.pack_into('<H', boot, 11, 512)
        boot[13] = cluster_size // 512
        struct.pack_into('<Q', boot, 40, self.volume_size // 512)
        struct.pack_into('<Q', boot, 48, self.mft_runs[0][0])
        struct.pack_into('<Q', boot, 56, 2)
        struct.pack_into('<b', boot, 64, -10)
        struct.pack_into('<b', boot, 68, -12)
        struct.pack_into('<Q', boot, 72, 0x123456789abcdef0)
        boot[510:512] = b'\x55\xaa'
        self.image[partition_offset:partition_offset + 512] = boot
        self.image[partition_offset + self.volume_size - 512:partition_offset + self.volume_size] = boot
        self.write_record(0, [resident_attr(0x30, file_name('$MFT')),
                             nonresident_attr(self.mft_runs, record_count * self.record_size,
                                              cluster_size=cluster_size)], in_use=True)
        self.write_record(5, [resident_attr(0x30, file_name('.', flags=0x10000000))],
                          in_use=True, directory=True)
        self.set_bitmap()

    def reader(self):
        return MemoryReader(self.image)

    def _mft_ranges(self, logical, size):
        if logical < 0 or size < 0 or logical + size > self.record_count * self.record_size:
            raise ValueError('MFT fixture range exceeds initialized records')
        base = 0
        remaining = size
        for lcn, count in self.mft_runs:
            run_size = count * self.cluster_size
            if logical >= base + run_size:
                base += run_size
                continue
            within = max(0, logical - base)
            amount = min(remaining, run_size - within)
            if amount:
                yield self.offset + lcn * self.cluster_size + within, amount
                remaining -= amount
                logical += amount
            base += run_size
            if not remaining:
                return
        if remaining:
            raise ValueError('Incomplete MFT fixture mapping')

    def record_offset(self, index):
        return next(self._mft_ranges(index * self.record_size, 1))[0]

    def record_bytes(self, index):
        return b''.join(self.image[offset:offset + size]
                        for offset, size in self._mft_ranges(index * self.record_size, self.record_size))

    def patch_record(self, index, offset, payload):
        consumed = 0
        for physical, size in self._mft_ranges(index * self.record_size + offset, len(payload)):
            self.image[physical:physical + size] = payload[consumed:consumed + size]
            consumed += size

    def write_record(self, index, attrs, *, in_use=False, directory=False, **options):
        record = file_record(attrs, in_use=in_use, directory=directory,
                             record_id=index, record_size=self.record_size,
                             sector_size=self.sector_size, **options)
        self.patch_record(index, 0, record)

    def write_cluster(self, lcn, payload):
        if lcn < 0 or lcn * self.cluster_size + len(payload) > self.volume_size:
            raise ValueError('Cluster fixture write exceeds volume')
        start = self.offset + lcn * self.cluster_size
        self.image[start:start + len(payload)] = payload

    def set_bitmap(self, allocated=(), *, nonresident=False):
        bitmap = bytearray(ceil(self.total_clusters / 8))
        system = {0, 1, 2, 3, self.total_clusters - 1}
        for lcn, count in self.mft_runs:
            system.update(range(lcn, lcn + count))
        bitmap_lcn = self.total_clusters - 16
        if nonresident:
            system.add(bitmap_lcn)
        for lcn in system | set(allocated):
            if not 0 <= lcn < self.total_clusters:
                raise ValueError('Bitmap cluster outside fixture volume')
            bitmap[lcn // 8] |= 1 << (lcn % 8)
        if nonresident:
            self.write_cluster(bitmap_lcn, bitmap)
            data = nonresident_attr([(bitmap_lcn, 1)], len(bitmap), cluster_size=self.cluster_size)
        else:
            data = resident_attr(0x80, bitmap)
        self.write_record(6, [resident_attr(0x30, file_name('$Bitmap')), data], in_use=True)

    bitmap = set_bitmap

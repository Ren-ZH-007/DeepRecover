"""Which parts of a volume a live file system still uses, read from its own allocation records.

A signature hit whose whole byte range lies in allocated space is the current
content of an existing file or of file-system metadata, not leftover data from
a deleted file; signature scans skip such hits unless asked to keep them.
Unknown or damaged file systems give no map, so nothing is skipped.
"""
import struct

from core.computer.io import DiskError

_CHUNK = 64 * 1024


class AllocationMap:
    """Allocation units of ``unit`` bytes starting at ``start``; ``test(first, count)`` -> all allocated."""

    def __init__(self, file_system, start, unit, units, test):
        self.file_system, self.start, self.unit, self.units, self._test = file_system, start, unit, units, test

    def all_allocated(self, offset, size):
        """True only when every byte of [offset, offset + size) is in allocated space.

        Space before the first allocation unit (boot sectors, FAT copies) is
        file-system metadata and counts as in use; space after the last unit
        is outside the file system and never does. Read errors keep the hit.
        """
        end = offset + max(size, 1)
        if end <= self.start:
            return True
        first_byte = max(offset, self.start)
        if end > self.start + self.units * self.unit:
            return False
        first = (first_byte - self.start) // self.unit
        last = -(-(end - self.start) // self.unit)
        try:
            return self._test(first, last - first)
        except (OSError, ValueError, struct.error, DiskError):
            return False


def _all_bits(read, first, count):
    """True when bits [first, first + count) of a little-endian bitmap are all set."""
    while count:
        byte = first // 8
        span = min(count, _CHUNK * 8 - first % 8)
        data = read(byte, (first % 8 + span + 7) // 8)
        window = (int.from_bytes(data, 'little') >> (first % 8)) & ((1 << span) - 1)
        if window != (1 << span) - 1:
            return False
        first, count = first + span, count - span
    return True


def _fat32(reader, partition, token):
    from core.computer.fat import _Fat32
    volume = _Fat32(reader, partition, token)
    table = volume.fat_offsets[volume.active]

    def test(first, count):
        cluster = first + 2
        while count:
            take = min(count, _CHUNK // 4)
            values = struct.unpack('<%dI' % take, volume.read(table + cluster * 4, take * 4))
            if any(not value & 0x0FFFFFFF for value in values):
                return False
            cluster, count = cluster + take, count - take
        return True
    return AllocationMap('FAT32', partition.offset + volume.data_offset, volume.cluster_size, volume.clusters, test)


def _exfat(reader, partition, token):
    from core.computer.exfat import _Volume
    volume = _Volume(reader, partition, token, 1000000)
    volume.locate_bitmap()
    start = partition.offset + volume.heap_sector * volume.bps
    return AllocationMap('exFAT', start, volume.cluster_size, volume.cluster_count,
                         lambda first, count: _all_bits(volume.bitmap_read, first, count))


def _ntfs(reader, partition, token):
    from core.computer.ntfs import open_volume
    volume, _mft, bitmap = open_volume(reader, partition, token)
    return AllocationMap('NTFS', volume.offset, volume.cluster, volume.clusters,
                         lambda first, count: _all_bits(bitmap.stream.read_at, first, count))


def _apfs(reader, partition, token):
    from core.computer.apfs import _Container
    container = _Container(reader, partition, token, 1)
    return AllocationMap('APFS', partition.offset, container.bs, container.blocks,
                         lambda first, count: container.all_held(first, first + count))


_BUILDERS = {'FAT32': _fat32, 'exFAT': _exfat, 'NTFS': _ntfs, 'APFS': _apfs}


def allocation_map(reader, partition, token):
    """An AllocationMap for a supported, readable file system, else None."""
    build = _BUILDERS.get(partition.file_system)
    if build is None:
        return None
    try:
        return build(reader, partition, token)
    except (OSError, ValueError, struct.error, DiskError):
        return None

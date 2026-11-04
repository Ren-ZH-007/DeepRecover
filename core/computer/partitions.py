"""Bounded, read-only partition discovery. No mounting or filesystem repairs."""
from dataclasses import dataclass
import struct
import zlib


class PartitionError(ValueError):
    pass


@dataclass(frozen=True)
class Partition:
    offset: int
    size: int
    file_system: str
    label: str
    scheme: str = 'volume'
    index: int = 0
    warning: str = ''


def filesystem(reader, offset, size):
    boot = reader.read_at(offset, min(size, 512))
    if boot[3:11] == b'NTFS    ': return 'NTFS'
    if boot[3:11] == b'EXFAT   ': return 'exFAT'
    if boot[82:90] == b'FAT32   ': return 'FAT32'
    if boot[54:62] in (b'FAT12   ', b'FAT16   '): return boot[54:59].decode('ascii')
    if boot[32:36] == b'NXSB': return 'APFS'
    if boot[3:11] == b'-FVE-FS-': return 'BitLocker'
    if size >= 1026 and reader.read_at(offset + 1024, 2) in (b'H+', b'HX'): return 'HFS+'
    if size >= 1082 and reader.read_at(offset + 1080, 2) == b'\x53\xef': return 'ext4'
    return 'unknown'


def _bounds(offset, size, total):
    if offset < 0 or size <= 0 or offset > total or size > total - offset:
        raise PartitionError('分区范围超出源文件或磁盘。')


def _check_overlaps(partitions):
    ordered = sorted(partitions, key=lambda p: p.offset)
    for left, right in zip(ordered, ordered[1:]):
        if left.offset + left.size > right.offset:
            raise PartitionError('分区范围相互重叠，不能可靠解析删除记录。')
    return ordered


def _gpt(reader, sector):
    if reader.size < sector * 3: raise PartitionError('GPT 源不完整。')
    header = reader.read_at(sector, sector)
    if header[:8] != b'EFI PART': raise PartitionError('GPT 头缺失。')
    revision, length, checksum = struct.unpack_from('<III', header, 8)
    if revision != 0x10000 or not 92 <= length <= sector:
        raise PartitionError('不支持的 GPT 头版本或长度。')
    check = bytearray(header[:length]); check[16:20] = b'\0' * 4
    if zlib.crc32(check) != checksum: raise PartitionError('GPT 头校验失败。')
    current, backup, first, last = struct.unpack_from('<QQQQ', header, 24)
    table, count, entry_size, table_crc = struct.unpack_from('<QIII', header, 72)
    sectors = reader.size // sector
    if current != 1 or not 1 < backup < sectors or not 1 < first <= last < sectors:
        raise PartitionError('GPT 磁盘边界无效。')
    if not 1 <= count <= 4096 or not 128 <= entry_size <= 4096 or entry_size % 8:
        raise PartitionError('GPT 分区表数量或项大小超出支持范围。')
    table_size = count * entry_size
    if table_size > 16 * 1024 * 1024 or table < 2 or table * sector + table_size > first * sector:
        raise PartitionError('GPT 分区表范围无效。')
    _bounds(table * sector, table_size, reader.size)
    entries = reader.read_at(table * sector, table_size)
    if zlib.crc32(entries) != table_crc: raise PartitionError('GPT 分区表校验失败。')
    result = []
    for index in range(count):
        entry = entries[index * entry_size:(index + 1) * entry_size]
        if entry[:16] == b'\0' * 16: continue
        start, end = struct.unpack_from('<QQ', entry, 32)
        if not first <= start <= end <= last: raise PartitionError('GPT 分区超出可用范围。')
        offset, size = start * sector, (end - start + 1) * sector
        _bounds(offset, size, reader.size)
        try: name = entry[56:128].decode('utf-16-le').split('\0', 1)[0]
        except UnicodeError: name = ''
        kind = filesystem(reader, offset, size)
        result.append(Partition(offset, size, kind, name or f'GPT 分区 {index + 1}', 'GPT', index + 1))
    return _check_overlaps(result)


def _mbr_entries(block):
    if len(block) < 512 or block[510:512] != b'\x55\xaa':
        raise PartitionError('MBR/EBR 签名无效。')
    for index in range(4):
        data = block[446 + index * 16:462 + index * 16]
        yield index, data[4], *struct.unpack_from('<II', data, 8)


def _mbr(reader, sector):
    result = []
    extended = (0x05, 0x0f, 0x85)
    for index, kind, lba, sectors in _mbr_entries(reader.read_at(0, 512)):
        if kind == 0:
            if lba or sectors: raise PartitionError('MBR 空项包含无效范围。')
            continue
        if kind == 0xee: raise PartitionError('保护性 MBR 缺少可验证的 GPT。')
        if not lba: raise PartitionError('MBR 分区不能覆盖分区表。')
        offset, size = lba * sector, sectors * sector
        _bounds(offset, size, reader.size)
        if kind not in extended:
            result.append(Partition(offset, size, filesystem(reader, offset, size), f'MBR 分区 {index + 1}', 'MBR', index + 1))
            continue
        base, end, current, seen = offset, offset + size, offset, set()
        while True:
            if current in seen or len(seen) >= 128:
                raise PartitionError('扩展分区链存在循环或超过 128 项。')
            seen.add(current)
            if not base <= current <= end - 512: raise PartitionError('EBR 超出扩展分区范围。')
            entries = list(_mbr_entries(reader.read_at(current, 512)))
            _, data_kind, data_start, data_count = entries[0]
            if data_kind:
                part_offset, part_size = current + data_start * sector, data_count * sector
                if data_kind in extended or not data_start or part_offset < base or part_offset + part_size > end:
                    raise PartitionError('逻辑分区范围无效。')
                _bounds(part_offset, part_size, reader.size)
                result.append(Partition(part_offset, part_size, filesystem(reader, part_offset, part_size), f'逻辑分区 {len(result) + 1}', 'EBR', len(result) + 1))
            for _, extra_kind, extra_start, extra_count in entries[2:]:
                if extra_kind or extra_start or extra_count: raise PartitionError('不支持的 EBR 布局。')
            _, link_kind, link_start, link_count = entries[1]
            if not link_kind:
                if link_start or link_count: raise PartitionError('EBR 结束项无效。')
                break
            if link_kind not in extended or not link_start or not link_count:
                raise PartitionError('扩展分区链接无效。')
            current = base + link_start * sector
    return _check_overlaps(result)


def discover_partitions(reader):
    """Return real volumes; a damaged table is an error, never a guessed layout."""
    if reader.size <= 0: return []
    kind = filesystem(reader, 0, reader.size)
    if kind != 'unknown': return [Partition(0, reader.size, kind, '整个卷 / 镜像')]
    for sector in dict.fromkeys((getattr(reader, 'sector_size', 512), 512, 4096)):
        if sector in (512, 4096) and reader.size >= sector + 8 and reader.read_at(sector, 8) == b'EFI PART':
            return _gpt(reader, sector)
    if reader.size >= 512 and reader.read_at(510, 2) == b'\x55\xaa':
        return _mbr(reader, getattr(reader, 'sector_size', 512))
    return []


def source_partitions(reader):
    """Also expose the full source for explicit, deletion-unknown carving."""
    if reader.size <= 0: raise PartitionError('源文件或磁盘为空。')
    try:
        partitions = discover_partitions(reader)
    except PartitionError as error:
        return [Partition(0, reader.size, 'unknown', '整个源（分区表无法验证；仅签名扫描）', warning=str(error))]
    if len(partitions) == 1 and partitions[0].offset == 0: return partitions
    whole = Partition(0, reader.size, 'unknown', '整个磁盘 / 镜像（签名扫描）', 'disk')
    return [whole, *partitions]

"""Bounded, read-only NTFS deleted-record interpretation.

This module does not open devices, write files, or infer that old data survived.
It returns source extents in logical file order. USA-replaced resident bytes
refer back to the update sequence array, rather than exporting on-disk markers.
Unsupported or currently allocated data stays a non-exportable partial record.
"""
from collections import OrderedDict
from dataclasses import dataclass
import struct


class NTFSError(ValueError):
    pass


MAX_RUNS = 4096
MAX_RECORD_SIZE = 65536
UNSUPPORTED_FLAGS = 0x0001 | 0x4000 | 0x8000  # compressed / encrypted / sparse


def _integer(value):
    return type(value) is int


def _read(reader, offset, size):
    if not _integer(offset) or not _integer(size) or offset < 0 or size < 0 or offset + size > reader.size:
        raise NTFSError('NTFS 源范围超出磁盘或镜像边界。')
    data = reader.read_at(offset, size)
    if len(data) != size:
        raise NTFSError('NTFS 来源已截断，未读取完整元数据。')
    return data


@dataclass(frozen=True)
class _Volume:
    reader: object
    offset: int
    size: int
    sector: int
    cluster: int
    clusters: int
    record_size: int
    mft_lcn: int


class _Stream:
    def __init__(self, volume, extents, size):
        self.volume, self.extents, self.size = volume, tuple(extents), size
        if not _integer(size) or size < 0 or sum(length for _, length in self.extents) < size:
            raise NTFSError('NTFS 数据流的长度或范围不完整。')

    def segments(self, offset, size):
        if not _integer(offset) or not _integer(size) or offset < 0 or size < 0 or offset + size > self.size:
            raise NTFSError('NTFS 逻辑数据流读取越界。')
        result, logical, remaining = [], 0, size
        for physical, length in self.extents:
            if not remaining:
                break
            if offset >= logical + length:
                logical += length
                continue
            within = max(0, offset - logical)
            amount = min(remaining, length - within)
            if amount:
                result.append((physical + within, amount))
                remaining -= amount
                offset += amount
            logical += length
        if remaining:
            raise NTFSError('NTFS 逻辑数据流缺少物理范围。')
        return result

    def read_at(self, offset, size):
        return b''.join(_read(self.volume.reader, physical, length)
                        for physical, length in self.segments(offset, size))


def _volume(reader, partition):
    offset, size = partition.offset, partition.size
    if not _integer(offset) or not _integer(size) or offset < 0 or size < 512 or offset + size > reader.size:
        raise NTFSError('NTFS 分区边界无效。')
    boot = _read(reader, offset, 512)
    if boot[3:11] != b'NTFS    ' or boot[510:512] != b'\x55\xaa':
        raise NTFSError('所选分区没有有效的 NTFS 引导扇区。')
    sector = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    if sector not in (512, 1024, 2048, 4096) or not spc or spc > 128 or spc & (spc - 1):
        raise NTFSError('不支持或损坏的 NTFS 扇区/簇布局。')
    total_sectors, mft_lcn = struct.unpack_from('<Q', boot, 40)[0], struct.unpack_from('<Q', boot, 48)[0]
    volume_size, cluster = total_sectors * sector, sector * spc
    encoded = struct.unpack_from('<b', boot, 64)[0]
    if encoded < 0:
        if not 9 <= -encoded <= 16:
            raise NTFSError('不支持的 NTFS MFT 记录大小。')
        record_size = 1 << -encoded
    else:
        record_size = encoded * cluster
    if (volume_size > size or volume_size < cluster * 8 or record_size < sector
            or record_size > MAX_RECORD_SIZE or record_size % sector
            or mft_lcn < 1 or mft_lcn * cluster + record_size > volume_size):
        raise NTFSError('NTFS 容量、MFT 位置或记录布局超出支持范围。')
    return _Volume(reader, offset, volume_size, sector, cluster,
                   volume_size // cluster, record_size, mft_lcn)


def _fixup(raw, sector):
    if len(raw) < 48 or raw[:4] != b'FILE' or len(raw) % sector:
        raise NTFSError('NTFS MFT 记录签名或长度损坏。')
    usa_offset, count = struct.unpack_from('<HH', raw, 4)
    if (count != len(raw) // sector + 1 or usa_offset < 42 or usa_offset % 2
            or usa_offset + count * 2 > sector - 2):
        raise NTFSError('NTFS 更新序列数组布局损坏。')
    marker = raw[usa_offset:usa_offset + 2]
    if marker == b'\0\0':
        raise NTFSError('NTFS 更新序列标记无效。')
    fixed = bytearray(raw)
    for index in range(1, count):
        end = index * sector - 2
        if raw[end:end + 2] != marker:
            raise NTFSError('NTFS 更新序列不匹配，记录可能已损坏或正在改变。')
        fixed[end:end + 2] = raw[usa_offset + index * 2:usa_offset + index * 2 + 2]
    used, allocated = struct.unpack_from('<II', fixed, 24)
    first = struct.unpack_from('<H', fixed, 20)[0]
    if (allocated != len(raw) or used > len(raw) or first % 8
            or first < usa_offset + count * 2 or first + 4 > used):
        raise NTFSError('NTFS MFT 属性边界损坏。')
    return bytes(fixed), usa_offset


def _attributes(record):
    position, used = struct.unpack_from('<H', record, 20)[0], struct.unpack_from('<I', record, 24)[0]
    attrs = []
    while position + 4 <= used:
        kind = struct.unpack_from('<I', record, position)[0]
        if kind == 0xffffffff:
            return attrs
        if position + 16 > used:
            break
        length = struct.unpack_from('<I', record, position + 4)[0]
        nonresident, name_length = record[position + 8:position + 10]
        minimum = 64 if nonresident == 1 else 24
        if nonresident not in (0, 1) or length < minimum or length % 8 or position + length > used:
            break
        data = record[position:position + length]
        name_offset, flags = struct.unpack_from('<HH', data, 10)
        if name_length:
            if name_offset < minimum or name_offset + name_length * 2 > length:
                break
            try:
                name = data[name_offset:name_offset + name_length * 2].decode('utf-16le', errors='strict')
            except UnicodeError:
                break
        else:
            name = ''
        value_offset = None
        value = None
        if not nonresident:
            value_size, value_offset = struct.unpack_from('<IH', data, 16)
            if (value_offset < minimum or value_offset + value_size > length
                    or name_length and value_offset < name_offset + name_length * 2):
                break
            value = data[value_offset:value_offset + value_size]
        attrs.append({'type': kind, 'name': name, 'flags': flags, 'nonresident': bool(nonresident),
                      'data': data, 'offset': position, 'value': value, 'value_offset': value_offset})
        position += length
    raise NTFSError('NTFS 属性列表不完整或越界。')


def _runs(data, volume):
    lowest, highest = struct.unpack_from('<QQ', data, 16)
    position, compression = struct.unpack_from('<HH', data, 32)
    if lowest != 0 or position < 64 or position >= len(data) or compression:
        raise NTFSError('NTFS 分段属性或压缩布局尚不支持。')
    allocated, size, initialized = struct.unpack_from('<QQQ', data, 40)
    if size > volume.size or initialized != size:
        raise NTFSError('NTFS 数据长度越界或包含尚未初始化的数据。')
    result, lcn, clusters = [], 0, 0
    while position < len(data):
        header = data[position]; position += 1
        if not header:
            break
        length_bytes, offset_bytes = header & 15, header >> 4
        if (not 1 <= length_bytes <= 8 or not 1 <= offset_bytes <= 8
                or position + length_bytes + offset_bytes > len(data) or len(result) >= MAX_RUNS):
            raise NTFSError('NTFS 数据运行列表损坏、稀疏或过于复杂。')
        count = int.from_bytes(data[position:position + length_bytes], 'little')
        position += length_bytes
        lcn += int.from_bytes(data[position:position + offset_bytes], 'little', signed=True)
        position += offset_bytes
        if count < 1 or lcn < 0 or lcn + count > volume.clusters:
            raise NTFSError('NTFS 数据簇超出分区边界。')
        result.append((volume.offset + lcn * volume.cluster, count * volume.cluster))
        clusters += count
    else:
        raise NTFSError('NTFS 数据运行列表没有终止符。')
    if ((clusters and highest != clusters - 1) or (not clusters and size)
            or allocated != clusters * volume.cluster or size > allocated):
        raise NTFSError('NTFS 运行列表和属性大小不一致。')
    previous_end = -1
    for physical, length in sorted(result):
        if physical < previous_end:
            raise NTFSError('NTFS 数据运行列表存在重复或重叠簇。')
        previous_end = physical + length
    return _Stream(volume, result, size)


def _resident_stream(attr, record_number, usa_offset, mft, volume):
    start = attr['offset'] + attr['value_offset']
    end, point, extents = start + len(attr['value']), start, []
    logical_record = record_number * volume.record_size
    while point < end:
        sector_index = point // volume.sector + 1
        trailer = sector_index * volume.sector - 2
        if point < trailer:
            amount = min(end - point, trailer - point)
            extents.extend(mft.segments(logical_record + point, amount))
        else:
            amount = min(end - point, sector_index * volume.sector - point)
            original = usa_offset + sector_index * 2 + point - trailer
            extents.extend(mft.segments(logical_record + original, amount))
        point += amount
    return _Stream(volume, extents, len(attr['value']))


def _data_stream(attrs, record_number, usa_offset, mft, volume):
    if any(attr['type'] == 0x20 for attr in attrs):
        raise NTFSError('NTFS ATTRIBUTE_LIST 扩展记录尚不支持。')
    data = [attr for attr in attrs if attr['type'] == 0x80 and not attr['name']]
    if len(data) != 1:
        raise NTFSError('NTFS 未命名数据属性缺失或重复。')
    attr = data[0]
    if attr['flags'] & UNSUPPORTED_FLAGS:
        raise NTFSError('NTFS 压缩、加密或稀疏数据尚不支持。')
    return (_runs(attr['data'], volume) if attr['nonresident'] else
            _resident_stream(attr, record_number, usa_offset, mft, volume)), attr


def _filename(attrs):
    names = []
    for attr in attrs:
        value = attr['value']
        if attr['type'] != 0x30 or attr['nonresident'] or attr['name'] or value is None:
            continue
        if len(value) < 66 or value[65] > 3 or not 1 <= value[64] <= 255 or 66 + value[64] * 2 > len(value):
            raise NTFSError('NTFS 文件名属性损坏。')
        try:
            name = value[66:66 + value[64] * 2].decode('utf-16le', errors='strict')
        except UnicodeError as error:
            raise NTFSError('NTFS 文件名编码损坏。') from error
        if '\0' in name or '/' in name or name in ('.', '..'):
            continue
        reference = struct.unpack_from('<Q', value)[0]
        names.append((value[65] in (1, 3), name, reference))
    if not names:
        raise NTFSError('NTFS 删除记录没有可确认的文件名。')
    _, name, reference = max(names, key=lambda item: item[0])
    return name, reference


class _Bitmap:
    def __init__(self, stream, clusters, token):
        if stream.size < (clusters + 7) // 8:
            raise NTFSError('NTFS $Bitmap 缺失完整的簇分配信息。')
        self.stream, self.clusters, self.token, self.cache = stream, clusters, token, OrderedDict()

    def allocated(self, cluster):
        self.token.checkpoint()
        if not 0 <= cluster < self.clusters:
            raise NTFSError('NTFS 分配位图索引越界。')
        page = cluster // (4096 * 8)
        if page not in self.cache:
            start = page * 4096
            self.cache[page] = self.stream.read_at(start, min(4096, self.stream.size - start))
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(page)
        return bool(self.cache[page][cluster // 8 - page * 4096] & (1 << (cluster & 7)))


def scan_ntfs(reader, partition, token, *, max_records=1000000, max_candidates=10000):
    """Yield deleted NTFS file records; valid source extents need content hashing.

    Compressed/encrypted/sparse, ATTRIBUTE_LIST and still-allocated clusters are
    visible as partial records with no extents. Corrupt FILE records never gain
    a verified deletion state. Limits fail explicitly and preserve prior yields.
    """
    if not _integer(max_records) or not _integer(max_candidates) or min(max_records, max_candidates) < 1:
        raise ValueError('Invalid NTFS scan limits')
    token.checkpoint()
    volume = _volume(reader, partition)
    first_offset = volume.offset + volume.mft_lcn * volume.cluster
    raw_first = _read(reader, first_offset, volume.record_size)
    first, _ = _fixup(raw_first, volume.sector)
    if not struct.unpack_from('<H', first, 22)[0] & 1:
        raise NTFSError('NTFS $MFT 系统记录不是在用记录。')
    attrs = _attributes(first)
    if any(attr['type'] == 0x20 for attr in attrs):
        raise NTFSError('NTFS $MFT 扩展运行列表尚不支持。')
    mft_data = [attr for attr in attrs if attr['type'] == 0x80 and not attr['name']]
    if len(mft_data) != 1 or not mft_data[0]['nonresident'] or mft_data[0]['flags'] & UNSUPPORTED_FLAGS:
        raise NTFSError('NTFS $MFT 数据布局不支持。')
    mft = _runs(mft_data[0]['data'], volume)
    if mft.size < volume.record_size * 7 or mft.size % volume.record_size or mft.read_at(0, volume.record_size) != raw_first:
        raise NTFSError('NTFS $MFT 长度或首记录映射不一致。')
    raw_bitmap = mft.read_at(6 * volume.record_size, volume.record_size)
    bitmap_record, bitmap_usa = _fixup(raw_bitmap, volume.sector)
    if not struct.unpack_from('<H', bitmap_record, 22)[0] & 1:
        raise NTFSError('NTFS $Bitmap 系统记录不是在用记录。')
    bitmap_stream, _ = _data_stream(_attributes(bitmap_record), 6, bitmap_usa, mft, volume)
    bitmap = _Bitmap(bitmap_stream, volume.clusters, token)
    count, total = 0, mft.size // volume.record_size
    token.report_progress(0, total, count, phase='computer')
    for number in range(min(total, max_records)):
        token.checkpoint()
        if number and number % 128 == 0:
            token.report_progress(number, total, count, phase='computer')
        raw = mft.read_at(number * volume.record_size, volume.record_size)
        if raw[:4] != b'FILE':
            continue  # unused, never-initialized MFT slots are not lost files
        flags = struct.unpack_from('<H', raw, 22)[0]
        if flags & 3 or struct.unpack_from('<Q', raw, 32)[0]:
            continue  # live files, directories and extension records
        if count >= max_candidates:
            raise NTFSError('NTFS 删除候选数量达到上限，扫描仅部分完成。')
        candidate = {'name': f'MFT_{number}.bin', 'original_path': f'[$MFT record {number}]',
                     'size': 0, 'extents': [], 'record_id': number, 'integrity': 'invalid_record',
                     'status': 'partial', 'file_system': 'NTFS', 'deletion_state': 'unverified_record'}
        try:
            fixed, usa = _fixup(raw, volume.sector)
            attrs = _attributes(fixed)
            name, _ = _filename(attrs)
            candidate.update(name=name, original_path=f'[$MFT record {number}]/{name}',
                             deletion_state='deleted_record', integrity='unsupported_layout')
            declared_data = [attr for attr in attrs if attr['type'] == 0x80 and not attr['name']]
            if len(declared_data) == 1:
                data_attr = declared_data[0]
                candidate['size'] = (struct.unpack_from('<Q', data_attr['data'], 48)[0]
                                     if data_attr['nonresident'] else len(data_attr['value']))
            if candidate['size'] > 8 * 1024 ** 3:
                candidate['integrity'] = 'size_limit'
                raise NTFSError('候选超过 8 GiB 单文件处理上限。')
            stream, data_attr = _data_stream(attrs, number, usa, mft, volume)
            extents = stream.segments(0, stream.size)
            allocated = False
            if data_attr['nonresident']:
                for physical, length in extents:
                    start = (physical - volume.offset) // volume.cluster
                    for cluster in range(start, start + (length + volume.cluster - 1) // volume.cluster):
                        if bitmap.allocated(cluster):
                            allocated = True
                            break
                    if allocated:
                        break
            candidate.update(size=stream.size, integrity='allocated_clusters' if allocated else 'metadata_checked')
            if not allocated:
                candidate.update(extents=extents, status='candidate')
        except NTFSError:
            pass  # keep a non-exportable diagnostic record, never guessed bytes
        count += 1
        token.report_progress(number + 1, total, count, phase='computer')
        yield candidate
    if total > max_records:
        raise NTFSError('NTFS MFT 记录数量达到上限，扫描仅部分完成。')
    token.checkpoint()
    token.report_progress(total, total, count, phase='computer')

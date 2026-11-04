"""Read-only exFAT deleted-entry candidates.

Only an intact deleted entry set with NoFatChain and currently free allocation
bits supplies recoverable extents. A deleted fragmented chain is never guessed.
"""
from collections import deque
import struct


def _u16(data, offset):
    return struct.unpack_from('<H', data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0]


class _Volume:
    def __init__(self, reader, partition, token, max_entries):
        self.reader, self.token = reader, token
        self.base, self.limit = partition.offset, partition.size
        self.budget, self.examined = max_entries, 0
        if (type(self.base) is not int or type(self.limit) is not int or
                self.base < 0 or self.limit < 512 or self.base + self.limit > reader.size):
            raise ValueError('exFAT 分区范围无效。')
        boot = self.read(0, 512)
        if boot[3:11] != b'EXFAT   ' or boot[11:64] != bytes(53) or boot[510:512] != b'\x55\xaa':
            raise ValueError('不是受支持的 exFAT 引导扇区。')
        bps_shift, spc_shift = boot[108], boot[109]
        if not 9 <= bps_shift <= 12 or not 0 <= spc_shift <= 25 - bps_shift:
            raise ValueError('exFAT 扇区或簇大小无效。')
        self.bps, self.cluster_size = 1 << bps_shift, 1 << (bps_shift + spc_shift)
        self.volume_sectors = _u64(boot, 72)
        self.fat_start, self.fat_sectors = _u32(boot, 80), _u32(boot, 84)
        self.heap_sector, self.cluster_count, self.root_cluster = _u32(boot, 88), _u32(boot, 92), _u32(boot, 96)
        self.fats, flags = boot[110], _u16(boot, 106)
        self.active_fat = flags & 1
        if (_u16(boot, 104) != 0x0100 or self.fats not in (1, 2) or
                self.active_fat >= self.fats or not 1 <= self.cluster_count <= 0xfffffff5 or
                self.volume_sectors * self.bps > self.limit or self.volume_sectors < 24 or
                self.fat_start < 24 or self.fat_sectors * self.bps < (self.cluster_count + 2) * 4 or
                self.heap_sector < self.fat_start + self.fats * self.fat_sectors or
                self.heap_sector * self.bps + self.cluster_count * self.cluster_size > self.volume_sectors * self.bps):
            raise ValueError('exFAT 几何信息不完整或超出分区。')
        self.check_cluster(self.root_cluster)
        self._check_boot_checksum()
        self.bitmap = None
        self.bitmap_length = 0

    def read(self, offset, size):
        self.token.checkpoint()
        if offset < 0 or size < 0 or offset + size > self.limit:
            raise ValueError('exFAT 读取范围超出分区。')
        data = self.reader.read_at(self.base + offset, size)
        if len(data) != size:
            raise ValueError('exFAT 来源已截断。')
        return data

    def _check_boot_checksum(self):
        checksum = 0
        for sector in range(11):
            self.token.checkpoint()
            data = self.read(sector * self.bps, self.bps)
            for index, value in enumerate(data):
                if sector == 0 and index in (106, 107, 112):
                    continue
                checksum = (((checksum & 1) << 31) | (checksum >> 1)) + value
                checksum &= 0xffffffff
        expected = self.read(11 * self.bps, self.bps)
        if any(value[0] != checksum for value in struct.iter_unpack('<I', expected)):
            raise ValueError('exFAT 主引导区校验失败。')

    def check_cluster(self, cluster):
        if type(cluster) is not int or not 2 <= cluster < self.cluster_count + 2:
            raise ValueError('exFAT 簇位置无效。')

    def cluster_offset(self, cluster):
        self.check_cluster(cluster)
        return self.heap_sector * self.bps + (cluster - 2) * self.cluster_size

    def next_cluster(self, cluster):
        self.check_cluster(cluster)
        offset = (self.fat_start + self.active_fat * self.fat_sectors) * self.bps + cluster * 4
        value = _u32(self.read(offset, 4), 0)
        if value >= 0xfffffff8:
            return None
        self.check_cluster(value)
        return value

    def clusters(self, first, count=None, contiguous=False):
        self.check_cluster(first)
        if count is not None and (count < 1 or count > self.budget):
            raise ValueError('exFAT 链长度超过扫描上限。')
        cluster, visited, emitted = first, set(), 0
        while cluster is not None and (count is None or emitted < count):
            self.token.checkpoint()
            self.check_cluster(cluster)
            if cluster in visited or emitted >= self.budget:
                raise ValueError('exFAT 簇链循环或超过扫描上限。')
            visited.add(cluster)
            next_value = cluster + 1 if contiguous else self.next_cluster(cluster)
            yield cluster
            emitted += 1
            if count is not None and emitted == count:
                return
            cluster = next_value
        if count is not None and emitted != count:
            raise ValueError('exFAT 簇链提前结束。')

    def layout(self, first, size, contiguous=False):
        if size == 0:
            return []
        count = (size + self.cluster_size - 1) // self.cluster_size
        if contiguous:
            self.check_cluster(first)
            self.check_cluster(first + count - 1)
            return [(self.base + self.cluster_offset(first), size)]
        extents, remaining = [], size
        for cluster in self.clusters(first, count, contiguous):
            physical = self.base + self.cluster_offset(cluster)
            length = min(remaining, self.cluster_size)
            if extents and extents[-1][0] + extents[-1][1] == physical:
                extents[-1] = (extents[-1][0], extents[-1][1] + length)
            else:
                extents.append((physical, length))
            remaining -= length
        return extents

    def entries(self, first, size=None, contiguous=False):
        if size is not None and (size <= 0 or size % self.cluster_size):
            raise ValueError('exFAT 目录长度无效。')
        count = None if size is None else size // self.cluster_size
        for cluster in self.clusters(first, count, contiguous):
            if self.bitmap is not None and not self.allocated(cluster):
                raise ValueError('exFAT 分配位图与活动目录冲突。')
            offset = self.cluster_offset(cluster)
            for start in range(0, self.cluster_size, 65536):
                block = self.read(offset + start, min(65536, self.cluster_size - start))
                for index in range(0, len(block), 32):
                    self.token.checkpoint()
                    if self.examined >= self.budget:
                        raise ValueError('exFAT 目录记录超过扫描上限，结果不完整。')
                    self.examined += 1
                    entry = block[index:index + 32]
                    if entry[0] == 0:
                        return
                    yield self.base + offset + start + index, entry

    def locate_bitmap(self):
        found = []
        for _, entry in self.entries(self.root_cluster):
            if entry[0] == 0x81 and entry[1] & 1 == self.active_fat:
                if entry[1] & ~1:
                    raise ValueError('exFAT 分配位图标记无效。')
                found.append((_u32(entry, 20), _u64(entry, 24)))
        if len(found) != 1:
            raise ValueError('缺少唯一的 exFAT 活动分配位图，不能判断簇是否已重用。')
        first, size = found[0]
        if size < (self.cluster_count + 7) // 8:
            raise ValueError('exFAT 分配位图不完整。')
        self.bitmap = self.layout(first, size)
        self.bitmap_length = size
        metadata_clusters = [self.root_cluster]
        for physical, length in self.bitmap:
            cluster = (physical - self.base - self.heap_sector * self.bps) // self.cluster_size + 2
            metadata_clusters.extend(range(cluster, cluster + (length + self.cluster_size - 1) // self.cluster_size))
        for cluster in metadata_clusters:
            self.token.checkpoint()
            if not self.allocated(cluster):
                raise ValueError('exFAT 分配位图与系统元数据冲突。')

    def bitmap_read(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.bitmap_length:
            raise ValueError('exFAT 位图读取越界。')
        result, position = bytearray(), 0
        for physical, length in self.bitmap:
            if position + length <= offset:
                position += length
                continue
            relative = max(0, offset - position)
            take = min(length - relative, size - len(result))
            result.extend(self.read(physical - self.base + relative, take))
            offset += take
            position += length
            if len(result) == size:
                return bytes(result)
        raise ValueError('exFAT 分配位图已截断。')

    def is_free(self, first, count):
        if not count:
            return True
        self.check_cluster(first)
        self.check_cluster(first + count - 1)
        first_bit, last_bit = first - 2, first - 2 + count
        start, end = first_bit // 8, (last_bit + 7) // 8
        while start < end:
            self.token.checkpoint()
            take = min(65536, end - start)
            data = bytearray(self.bitmap_read(start, take))
            if start == first_bit // 8:
                data[0] &= (0xff << (first_bit % 8)) & 0xff
            if start + take == end and last_bit % 8:
                data[-1] &= (1 << (last_bit % 8)) - 1
            if any(data):
                return False
            start += take
        return True

    def allocated(self, cluster):
        self.check_cluster(cluster)
        bit = cluster - 2
        return bool(self.bitmap_read(bit // 8, 1)[0] & (1 << (bit % 8)))


def _entry_set(iterator, primary, volume):
    secondary_count = primary[1]
    if secondary_count < 2:
        raise ValueError('exFAT 文件目录项缺少必要附属记录。')
    entries = [primary]
    for _ in range(secondary_count):
        volume.token.checkpoint()
        try:
            _, entry = next(iterator)
        except StopIteration as error:
            raise ValueError('exFAT 文件目录项被截断。') from error
        entries.append(entry)
    deleted = primary[0] == 0x05
    checksum = 0
    for entry_index, entry in enumerate(entries):
        for index, value in enumerate(entry):
            if entry_index == 0 and index in (2, 3):
                continue
            if index == 0 and deleted:
                value |= 0x80
            checksum = (((checksum & 1) << 15) | (checksum >> 1)) + value
            checksum &= 0xffff
    if checksum != _u16(primary, 2):
        raise ValueError('exFAT 文件目录项校验失败，不能信任删除布局。')
    expected_stream = 0x40 if deleted else 0xc0
    expected_name = 0x41 if deleted else 0xc1
    if entries[1][0] != expected_stream:
        raise ValueError('exFAT 文件流记录无效。')
    stream = entries[1]
    name_length = stream[3]
    if not 1 <= name_length <= 255 or stream[1] & ~3 or not stream[1] & 1:
        raise ValueError('exFAT 文件流标记或名称长度无效。')
    name_entries = []
    for entry in entries[2:]:
        if entry[0] == expected_name:
            name_entries.append(entry)
        elif bool(entry[0] & 0x80) == deleted or not entry[0] & 0x40 or not entry[0] & 0x20:
            raise ValueError('exFAT 包含不支持的附属目录记录。')
    if len(name_entries) != (name_length + 14) // 15:
        raise ValueError('exFAT 文件名记录数量无效。')
    raw = b''.join(entry[2:32] for entry in name_entries)[:name_length * 2]
    try:
        name = raw.decode('utf-16-le', errors='strict')
    except UnicodeError as error:
        raise ValueError('exFAT 文件名编码损坏。') from error
    if name in ('.', '..') or any(ord(c) < 32 or c in '"*/:<>?\\|' for c in name):
        raise ValueError('exFAT 文件名包含无效字符。')
    return name, stream


def scan_exfat(reader, partition, token, *, max_entries=1000000, max_candidates=10000):
    """Yield deleted exFAT metadata; never restore source allocation tables."""
    if type(max_entries) is not int or type(max_candidates) is not int or min(max_entries, max_candidates) < 1:
        raise ValueError('exFAT 扫描上限无效。')
    volume = _Volume(reader, partition, token, max_entries)
    volume.locate_bitmap()
    pending = deque([(volume.root_cluster, None, False, '', 0)])
    directories, candidates = set(), 0
    while pending:
        first, directory_size, contiguous, parent, depth = pending.popleft()
        if first in directories:
            raise ValueError('exFAT 目录簇重复或循环。')
        directories.add(first)
        iterator = iter(volume.entries(first, directory_size, contiguous))
        for record_offset, primary in iterator:
            if primary[0] not in (0x05, 0x85):
                continue
            name, stream = _entry_set(iterator, primary, volume)
            first_cluster, size, valid_size = _u32(stream, 20), _u64(stream, 24), _u64(stream, 8)
            path = parent + '/' + name
            if len(path) > 4096:
                raise ValueError('exFAT 路径超过 4096 字符扫描上限。')
            is_directory, deleted = bool(_u16(primary, 4) & 0x10), primary[0] == 0x05
            no_fat_chain = bool(stream[1] & 2)
            if is_directory:
                if not deleted:
                    if valid_size != size:
                        raise ValueError('exFAT 目录初始化长度无效。')
                    if depth >= 128:
                        raise ValueError('exFAT 目录深度超过 128 层扫描上限。')
                    pending.append((first_cluster, size, no_fat_chain, path, depth + 1))
                continue
            if not deleted:
                continue
            if candidates >= max_candidates:
                raise ValueError('exFAT 删除候选达到上限，扫描未完成。')
            candidates += 1
            integrity, status, extents = 'metadata_checked', 'candidate', []
            if size > 8 * 1024 ** 3:
                integrity, status = 'size_limit', 'partial'
            elif size > volume.cluster_count * volume.cluster_size or valid_size > size:
                integrity, status = 'invalid_metadata', 'partial'
            elif valid_size != size:
                integrity, status = 'uninitialized_data', 'partial'
            elif not size:
                if first_cluster != 0:
                    integrity, status = 'invalid_metadata', 'partial'
            elif not no_fat_chain:
                integrity, status = 'layout_unverified', 'partial'
            else:
                count = (size + volume.cluster_size - 1) // volume.cluster_size
                try:
                    free = volume.is_free(first_cluster, count)
                except ValueError:
                    integrity, status = 'invalid_metadata', 'partial'
                else:
                    if not free:
                        integrity, status = 'allocated_clusters', 'partial'
                    else:
                        extents = volume.layout(first_cluster, size, contiguous=True)
            yield {'name': name, 'original_path': path, 'size': size, 'extents': extents,
                   'record_id': record_offset, 'integrity': integrity, 'status': status,
                   'file_system': 'exFAT', 'deletion_state': 'deleted_record'}

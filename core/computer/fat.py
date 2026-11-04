"""Bounded FAT32 deleted-directory-entry inspection; never writes the source.

Deleting a FAT32 file normally erases its FAT chain. Only an empty file or an
unallocated single-cluster file has a layout this parser can establish from the
remaining metadata. Larger files remain visible as non-exportable candidates.
"""
from collections import deque
import struct


class _FatMismatch(ValueError):
    pass


def _u16(data, offset):
    return struct.unpack_from('<H', data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def _short_checksum(name):
    checksum = 0
    for byte in name:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 0xff
    return checksum


def _short_name(entry, deleted=False):
    raw = bytearray(entry[:11])
    raw[0] = ord('_') if deleted else (0xe5 if raw[0] == 0x05 else raw[0])
    if any(byte < 0x20 or byte == 0x7f or byte in b'"*+,/:;<=>?[\\]|' for byte in raw):
        return None
    stem = bytes(raw[:8]).rstrip(b' ').decode('cp437', errors='replace')
    extension = bytes(raw[8:]).rstrip(b' ').decode('cp437', errors='replace')
    if entry[12] & 0x08:
        stem = stem.lower()
    if entry[12] & 0x10:
        extension = extension.lower()
    return stem + ('.' + extension if extension else '')


def _long_name(entries, short, deleted):
    if not entries or len(entries) > 20:
        return None
    if any(entry[11] != 0x0f or entry[12] != 0 or entry[26:28] != b'\0\0' for entry in entries):
        return None
    checksums = {entry[13] for entry in entries}
    if len(checksums) != 1:
        return None
    checksum = checksums.pop()
    if deleted:
        if any(entry[0] != 0xe5 for entry in entries):
            return None
        # The original first short-name byte was overwritten. Check that a
        # legal first byte could have produced this preserved LFN checksum.
        forbidden = b'"*+,./:;<=>?[\\]|'
        first_bytes = [5] + [byte for byte in range(0x21, 0x100)
                             if byte != 0xe5 and byte not in forbidden]
        if not any(_short_checksum(bytes([byte]) + short[1:11]) == checksum for byte in first_bytes):
            return None
    else:
        expected = list(range(len(entries), 0, -1))
        if [entry[0] & 0x1f for entry in entries] != expected:
            return None
        if not entries[0][0] & 0x40 or any(entry[0] & 0x40 for entry in entries[1:]):
            return None
        if any(entry[0] & 0xa0 for entry in entries) or _short_checksum(short[:11]) != checksum:
            return None
    data = b''.join(entry[1:11] + entry[14:26] + entry[28:32] for entry in reversed(entries))
    words = list(struct.unpack('<' + 'H' * (len(data) // 2), data))
    if 0 in words:
        ending = words.index(0)
        if any(word != 0xffff for word in words[ending + 1:]):
            return None
        words = words[:ending]
    if not words or len(words) > 255 or 0xffff in words:
        return None
    try:
        name = struct.pack('<' + 'H' * len(words), *words).decode('utf-16-le')
    except UnicodeDecodeError:
        return None
    if name in ('.', '..') or any(ord(char) < 32 or char in '/\\' for char in name):
        return None
    return name


class _Fat32:
    def __init__(self, reader, partition, token):
        self.reader, self.token = reader, token
        self.base, self.limit = partition.offset, partition.size
        if type(self.base) is not int or type(self.limit) is not int or self.base < 0 or self.limit < 512:
            raise ValueError('FAT32 分区范围无效。')
        if self.base + self.limit > reader.size:
            raise ValueError('FAT32 分区超出只读来源。')
        boot = self.read(0, 512)
        if boot[510:512] != b'\x55\xaa':
            raise ValueError('FAT32 引导扇区签名无效。')
        self.sector = _u16(boot, 11)
        self.spc, self.reserved, self.fats = boot[13], _u16(boot, 14), boot[16]
        total, fat_size = _u32(boot, 32), _u32(boot, 36)
        if self.sector not in (512, 1024, 2048, 4096) or self.spc not in (1, 2, 4, 8, 16, 32, 64, 128):
            raise ValueError('不支持此 FAT32 扇区或簇大小。')
        if not self.reserved or self.fats not in (1, 2) or not fat_size or not total:
            raise ValueError('FAT32 几何信息无效。')
        if _u16(boot, 17) or _u16(boot, 19) or _u16(boot, 22) or _u16(boot, 42):
            raise ValueError('来源不是受支持的 FAT32 卷。')
        overhead = self.reserved + self.fats * fat_size
        self.total_bytes = total * self.sector
        if overhead >= total or self.total_bytes > self.limit:
            raise ValueError('FAT32 卷大小或 FAT 范围无效。')
        self.clusters = (total - overhead) // self.spc
        if not 65525 <= self.clusters < 0x0ffffff5 or fat_size * self.sector // 4 < self.clusters + 2:
            raise ValueError('来源不是有效的 FAT32 簇布局。')
        self.cluster_size = self.sector * self.spc
        self.fat_offsets = [(self.reserved + number * fat_size) * self.sector for number in range(self.fats)]
        flags = _u16(boot, 40)
        self.mirrored = not bool(flags & 0x80)
        self.active = 0 if self.mirrored else flags & 0x0f
        if self.active >= self.fats:
            raise ValueError('FAT32 活动 FAT 索引无效。')
        self.data_offset = overhead * self.sector
        self.root = _u32(boot, 44) & 0x0fffffff
        self.require_cluster(self.root)

    def read(self, offset, size):
        self.token.checkpoint()
        if offset < 0 or size < 0 or offset + size > self.limit:
            raise ValueError('FAT32 读取范围超出分区。')
        result = self.reader.read_at(self.base + offset, size)
        if len(result) != size:
            raise ValueError('FAT32 来源已截断。')
        return result

    def require_cluster(self, cluster):
        if not 2 <= cluster < self.clusters + 2:
            raise ValueError('FAT32 簇编号超出数据区域。')

    def cluster_offset(self, cluster):
        self.require_cluster(cluster)
        return self.data_offset + (cluster - 2) * self.cluster_size

    def fat_value(self, cluster):
        self.require_cluster(cluster)
        value = _u32(self.read(self.fat_offsets[self.active] + cluster * 4, 4), 0) & 0x0fffffff
        if self.mirrored:
            for number in range(1, self.fats):
                other = _u32(self.read(self.fat_offsets[number] + cluster * 4, 4), 0) & 0x0fffffff
                if value != other:
                    raise _FatMismatch('FAT32 的 FAT 副本不一致，无法可靠判断分配状态。')
        return value

    def directory_clusters(self, first, owned):
        cluster, visited = first, set()
        while True:
            self.require_cluster(cluster)
            if cluster in visited or cluster in owned:
                raise ValueError('FAT32 目录链循环或目录共享簇，扫描未完成。')
            visited.add(cluster)
            owned.add(cluster)
            following = self.fat_value(cluster)
            if following < 0x0ffffff8 and (following in (0, 1, 0x0ffffff7) or following >= 0x0ffffff0):
                raise ValueError('FAT32 目录链已损坏，扫描未完成。')
            if following in visited:
                raise ValueError('FAT32 目录链循环，扫描未完成。')
            yield cluster
            if following >= 0x0ffffff8:
                return
            cluster = following

    def candidate(self, entry, name, parent, record_id):
        size = _u32(entry, 28)
        cluster = ((_u16(entry, 20) << 16) | _u16(entry, 26)) & 0x0fffffff
        integrity, extents = 'metadata_checked', []
        if size == 0 and cluster == 0:
            pass
        elif not 2 <= cluster < self.clusters + 2 or size > self.clusters * self.cluster_size:
            integrity = 'invalid_extent'
        else:
            try:
                allocated = self.fat_value(cluster) != 0
            except _FatMismatch:
                integrity = 'allocation_unknown'
            else:
                if allocated:
                    integrity = 'allocated_clusters'
                elif size > self.cluster_size:
                    integrity = 'layout_unverified'
                elif size:
                    extents = [(self.base + self.cluster_offset(cluster), size)]
        return {'name': name, 'original_path': parent + '/' + name, 'size': size,
                'extents': extents, 'record_id': record_id, 'file_system': 'FAT32',
                'deletion_state': 'deleted_record', 'integrity': integrity,
                'status': 'candidate' if integrity == 'metadata_checked' else 'partial'}


def scan_fat32(reader, partition, token, *, max_entries=1000000, max_candidates=10000):
    """Yield deleted ordinary-file metadata; active files are never recovered."""
    if any(type(limit) is not int or limit < 1 for limit in (max_entries, max_candidates)):
        raise ValueError('FAT32 扫描上限无效。')
    volume = _Fat32(reader, partition, token)
    directories = deque([(volume.root, '', 0)])
    visited_directories, owned_clusters = set(), set()
    entries_seen = candidates_seen = 0
    while directories:
        first, parent, depth = directories.popleft()
        if first in visited_directories or depth > 128 or len(parent) > 4096:
            raise ValueError('FAT32 目录循环或层级超出上限，扫描未完成。')
        visited_directories.add(first)
        long_entries, lfn_overflow = [], False
        finished = False
        for cluster in volume.directory_clusters(first, owned_clusters):
            data = volume.read(volume.cluster_offset(cluster), volume.cluster_size)
            for offset in range(0, len(data), 32):
                token.checkpoint()
                entries_seen += 1
                if entries_seen > max_entries:
                    raise ValueError('FAT32 目录项达到上限，扫描未完成。')
                entry = data[offset:offset + 32]
                if entry[0] == 0:
                    finished = True
                    break
                if entry[11] == 0x0f:
                    if not lfn_overflow:
                        long_entries.append(entry)
                        if len(long_entries) > 20:
                            long_entries, lfn_overflow = [], True
                    continue
                deleted = entry[0] == 0xe5
                short = _short_name(entry, deleted)
                name = (_long_name(long_entries, entry, deleted) or short) if short else None
                long_entries, lfn_overflow = [], False
                attributes = entry[11]
                if attributes & 0xc8 or not name or name in ('.', '..'):
                    continue
                if attributes & 0x10:
                    if not deleted:
                        child = ((_u16(entry, 20) << 16) | _u16(entry, 26)) & 0x0fffffff
                        volume.require_cluster(child)
                        directories.append((child, parent + '/' + name, depth + 1))
                    continue
                if not deleted:
                    continue
                if candidates_seen >= max_candidates:
                    raise ValueError('FAT32 删除候选达到上限，扫描未完成。')
                candidates_seen += 1
                record_id = volume.base + volume.cluster_offset(cluster) + offset
                yield volume.candidate(entry, name, parent, record_id)
            if finished:
                break

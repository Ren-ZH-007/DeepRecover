"""Read-only APFS deleted-file candidates from snapshots and orphaned tree nodes.

Snapshots: a file that a snapshot still holds but the current tree no longer
has is read from the snapshot's own file-system tree. Its blocks stay allocated
while the snapshot exists, so the bytes are those of the moment it was taken.

Orphaned nodes: APFS is copy-on-write, so changing metadata writes new B-tree
nodes and frees the old ones. A deleted file's directory record, inode and
extents often survive in such freed leaf nodes until their blocks are reused.

Only unencrypted, single-volume containers are supported.

A snapshot candidate supplies extents when its layout is complete and every
block is still allocated. An orphaned-node candidate supplies extents only when
every data block is free in the space manager (bitmap or free queue), is not
used by the current tree, is not claimed by another deleted file whose records
may be newer, and does not now hold an APFS object. Even then the content is
not proven intact: overwrites by files whose own records are gone cannot be
seen. See docs/APFS_FEASIBILITY.md and docs/APFS_DELETED_RECORDS.md.
"""
from array import array
from bisect import bisect_left, bisect_right
from itertools import accumulate
import struct

from core.file_times import unix_modified

_M = 0xFFFFFFFF
_ROOT, _LEAF, _FIXED = 1, 2, 4
_BTREE, _BTREE_NODE, _FSTREE = 2, 3, 0x0E
_ROOT_DIR, _ROOT_PARENT = 2, 1
_DT_REG = 8
_S_IFMT, _S_IFREG = 0o170000, 0o100000
_UF_COMPRESSED = 0x20
_INODE_WAS_CLONED, _INODE_IS_SPARSE = 0x10, 0x200
_MAX_FILE = 8 * 1024 ** 3
_CHUNK = 4 * 1024 * 1024


def fletcher64(block):
    """APFS object checksum over everything after the stored checksum."""
    words = struct.unpack_from('<%dI' % (len(block) // 4 - 2), block, 8)
    s1 = sum(words) % _M
    s2 = sum(accumulate(words)) % _M
    c1 = _M - ((s1 + s2) % _M)
    c2 = _M - ((s1 + c1) % _M)
    return (c2 << 32) | c1


def valid_object(block):
    return len(block) >= 64 and block[:8] != bytes(8) and struct.unpack_from('<Q', block)[0] == fletcher64(block)


def _u16(data, offset): return struct.unpack_from('<H', data, offset)[0]
def _u32(data, offset): return struct.unpack_from('<I', data, offset)[0]
def _u64(data, offset): return struct.unpack_from('<Q', data, offset)[0]


def node_entries(block, key_size=None, value_size=None):
    """(flags, level, [(key, value or None)]) of a B-tree node, bounds-checked."""
    flags, level, count = struct.unpack_from('<HHI', block, 32)
    toc_offset, toc_length = struct.unpack_from('<HH', block, 40)
    toc = 56 + toc_offset
    keys = toc + toc_length
    end = len(block) - (40 if flags & _ROOT else 0)
    fixed = flags & _FIXED
    if fixed and key_size is None:
        raise ValueError('APFS 固定长度节点缺少键长度。')
    if level > 16 or bool(flags & _LEAF) != (level == 0) or count * (4 if fixed else 8) > toc_length or keys > end:
        raise ValueError('APFS B 树节点格式无效。')
    result = []
    for index in range(count):
        if fixed:
            key_at, value_at = struct.unpack_from('<HH', block, toc + 4 * index)
            key_length, value_length = key_size, (value_size if level == 0 else 8)
        else:
            key_at, key_length, value_at, value_length = struct.unpack_from('<HHHH', block, toc + 8 * index)
        start = keys + key_at
        if start + key_length > end:
            raise ValueError('APFS B 树键越界。')
        value = None
        if value_at != 0xFFFF:
            begin = end - value_at
            if begin < keys or begin + value_length > end:
                raise ValueError('APFS B 树值越界。')
            value = block[begin:begin + value_length]
        result.append((block[start:start + key_length], value))
    return flags, level, result


class _Intervals:
    """Sorted, possibly overlapping block ranges with an attached payload."""

    def __init__(self, items):
        self.items = sorted(items)
        self.starts = [s for s, _e, _p in self.items]
        self.reach = list(accumulate((e for _s, e, _p in self.items), max))

    def overlapping(self, start, end):
        index = bisect_left(self.starts, end) - 1
        while index >= 0 and self.reach[index] > start:
            s, e, payload = self.items[index]
            if e > start:
                yield s, e, payload
            index -= 1


class _Container:
    def __init__(self, reader, partition, token, max_records):
        self.reader, self.token, self.budget = reader, token, max_records
        self.base, self.limit = partition.offset, partition.size
        if (type(self.base) is not int or type(self.limit) is not int or self.base < 0
                or self.limit < 4096 * 16 or self.base + self.limit > reader.size):
            raise ValueError('APFS 分区范围无效。')
        head = reader.read_at(self.base, 4096)
        if head[32:36] != b'NXSB':
            raise ValueError('不是 APFS 容器。')
        self.bs = _u32(head, 36)
        if self.bs not in (4096, 8192, 16384, 32768, 65536):
            raise ValueError('APFS 块大小不受支持。')
        self.blocks = _u64(head, 40)
        if self.blocks < 16 or self.blocks * self.bs > self.limit:
            raise ValueError('APFS 容器超出分区范围，或镜像已截断。')
        desc_blocks, data_blocks = _u32(head, 104), _u32(head, 108)
        if desc_blocks & 0x80000000 or data_blocks & 0x80000000:
            raise ValueError('APFS 检查点区不连续，暂不支持。')
        self.desc_base, self.desc_blocks = _u64(head, 112), desc_blocks
        self.data_base, self.data_blocks = _u64(head, 120), data_blocks
        for base, count in ((self.desc_base, desc_blocks), (self.data_base, data_blocks)):
            if not count or base + count > self.blocks:
                raise ValueError('APFS 检查点区超出容器。')
        found = []
        for index in range(desc_blocks):
            block = self.block(self.desc_base + index)
            if block[32:36] == b'NXSB' and _u32(block, 24) & 0xFFFF == 1 and valid_object(block):
                found.append((_u64(block, 16), index, block))
        if not found:
            raise ValueError('找不到有效的 APFS 容器超级块。')
        # Only the newest checkpoint describes current allocation; an older one would make
        # blocks allocated since then look free, so a damaged newest checkpoint is an error.
        self._load(*max(found, key=lambda item: item[0]))

    # -- raw access -------------------------------------------------------------
    def block(self, number, count=1):
        if type(number) is not int or number < 0 or number + count > self.blocks:
            raise ValueError('APFS 块地址越界。')
        return self.reader.read_at(self.base + number * self.bs, count * self.bs)

    def obj(self, number, kind, subtype=None):
        block = self.block(number)
        if not valid_object(block) or _u32(block, 24) & 0xFFFF != kind or (subtype is not None and _u32(block, 28) != subtype):
            raise ValueError('APFS 元数据对象校验失败。')
        return block

    def ephemeral(self, oid, *kinds):
        if oid not in self.mapping:
            raise ValueError('APFS 检查点缺少所需对象。')
        paddr, size = self.mapping[oid]
        if size % self.bs or not self.bs <= size <= 64 * self.bs or not self.data_base <= paddr < self.data_base + self.data_blocks:
            raise ValueError('APFS 检查点对象位置无效。')
        first = paddr - self.data_base
        data = b''.join(self.block(self.data_base + (first + i) % self.data_blocks) for i in range(size // self.bs))
        if not valid_object(data) or _u32(data, 24) & 0xFFFF not in kinds:
            raise ValueError('APFS 检查点对象校验失败。')
        return data

    # -- checkpoint -------------------------------------------------------------
    def _load(self, xid, index, sb):
        if _u32(sb, 36) != self.bs or _u64(sb, 40) != self.blocks:
            raise ValueError('APFS 容器超级块之间不一致。')
        if _u64(sb, 64) & ~0x3:
            raise ValueError('此 APFS 容器使用了 Fusion Drive 或其他暂不支持的特性。')
        self.xid, self.next_xid = xid, _u64(sb, 96)
        start, length = _u32(sb, 136), _u32(sb, 140)
        if not 1 <= length <= self.desc_blocks or start >= self.desc_blocks or (start + length - 1) % self.desc_blocks != index:
            raise ValueError('APFS 检查点描述无效。')
        self.mapping = {}
        for i in range(length - 1):
            block = self.block(self.desc_base + (start + i) % self.desc_blocks)
            if not valid_object(block) or _u32(block, 24) & 0xFFFF != 0x0C or _u64(block, 16) != xid:
                raise ValueError('APFS 检查点映射校验失败。')
            count = _u32(block, 36)
            if 40 + 40 * count > self.bs:
                raise ValueError('APFS 检查点映射格式无效。')
            for j in range(count):
                _t, _s, size, _pad, _fs, oid, paddr = struct.unpack_from('<IIIIQQQ', block, 40 + 40 * j)
                self.mapping[oid] = (paddr, size)
        volumes = [oid for oid in struct.unpack_from('<100Q', sb, 184) if oid]
        if not volumes:
            raise ValueError('APFS 容器中没有卷。')
        if len(volumes) > 1:
            raise ValueError('此 APFS 容器有多个卷，暂不支持删除记录解析；可选择签名候选扫描。')
        self.omap_cache = {}
        container_omap = self._omap_tree(_u64(sb, 160))
        apsb_at = self.omap_lookup(container_omap, volumes[0], xid)
        volume = self.obj(apsb_at, 0x0D)
        if volume[32:36] != b'APSB':
            raise ValueError('APFS 卷超级块无效。')
        if not _u64(volume, 264) & 1:
            raise ValueError('此 APFS 卷已加密，无法解析删除记录。')
        incompatible = _u64(volume, 56)
        if incompatible & ~0x0B:
            raise ValueError('此 APFS 卷使用了暂不支持的特性（如系统封存卷或加密变更）。')
        self.snapshot_count, self.snap_tree = _u64(volume, 216), _u64(volume, 152)
        if self.snapshot_count and _u32(volume, 124) != 0x40000002:
            raise ValueError('此 APFS 卷的快照元数据类型暂不支持。')
        if _u32(volume, 116) != _BTREE:
            raise ValueError('此 APFS 卷的文件树类型暂不支持。')
        self.hashed_names = bool(incompatible & 0x09)
        self.volume_omap = self._omap_tree(_u64(volume, 128))
        self.root_tree = _u64(volume, 136)
        self._load_spaceman(_u64(sb, 152))

    def _omap_tree(self, paddr):
        omap = self.obj(paddr, 0x0B)
        if _u32(omap, 32) & 0x1E:
            raise ValueError('此 APFS 卷正在加密或解密，无法解析删除记录。')
        return _u64(omap, 48)

    def _omap_node(self, paddr):
        node = self.omap_cache.get(paddr)
        if node is None:
            block = self.block(paddr)
            if not valid_object(block) or _u32(block, 24) & 0xFFFF not in (_BTREE, _BTREE_NODE) or _u32(block, 28) != 0x0B:
                raise ValueError('APFS 对象映射节点校验失败。')
            flags, level, entries = node_entries(block, 16, 16)
            if not flags & _FIXED:
                raise ValueError('APFS 对象映射节点格式无效。')
            keys = [struct.unpack('<QQ', k) for k, _v in entries]
            if keys != sorted(keys):
                raise ValueError('APFS 对象映射节点顺序无效。')
            node = (level, keys, [v for _k, v in entries])
            if len(self.omap_cache) > 4096:
                self.omap_cache.clear()
            self.omap_cache[paddr] = node
        return node

    def omap_lookup(self, tree, oid, xid):
        paddr, depth = tree, 0
        while True:
            level, keys, values = self._omap_node(paddr)
            index = bisect_right(keys, (oid, xid)) - 1
            if index < 0 or values[index] is None:
                raise ValueError('APFS 对象映射缺少所需对象。')
            if level == 0:
                flags, _size, target = struct.unpack('<IIQ', values[index])
                if keys[index][0] != oid or flags & 1:
                    raise ValueError('APFS 对象映射缺少所需对象。')
                if flags & 4:
                    raise ValueError('此 APFS 卷的元数据已加密。')
                return target
            depth += 1
            if depth > 16:
                raise ValueError('APFS 对象映射层级无效。')
            paddr = _u64(values[index], 0)

    # -- space manager ----------------------------------------------------------
    def _load_spaceman(self, oid):
        sm = self.ephemeral(oid, 0x05)
        self.per_chunk, self.chunks_per_cib, self.cibs_per_cab = struct.unpack_from('<III', sm, 36)
        block_count, chunk_count, cib_count, cab_count, free_count, addr_offset = struct.unpack_from('<QQIIQI', sm, 48)
        if (_u32(sm, 32) != self.bs or block_count != self.blocks or _u64(sm, 96) or self.per_chunk != self.bs * 8
                or not self.chunks_per_cib or chunk_count != -(-block_count // self.per_chunk)
                or cib_count != -(-chunk_count // self.chunks_per_cib) or free_count > block_count):
            raise ValueError('APFS 空间管理器格式无效。')
        if cab_count and (not self.cibs_per_cab or cab_count != -(-cib_count // self.cibs_per_cab)):
            raise ValueError('APFS 空间管理器格式无效。')
        addresses = cab_count or cib_count
        if addr_offset + 8 * addresses > len(sm):
            raise ValueError('APFS 空间管理器格式无效。')
        self.addresses, self.cabs = struct.unpack_from('<%dQ' % addresses, sm, addr_offset), bool(cab_count)
        self.cib_cache, self.bitmap_cache = {}, {}
        queued = []
        for q in range(3):
            count, tree = struct.unpack_from('<QQ', sm, 200 + 40 * q)
            if tree:
                queued.extend(self._free_queue(tree))
        self.queued = _Intervals((s, s + n, None) for s, n in queued)

    def _free_queue(self, tree):
        stack, seen, out = [tree], set(), []
        while stack:
            oid = stack.pop()
            if oid in seen or len(seen) > 100000:
                raise ValueError('APFS 空闲队列结构无效。')
            seen.add(oid)
            if oid in self.mapping:
                node = self.ephemeral(oid, _BTREE, _BTREE_NODE)
            else:
                node = self.block(oid)
                if not valid_object(node) or _u32(node, 24) & 0xFFFF not in (_BTREE, _BTREE_NODE):
                    raise ValueError('APFS 空闲队列结构无效。')
            if _u32(node, 28) != 0x09:
                raise ValueError('APFS 空闲队列结构无效。')
            flags, level, entries = node_entries(node, 16, 8)
            if not flags & _FIXED:
                raise ValueError('APFS 空闲队列结构无效。')
            for key, value in entries:
                if level:
                    stack.append(_u64(value, 0))
                else:
                    _xid, paddr = struct.unpack('<QQ', key)
                    count = _u64(value, 0) if value else 1
                    if not count or paddr + count > self.blocks:
                        raise ValueError('APFS 空闲队列记录越界。')
                    out.append((paddr, count))
        return out

    def _chunk(self, index):
        cib_index, entry = divmod(index, self.chunks_per_cib)
        cib = self.cib_cache.get(cib_index)
        if cib is None:
            if self.cabs:
                cab = self.obj(self.addresses[cib_index // self.cibs_per_cab], 0x06)
                slot = cib_index % self.cibs_per_cab
                if _u32(cab, 32) != cib_index // self.cibs_per_cab or slot >= _u32(cab, 36):
                    raise ValueError('APFS 空间管理器结构无效。')
                address = _u64(cab, 40 + 8 * slot)
            else:
                address = self.addresses[cib_index]
            block = self.obj(address, 0x07)
            count = _u32(block, 36)
            if _u32(block, 32) != cib_index or 40 + 32 * count > self.bs:
                raise ValueError('APFS 空间管理器结构无效。')
            cib = [struct.unpack_from('<QQIIQ', block, 40 + 32 * j) for j in range(count)]
            self.cib_cache[cib_index] = cib
        if entry >= len(cib):
            raise ValueError('APFS 空间管理器结构无效。')
        _xid, address, count, free, bitmap = cib[entry]
        if address != index * self.per_chunk or count > self.per_chunk or free > count:
            raise ValueError('APFS 空间管理器结构无效。')
        if free == count:
            return count, None
        cached = self.bitmap_cache.get(index)
        if cached is None:
            if not bitmap:
                raise ValueError('APFS 空间管理器缺少位图。')
            bits = int.from_bytes(self.block(bitmap), 'little') & ((1 << count) - 1)
            if count - bits.bit_count() != free:
                raise ValueError('APFS 空间管理器位图与计数不一致。')
            if len(self.bitmap_cache) > 256:
                self.bitmap_cache.clear()
            self.bitmap_cache[index] = cached = bits
        return count, cached

    def all_held(self, start, end):
        """True when every block in [start, end) is allocated and none waits in a free queue."""
        if next(self.queued.overlapping(start, end), None) is not None:
            return False
        a = start
        while a < end:
            index = a // self.per_chunk
            count, bits = self._chunk(index)
            first = index * self.per_chunk
            stop = min(end, first + self.per_chunk)
            if stop - first > count:
                raise ValueError('APFS 空间管理器结构无效。')
            mask = (1 << (stop - a)) - 1
            if bits is None or (bits >> (a - first)) & mask != mask:
                return False
            a = stop
        return True

    def any_used(self, start, end):
        """True when a block in [start, end) is allocated and not waiting in a free queue."""
        pieces = [(start, end)]
        for s, e, _p in sorted(self.queued.overlapping(start, end)):
            rest = []
            for a, b in pieces:
                if e <= a or s >= b:
                    rest.append((a, b)); continue
                if a < s: rest.append((a, s))
                if e < b: rest.append((e, b))
            pieces = rest
        for a, b in pieces:
            while a < b:
                index = a // self.per_chunk
                count, bits = self._chunk(index)
                first = index * self.per_chunk
                stop = min(b, first + self.per_chunk)
                if stop - first > count:
                    raise ValueError('APFS 空间管理器结构无效。')
                if bits is not None and bits >> (a - first) & ((1 << (stop - a)) - 1):
                    return True
                a = stop
        return False


def entry_name(key, hashed):
    """Directory-record name; hashed keys carry a 22-bit name hash beside the length."""
    if hashed:
        if len(key) < 12: return None
        length = _u32(key, 8) & 0x3FF; raw = key[12:12 + length]
    else:
        if len(key) < 10: return None
        length = _u16(key, 8); raw = key[10:10 + length]
    if not length or len(raw) != length or raw[-1:] != b'\0':
        return None
    try:
        name = raw[:-1].decode('utf-8')
    except UnicodeDecodeError:
        return None
    return name if name and '/' not in name and '\0' not in name else None


class _Records:
    """FS-tree records from orphaned leaf nodes; the newest node version wins per key."""

    def __init__(self, hashed_names):
        self.hashed = hashed_names
        self.drec, self.inode, self.extent, self.versions = {}, {}, {}, {}
        self.count = 0

    def add(self, xid, key, value):
        """Return False for records this module does not use."""
        if len(key) < 8 or value is None:
            return False
        header = _u64(key, 0)
        oid, kind = header & ((1 << 60) - 1), header >> 60
        if kind == 9 and len(value) >= 18:
            name = entry_name(key, self.hashed)
            if name is None:
                return False
            fid, _added, flags = struct.unpack_from('<QQH', value)
            self._newest(self.drec, (oid, name), xid, (fid, flags & 0xF))
        elif kind == 3 and len(value) >= 92:
            self._newest(self.inode, oid, xid, inode_fields(value))
        elif kind == 8 and len(key) >= 16 and len(value) >= 24:
            logical = _u64(key, 8)
            length, physical = _u64(value, 0) & ((1 << 56) - 1), _u64(value, 8)
            self._newest(self.extent, (oid, logical), xid, (length, physical))
            span = self.versions.get((oid, logical, length, physical))
            self.versions[(oid, logical, length, physical)] = (xid, xid) if span is None else (min(span[0], xid), max(span[1], xid))
        else:
            return False
        self.count += 1
        return True

    @staticmethod
    def _newest(table, key, xid, value):
        old = table.get(key)
        if old is None or old[0] <= xid:
            table[key] = (xid, value)


def inode_fields(value):
    parent, private = struct.unpack_from('<QQ', value)
    fields = {'parent': parent, 'private': private, 'modified': _u64(value, 24), 'internal': _u64(value, 48),
              'bsd': _u32(value, 68), 'mode': _u16(value, 80), 'size': None}
    if len(value) < 96:
        return fields
    count, used = struct.unpack_from('<HH', value, 92)
    data = 96 + 4 * count
    if data + used <= len(value):
        for index in range(count):
            kind, _flags, size = struct.unpack_from('<BBH', value, 96 + 4 * index)
            if kind == 8 and size >= 16 and data + 16 <= len(value):
                fields['size'], fields['allocated'] = struct.unpack_from('<QQ', value, data)
            data += (size + 7) & ~7
    return fields


def _walk_tree(volume, root, xid, visited, what):
    """Leaf (key, value) pairs of a file-system tree as of ``xid``.

    Nodes already in ``visited`` (shared with a tree walked earlier) are skipped;
    every node reached is added to it.
    """
    stack, seen = [(root, None)], set()
    while stack:
        volume.token.checkpoint()
        oid, expected = stack.pop()
        paddr = volume.omap_lookup(volume.volume_omap, oid, xid)
        if paddr in seen:
            raise ValueError(f'APFS {what}结构无效。')
        seen.add(paddr)
        if paddr in visited:
            continue
        visited.add(paddr)
        block = volume.block(paddr)
        if (not valid_object(block) or _u32(block, 24) & 0xFFFF not in (_BTREE, _BTREE_NODE)
                or _u32(block, 28) != _FSTREE or _u32(block, 24) >> 16):
            raise ValueError(f'APFS {what}不完整，不能判断哪些文件仍然存在。')
        _flags, level, entries = node_entries(block)
        if expected is not None and level != expected:
            raise ValueError(f'APFS {what}结构无效。')
        for key, value in entries:
            if len(key) < 8 or value is None or (level and len(value) < 8):
                raise ValueError(f'APFS {what}结构无效。')
            if level:
                stack.append((_u64(value, 0), level - 1)); continue
            volume.budget -= 1
            if volume.budget < 0:
                raise ValueError('APFS 记录数量达到上限，扫描未完成。')
            yield key, value


def _current_tree(volume, visited):
    """Live file ids, live directory names and live extents."""
    live, names, extents = set(), {}, []
    for key, value in _walk_tree(volume, volume.root_tree, volume.xid, visited, '当前文件树'):
        header = _u64(key, 0)
        oid, kind = header & ((1 << 60) - 1), header >> 60
        if kind == 3:
            live.add(oid)
        elif kind == 9 and len(value) >= 18:
            fid = _u64(value, 0); live.add(fid)
            name = entry_name(key, volume.hashed_names)
            if name is not None:
                names[fid] = (oid, name)
        elif kind == 8 and len(key) >= 16 and len(value) >= 16:
            length, physical = _u64(value, 0) & ((1 << 56) - 1), _u64(value, 8)
            if physical and length:
                extents.append((physical, physical + -(-length // volume.bs), oid))
    return live, names, _Intervals(extents)


def _snapshots(volume):
    """Snapshot metadata records, newest first: dicts with xid, name, created, superblock, flags."""
    if not volume.snapshot_count:
        return []
    stack, seen, found = [volume.snap_tree], set(), []
    while stack:
        paddr = stack.pop()
        if paddr in seen or len(seen) > 100000:
            raise ValueError('APFS 快照元数据结构无效。')
        seen.add(paddr)
        block = volume.block(paddr)
        if not valid_object(block) or _u32(block, 24) & 0xFFFF not in (_BTREE, _BTREE_NODE) or _u32(block, 28) != 0x10:
            raise ValueError('APFS 快照元数据无法读取。')
        _flags, level, entries = node_entries(block)
        for key, value in entries:
            if value is None or len(key) < 8:
                raise ValueError('APFS 快照元数据结构无效。')
            if level:
                if len(value) < 8:
                    raise ValueError('APFS 快照元数据结构无效。')
                stack.append(_u64(value, 0)); continue
            header = _u64(key, 0)
            if header >> 60 != 1:                       # name records repeat the metadata records
                continue
            if len(value) < 51:
                raise ValueError('APFS 快照元数据结构无效。')
            _extentref, superblock, created, _changed, _inode, _type, flags, length = struct.unpack_from('<QQQQQIIH', value)
            raw = value[50:50 + length]
            if not length or len(raw) != length or raw[-1:] != b'\0':
                raise ValueError('APFS 快照名称无效。')
            xid = header & ((1 << 60) - 1)
            if not 0 < xid <= volume.xid:
                raise ValueError('APFS 快照事务号无效。')
            found.append({'xid': xid, 'name': raw[:-1].decode('utf-8', 'replace'), 'created': created,
                          'superblock': superblock, 'flags': flags})
    if len(found) != volume.snapshot_count:
        raise ValueError('APFS 快照元数据与卷记录的快照数量不一致。')
    return sorted(found, key=lambda item: item['xid'], reverse=True)


def _snapshot_files(volume, snapshots, visited, live, live_names):
    """Candidates for files that a snapshot still holds but the current tree no longer has.

    Snapshots are walked newest first, so each file is reported once, from the
    newest snapshot that contains it. Returns (candidates, file ids seen, names).
    """
    claimed, seen, names, results = set(), set(), dict(live_names), []
    for snapshot in snapshots:
        superblock = volume.obj(snapshot['superblock'], 0x0D)
        if superblock[32:36] != b'APSB':
            raise ValueError('APFS 快照卷超级块无效。')
        records = _Records(volume.hashed_names)
        for key, value in _walk_tree(volume, _u64(superblock, 136), snapshot['xid'], visited, '快照文件树'):
            records.add(snapshot['xid'], key, value)
        local = {}
        for (parent, name), (_x, (fid, kind)) in records.drec.items():
            local[fid] = (parent, name, kind)
        for fid, (parent, name, _kind) in local.items():
            names.setdefault(fid, (parent, name))
        seen.update(records.inode)
        seen.update(fid for fid, _ in local.items())
        if snapshot['flags']:
            continue                                     # dataless or merging: its data is not held
        label = '[快照 ' + snapshot['name'].replace('/', '_').replace('\\', '_') + ']'
        details = {'check_version': 1, 'snapshot_name': snapshot['name'], 'snapshot_xid': snapshot['xid'],
                   'snapshot_created_utc': unix_modified(snapshot['created'] // 1_000_000_000).get('modified_at')}
        for fid, (_x, fields) in records.inode.items():
            if fid in live or fid in claimed or fields['mode'] & _S_IFMT != _S_IFREG:
                continue
            volume.token.checkpoint()
            claimed.add(fid)
            parent, name, _kind = local.get(fid, (fields['parent'], f'APFS_{fid}.bin', _DT_REG))
            integrity, extents = _check_snapshot(volume, fields, records)
            results.append({'name': name, 'original_path': label + _path(parent, names) + '/' + name,
                            'size': fields['size'] or 0, 'extents': extents if integrity == 'snapshot_checked' else [],
                            'record_id': fid, 'integrity': integrity,
                            'status': 'candidate' if integrity == 'snapshot_checked' else 'partial',
                            'file_system': 'APFS', 'deletion_state': 'snapshot_record', 'structure_details': details,
                            **unix_modified(fields['modified'] // 1_000_000_000)})
    return results, seen, names


def _path(fid, names, depth=0):
    """Path of a directory from (parent, name) pairs; unknown parents are marked, never guessed."""
    if fid in (_ROOT_DIR, _ROOT_PARENT):
        return ''
    if depth > 256:
        return '[APFS 目录层级过深]'
    if fid not in names:
        return f'[APFS 目录 {fid}]'
    parent, name = names[fid]
    return _path(parent, names, depth + 1) + '/' + name


def _carve(volume, current_nodes, records):
    """Scan every container block once: orphaned FS-tree leaves and anything that looks like an object."""
    objects, truncated = array('Q'), False
    limit = volume.next_xid + (1 << 16)
    total = volume.blocks * volume.bs
    position, step = 0, max(volume.bs, _CHUNK // volume.bs * volume.bs)
    while position < total:
        volume.token.checkpoint()
        data = volume.reader.read_at(volume.base + position, min(step, total - position))
        first = position // volume.bs
        for at in range(0, len(data), volume.bs):
            oid, xid, kind, subtype = struct.unpack_from('<QQII', data, at + 8)
            if not oid or not 0 < xid <= limit or kind & 0x07FF0000 or not 0 < kind & 0xFFFF <= 0x20:
                continue
            number = first + at // volume.bs
            objects.append(number)
            if (kind in (_BTREE, _BTREE_NODE) and subtype == _FSTREE and xid <= volume.xid and not truncated
                    and number not in current_nodes and _u16(data, at + 34) == 0):
                block = data[at:at + volume.bs]
                if not valid_object(block):
                    continue
                try:
                    _flags, _level, entries = node_entries(block)
                except ValueError:
                    continue
                for key, value in entries:
                    records.add(xid, key, value)
                if records.count > volume.budget:
                    truncated = True
        position += len(data)
        volume.token.report_progress(min(position, total - 1), total, 0)
    return objects, truncated


def scan_apfs(reader, partition, token, *, max_records=1000000, max_candidates=10000):
    """Yield deleted APFS files from snapshots and old tree nodes; never mounts or modifies the container."""
    if type(max_records) is not int or type(max_candidates) is not int or min(max_records, max_candidates) < 1:
        raise ValueError('APFS 扫描上限无效。')
    try:
        yield from _scan(reader, partition, token, max_records, max_candidates)
    except struct.error as error:              # a checksum-valid object whose fields are still inconsistent
        raise ValueError('APFS 元数据格式无效。') from error


def _scan(reader, partition, token, max_records, max_candidates):
    volume = _Container(reader, partition, token, max_records)
    visited = set()                                  # nodes of the current tree and of every snapshot tree
    live, live_names, live_extents = _current_tree(volume, visited)
    held, in_snapshots, known_names = _snapshot_files(volume, _snapshots(volume), visited, live, live_names)
    records = _Records(volume.hashed_names)
    volume.budget = max(volume.budget, 0)
    objects, truncated = _carve(volume, visited, records)
    checked_objects = {}

    def holds_object(start, end):
        for number in objects[bisect_left(objects, start):bisect_left(objects, end)]:
            if number not in checked_objects:
                checked_objects[number] = valid_object(volume.block(number))
            if checked_objects[number]:
                return True
        return False

    others = _Intervals((physical, physical + -(-length // volume.bs), (owner, span[1]))
                        for (owner, _logical, length, physical), span in records.versions.items() if physical and length)
    names = {}
    for (parent, name), (xid, (fid, kind)) in records.drec.items():
        if fid not in names or names[fid][0] <= xid:
            names[fid] = (xid, parent, name, kind)
    directories = dict(known_names)                  # current tree, then snapshots, then old nodes
    for fid, (_x, parent, name, _kind) in names.items():
        directories.setdefault(fid, (parent, name))

    deleted = {fid for fid, (_x, _p, _n, kind) in names.items() if kind == _DT_REG}
    deleted |= {oid for oid, (_x, fields) in records.inode.items() if fields['mode'] & _S_IFMT == _S_IFREG}
    deleted -= live | in_snapshots                   # files a snapshot holds are reported from the snapshot
    results = list(held)
    for fid in deleted:
        token.checkpoint()
        named = names.get(fid)
        inode = records.inode.get(fid)
        if inode is not None and inode[1]['mode'] & _S_IFMT != _S_IFREG:
            continue
        if named is not None:
            _x, parent, name, _k = named
        else:
            parent, name = inode[1]['parent'], f'APFS_{fid}.bin'
        candidate = {'name': name, 'original_path': _path(parent, directories) + '/' + name, 'size': 0, 'extents': [],
                     'record_id': fid, 'integrity': 'invalid_metadata', 'status': 'partial',
                     'file_system': 'APFS', 'deletion_state': 'deleted_record'}
        if inode is not None:
            fields = inode[1]
            candidate.update(unix_modified(fields['modified'] // 1_000_000_000))
            candidate['size'] = fields['size'] or 0
            integrity, extents = _check(volume, fields, records, live_extents, others, holds_object)
            if truncated and integrity == 'metadata_checked':
                integrity = 'validation_limit'      # the overwrite check needs every old record
            candidate['integrity'] = integrity
            if integrity == 'metadata_checked':
                candidate.update(status='candidate', extents=extents)
        results.append(candidate)
    results.sort(key=lambda item: (item['original_path'], item['record_id']))
    for count, candidate in enumerate(results):
        if count >= max_candidates:
            raise ValueError('APFS 删除候选达到上限，扫描未完成。')
        token.report_progress(volume.blocks * volume.bs - 1, volume.blocks * volume.bs, count + 1)
        yield candidate
    if truncated:
        raise ValueError('APFS 删除记录数量达到上限，扫描仅部分完成。')


def _layout(volume, fields, records):
    """(problem or None, extents in logical order) covering the file size from the newest records."""
    size = fields['size'] or 0
    if size > _MAX_FILE:
        return 'size_limit', []
    if fields['bsd'] & _UF_COMPRESSED or fields['internal'] & _INODE_IS_SPARSE:
        return 'unsupported_layout', []
    owner = fields['private']
    pieces = sorted((logical, length, physical) for (o, logical), (_x, (length, physical)) in records.extent.items() if o == owner)
    covered, used = 0, []
    for logical, length, physical in pieces:
        if covered >= size:
            break
        if logical != covered or not length or length % volume.bs:
            return 'invalid_metadata', []
        if not physical:
            return 'unsupported_layout', []
        if physical + length // volume.bs > volume.blocks:
            return 'invalid_extent', []
        used.append((logical, length, physical)); covered += length
    if covered < size:
        return 'invalid_metadata', []
    return None, used


def _byte_ranges(volume, used, size):
    extents, remaining = [], size
    for _logical, length, physical in used:
        amount = min(length, remaining)
        offset = volume.base + physical * volume.bs
        if extents and extents[-1][0] + extents[-1][1] == offset:
            extents[-1] = (extents[-1][0], extents[-1][1] + amount)
        else:
            extents.append((offset, amount))
        remaining -= amount
    return extents


def _check_snapshot(volume, fields, records):
    """A snapshot keeps its blocks allocated; anything else means the snapshot is not intact."""
    problem, used = _layout(volume, fields, records)
    if problem:
        return problem, []
    try:
        if not all(volume.all_held(physical, physical + length // volume.bs) for _l, length, physical in used):
            return 'allocation_unknown', []
    except ValueError:
        return 'allocation_unknown', []
    return 'snapshot_checked', _byte_ranges(volume, used, fields['size'] or 0)


def _check(volume, fields, records, live_extents, others, holds_object):
    """(integrity, extents) for one deleted inode; extents only when every check passes."""
    problem, used = _layout(volume, fields, records)
    if problem:
        return problem, []
    size, owner = fields['size'] or 0, fields['private']
    if not size:
        return 'metadata_checked', []
    ranges = [(physical, physical + length // volume.bs) for _l, length, physical in used]
    live = sum(min(e, end) - max(s, start) for start, end in ranges for s, e, _o in live_extents.overlapping(start, end))
    blocks = sum(end - start for start, end in ranges)
    if live:
        return ('shared_with_live' if live >= blocks and fields['internal'] & _INODE_WAS_CLONED else 'allocated_clusters'), []
    try:
        if any(volume.any_used(start, end) for start, end in ranges):
            return 'allocated_clusters', []
    except ValueError:
        return 'allocation_unknown', []
    earliest = min(records.versions[(owner, logical, length, physical)][0] for logical, length, physical in used)
    for start, end in ranges:
        for _s, _e, (other, newest) in others.overlapping(start, end):
            if other != owner and newest >= earliest:
                return 'overwritten_after_delete', []
    if any(holds_object(start, end) for start, end in ranges):
        return 'overwritten_after_delete', []
    return 'metadata_checked', _byte_ranges(volume, used, size)

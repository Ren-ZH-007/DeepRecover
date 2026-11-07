"""Small generated APFS containers; no user disk data or external fixtures.

Layout (4 KiB blocks): 0 superblock copy, 1-4 checkpoint descriptors, 5-8
checkpoint data (space manager, free queue), 9-10 container object map,
11 volume superblock, 12-13 volume object map, 14 current file-system tree,
15 chunk info, 16 allocation bitmap, 17 snapshot metadata tree; files, old tree
nodes, snapshot trees and snapshot superblocks from block 20.
"""
from dataclasses import dataclass
import struct

from core.computer.apfs import fletcher64
from tests.exfat_fixture import MemoryReader

BS = 4096
VOLUME_OID, ROOT_TREE_OID, SPACEMAN_OID, FREE_QUEUE_OID = 1026, 1028, 0x400, 0x401
FIRST_FREE = 20


@dataclass
class Partition:
    offset: int
    size: int
    file_system: str = 'APFS'


def seal(block):
    block = bytearray(block)
    struct.pack_into('<Q', block, 0, fletcher64(bytes(block)))
    return bytes(block)


def header(oid, xid, kind, subtype=0):
    return bytearray(struct.pack('<QQQII', 0, oid, xid, kind, subtype)) + bytearray(BS - 32)


def node(oid, xid, kind, subtype, entries, *, level=0, root=True, fixed=None):
    """B-tree node; ``fixed`` is (key size, value size) for fixed-size trees."""
    block = header(oid, xid, kind, subtype)
    flags = (1 if root else 0) | (2 if level == 0 else 0) | (4 if fixed else 0)
    toc_entry = 4 if fixed else 8
    toc_length = toc_entry * len(entries)
    struct.pack_into('<HHI', block, 32, flags, level, len(entries))
    struct.pack_into('<HH', block, 40, 0, toc_length)
    keys, end = 56 + toc_length, BS - (40 if root else 0)
    key_at, value_at = 0, 0
    for index, (key, value) in enumerate(entries):
        block[keys + key_at:keys + key_at + len(key)] = key
        value_at += len(value)
        block[end - value_at:end - value_at + len(value)] = value
        if fixed:
            struct.pack_into('<HH', block, 56 + 4 * index, key_at, value_at)
        else:
            struct.pack_into('<HHHH', block, 56 + 8 * index, key_at, len(key), value_at, len(value))
        key_at += len(key)
    assert keys + key_at <= end - value_at, 'fixture node overflow'
    return seal(block)


def name_key(parent, name, hashed=True):
    raw = name.encode('utf-8') + b'\0'
    prefix = struct.pack('<Q', parent | 9 << 60)
    return prefix + (struct.pack('<I', len(raw) | 0x12345 << 10) if hashed else struct.pack('<H', len(raw))) + raw


def drec(parent, name, fid, kind=8, hashed=True):
    return name_key(parent, name, hashed), struct.pack('<QQH', fid, 0, kind)


def inode(fid, *, parent=2, size=None, private=None, mode=0o100644, internal=0, bsd=0,
          modified=1_700_000_000 * 10 ** 9):
    value = struct.pack('<QQQQQQQIIIIIIHHQ', parent, fid if private is None else private, modified, modified, modified,
                        modified, internal, 1, 0, 0, bsd, 501, 20, mode, 0, 0)
    if size is None:
        value += struct.pack('<HH', 0, 0)
    else:
        value += struct.pack('<HHBBH', 1, 40, 8, 0x20, 40) + struct.pack('<QQQQQ', size, -(-size // BS) * BS, 0, size, 0)
    return struct.pack('<Q', fid | 3 << 60), value


def extent(owner, logical, length, physical):
    return struct.pack('<QQ', owner | 8 << 60, logical), struct.pack('<QQQ', length, physical, 0)


class ApfsImage:
    def __init__(self, blocks=96, *, xid=10, hashed=True, offset=0):
        self.blocks, self.xid, self.hashed, self.offset = blocks, xid, hashed, offset
        self.data = bytearray(blocks * BS)
        self.used = set(range(FIRST_FREE))
        self.queued = []                 # (xid, first block, count) waiting in the free queue
        self.live = []                   # records of the current tree
        self.orphans = {}                # block -> (xid, records, root)
        self.next_block, self.next_fid = FIRST_FREE, 100
        self.encrypted, self.volumes = False, 1
        self.snaps, self.snapshot_count = [], None   # count overrides len(snaps) to fake a mismatch

    # -- allocation ------------------------------------------------------------
    def allocate(self, count):
        start = self.next_block
        self.next_block += count
        assert self.next_block <= self.blocks, 'fixture is full'
        return start

    def write(self, block, payload):
        self.data[block * BS:block * BS + len(payload)] = payload

    def fid(self):
        self.next_fid += 1
        return self.next_fid

    def place(self, payload, start=None):
        count = -(-len(payload) // BS)
        start = self.allocate(count) if start is None else start
        self.write(start, payload)
        return start, count

    # -- files -----------------------------------------------------------------
    def live_file(self, name, payload, *, parent=2, internal=0, start=None):
        fid = self.fid()
        start, count = self.place(payload, start)
        self.used.update(range(start, start + count))
        self.live += [inode(fid, parent=parent, size=len(payload), internal=internal), drec(parent, name, fid, hashed=self.hashed)]
        if count:
            self.live.append(extent(fid, 0, count * BS, start))
        return fid, start

    def live_directory(self, name, parent=2):
        fid = self.fid()
        self.live += [inode(fid, parent=parent, mode=0o40755), drec(parent, name, fid, 4, self.hashed)]
        return fid

    def deleted_records(self, name, payload, *, parent=2, fid=None, start=None, pieces=None, with_name=True,
                        with_inode=True, size=None, internal=0, bsd=0, private=None):
        """Records of a deleted file whose data is written but left free; returns (fid, records, first block)."""
        fid = self.fid() if fid is None else fid
        owner = fid if private is None else private
        records = []
        if with_inode:
            records.append(inode(fid, parent=parent, size=len(payload) if size is None else size,
                                 internal=internal, bsd=bsd, private=private))
        if with_name:
            records.append(drec(parent, name, fid, hashed=self.hashed))
        if pieces is None:
            start, count = self.place(payload, start)
            if count:
                records.append(extent(owner, 0, count * BS, start))
        else:                            # [(physical block, block count)] in logical order
            logical = 0
            for physical, count in pieces:
                self.write(physical, payload[logical:logical + count * BS])
                records.append(extent(owner, logical, count * BS, physical))
                logical += count * BS
            start = pieces[0][0]
        return fid, records, start

    def orphan(self, xid, records, *, root=True, block=None):
        block = self.allocate(1) if block is None else block
        self.orphans[block] = (xid, records, root)
        return block

    def deleted_file(self, name, payload, *, xid=5, **options):
        fid, records, start = self.deleted_records(name, payload, **options)
        self.orphan(xid, records)
        return fid, start

    def snapshot(self, name, xid, *, with_live=True, flags=0, share=None):
        """A snapshot taken at ``xid``: the root directory, the live files (optional) and its own files.

        ``share`` reuses another snapshot's tree node, as when nothing changed between them.
        """
        assert 0 < xid < self.xid
        spec = {'name': name, 'xid': xid, 'records': [], 'flags': flags, 'with_live': with_live,
                'node': share['node'] if share else self.allocate(1), 'superblock': self.allocate(1), 'share': share}
        self.used.update((spec['node'], spec['superblock']))
        self.snaps.append(spec)
        return spec

    def snapshot_file(self, snap, name, payload, *, parent=2, fid=None, held=True, bsd=0, mode=0o100644):
        """A file the snapshot holds; ``held`` keeps its blocks allocated, as a real snapshot does."""
        fid = self.fid() if fid is None else fid
        start, count = self.place(payload)
        if held:
            self.used.update(range(start, start + count))
        snap['records'] += [inode(fid, parent=parent, size=len(payload), bsd=bsd, mode=mode), drec(parent, name, fid, hashed=self.hashed)]
        if count:
            snap['records'].append(extent(fid, 0, count * BS, start))
        return fid, start

    def snapshot_directory(self, snap, name, parent=2):
        fid = self.fid()
        snap['records'] += [inode(fid, parent=parent, mode=0o40755), drec(parent, name, fid, 4, self.hashed)]
        return fid

    def queue(self, start, count, xid=9):
        """Blocks freed by a recent transaction: still set in the bitmap, listed in the free queue."""
        self.used.update(range(start, start + count))
        self.queued.append((xid, start, count))

    def occupy(self, start, count=1):
        self.used.update(range(start, start + count))

    # -- build -----------------------------------------------------------------
    def fs_records(self, records):
        return sorted(records, key=lambda kv: (struct.unpack_from('<Q', kv[0])[0] & ((1 << 60) - 1),
                                               struct.unpack_from('<Q', kv[0])[0] >> 60, kv[0][8:]))

    def build(self):
        bs, xid = BS, self.xid
        root = [inode(2, parent=1, mode=0o40755)] + self.live
        self.write(14, node(ROOT_TREE_OID, xid, 0x2, 0x0E, self.fs_records(root)))
        for block, (old_xid, records, is_root) in self.orphans.items():
            self.write(block, node(ROOT_TREE_OID, old_xid, 0x2 if is_root else 0x3, 0x0E, self.fs_records(records), root=is_root))
        # object maps
        omap_value = lambda paddr: struct.pack('<IIQ', 0, bs, paddr)
        self.write(10, node(10, xid, 0x40000002, 0x0B, [(struct.pack('<QQ', VOLUME_OID, xid), omap_value(11))], fixed=(16, 16)))
        mappings = [(snap['xid'], snap['node']) for snap in self.snaps] + [(xid, 14)]
        self.write(13, node(13, xid, 0x40000002, 0x0B, [(struct.pack('<QQ', ROOT_TREE_OID, at), omap_value(paddr))
                                                        for at, paddr in sorted(mappings)], fixed=(16, 16)))
        snap_entries = []
        for snap in self.snaps:
            if snap['share'] is None:
                held = [inode(2, parent=1, mode=0o40755)] + (self.live if snap['with_live'] else []) + snap['records']
                self.write(snap['node'], node(ROOT_TREE_OID, snap['xid'], 0x2, 0x0E, self.fs_records(held)))
            raw = snap['name'].encode() + b'\0'
            snap_entries.append((struct.pack('<Q', snap['xid'] | 1 << 60),
                                 struct.pack('<QQQQQIIH', 0, snap['superblock'], 1_750_000_000 * 10 ** 9 + snap['xid'],
                                             0, 900 + snap['xid'], 0x40000002, snap['flags'], len(raw)) + raw))
            snap_entries.append((struct.pack('<QH', ((1 << 60) - 1) | 11 << 60, len(raw)) + raw, struct.pack('<Q', snap['xid'])))
        snap_entries.sort(key=lambda kv: (struct.unpack_from('<Q', kv[0])[0] & ((1 << 60) - 1), kv[0][8:]))
        self.write(17, node(17, xid, 0x40000002, 0x10, snap_entries))
        for at, tree in ((9, 10), (12, 13)):
            block = header(at, xid, 0x4000000B)
            struct.pack_into('<IIIIQ', block, 32, 0, 0, 0x40000002, 0x40000002, tree)
            self.write(at, seal(block))
        # volume superblock
        volume = header(VOLUME_OID, xid, 0x0D)
        volume[32:36] = b'APSB'
        struct.pack_into('<Q', volume, 56, 1 if self.hashed else 0)
        struct.pack_into('<I', volume, 116, 2)
        struct.pack_into('<QQ', volume, 128, 12, ROOT_TREE_OID)
        struct.pack_into('<I', volume, 124, 0x40000002)
        struct.pack_into('<Q', volume, 152, 17)
        struct.pack_into('<Q', volume, 216, len(self.snaps) if self.snapshot_count is None else self.snapshot_count)
        struct.pack_into('<Q', volume, 264, 0 if self.encrypted else 1)
        volume[704:713] = b'DR test\0\0'
        for snap in self.snaps:                  # snapshot superblocks: physical copies without their own maps
            copy = bytearray(volume)
            struct.pack_into('<QQI', copy, 8, snap['superblock'], snap['xid'], 0x4000000D)
            struct.pack_into('<Q', copy, 128, 0); struct.pack_into('<Q', copy, 152, 0); struct.pack_into('<Q', copy, 216, 0)
            self.write(snap['superblock'], seal(copy))
        self.write(11, seal(volume))
        # free queue (checkpoint data block 6) and space manager (block 5)
        entries = [(struct.pack('<QQ', q_xid, start), struct.pack('<Q', count)) for q_xid, start, count in sorted(self.queued)]
        self.write(6, node(FREE_QUEUE_OID, xid, 0x80000002, 0x09, entries, fixed=(16, 8)))
        bits = 0
        for number in self.used:
            bits |= 1 << number
        self.write(16, bits.to_bytes(bs, 'little'))
        free = self.blocks - len(self.used)
        cib = header(15, xid, 0x40000007)
        struct.pack_into('<II', cib, 32, 0, 1)
        struct.pack_into('<QQIIQ', cib, 40, xid, 0, self.blocks, free, 16)
        self.write(15, seal(cib))
        spaceman = header(SPACEMAN_OID, xid, 0x80000005)
        struct.pack_into('<IIII', spaceman, 32, bs, bs * 8, (bs - 40) // 32, (bs - 40) // 8)
        struct.pack_into('<QQIIQI', spaceman, 48, self.blocks, 1, 1, 0, free, 1024)
        struct.pack_into('<QQ', spaceman, 240, sum(n for _x, _s, n in self.queued), FREE_QUEUE_OID)
        struct.pack_into('<Q', spaceman, 1024, 15)
        self.write(5, seal(spaceman))
        # checkpoint: map at descriptor block 1, superblock at descriptor block 2 (and copy at block 0)
        cpm = header(1, xid, 0x4000000C)
        struct.pack_into('<II', cpm, 32, 1, 2)
        struct.pack_into('<IIIIQQQ', cpm, 40, 0x80000005, 0, bs, 0, 0, SPACEMAN_OID, 5)
        struct.pack_into('<IIIIQQQ', cpm, 80, 0x80000002, 0x09, bs, 0, 0, FREE_QUEUE_OID, 6)
        self.write(1, seal(cpm))
        sb = header(1, xid, 0x80000001)
        sb[32:36] = b'NXSB'
        struct.pack_into('<IQ', sb, 36, bs, self.blocks)
        struct.pack_into('<Q', sb, 64, 2)
        struct.pack_into('<QQIIQQ', sb, 88, 2048, xid + 1, 4, 4, 1, 5)
        struct.pack_into('<IIII', sb, 136, 0, 2, 0, 2)
        struct.pack_into('<QQ', sb, 152, SPACEMAN_OID, 9)
        struct.pack_into('<I', sb, 180, 100)
        struct.pack_into('<Q', sb, 184, VOLUME_OID)
        if self.volumes > 1:
            struct.pack_into('<Q', sb, 192, VOLUME_OID + 4)
        sb = seal(sb)
        self.write(2, sb); self.write(0, sb)
        return b'\0' * self.offset + bytes(self.data)

    def image(self):
        return self.build()

    def reader(self):
        return MemoryReader(self.build())

    @property
    def partition(self):
        return Partition(self.offset, self.blocks * BS)

    def at(self, block):
        return self.offset + block * BS

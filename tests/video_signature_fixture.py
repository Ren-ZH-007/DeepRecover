"""Construct small container/sample-table fixtures, without claiming decodability."""
import struct


def atom(kind, payload, *, large=False):
    if large:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def table(kind, rows, width):
    return atom(kind, b'\0' * 4 + struct.pack('>I', len(rows)) + b''.join(n.to_bytes(width, 'big') for row in rows for n in row))


def movie(*, mov=False, last=False, co64=False, large=False, multiple=False, payload=b'12345678'):
    ftyp = atom(b'ftyp', (b'qt  ' if mov else b'isom') + b'\0' * 4 + b'isom')
    clock = b'\0' * 12 + struct.pack('>II', 1000, 2000)
    tkhd = bytearray(84); struct.pack_into('>I', tkhd, 12, 1)
    visual = bytearray(78); struct.pack_into('>H', visual, 6, 1); struct.pack_into('>HH', visual, 24, 96, 64)
    avcc = b'\1\x42\0\x0a\xff\xe1\0\1\x67\1\0\1\x68'
    sample = atom(b'avc1', visual + atom(b'avcC', avcc))
    dref = atom(b'dref', b'\0' * 4 + struct.pack('>I', 1) + atom(b'url ', b'\0\0\0\1'))
    def moov(offsets):
        samples = (atom(b'stsd', b'\0' * 4 + struct.pack('>I', 1) + sample)
                   + table(b'stts', [(2, 1000)], 4) + table(b'stsc', [(1, 1, 1)], 4)
                   + atom(b'stsz', b'\0' * 4 + struct.pack('>II', 4, 2))
                   + table(b'co64' if co64 else b'stco', [(o,) for o in offsets], 8 if co64 else 4))
        info = atom(b'vmhd', b'\0' * 12) + atom(b'dinf', dref) + atom(b'stbl', samples)
        media = atom(b'mdia', atom(b'mdhd', clock + b'\0' * 4) + atom(b'hdlr', b'\0' * 8 + b'vide' + b'\0' * 12) + atom(b'minf', info))
        return atom(b'moov', atom(b'mvhd', clock + b'\0' * 80) + atom(b'trak', atom(b'tkhd', tkhd) + media))
    header = 16 if large else 8
    before = len(ftyp) + (0 if last else len(moov([0, 0])))
    offsets = [before + header, before + header + (header + 4 if multiple else 4)]
    mdats = (atom(b'mdat', payload[:4], large=large) + atom(b'mdat', payload[4:], large=large)) if multiple else atom(b'mdat', payload, large=large)
    return ftyp + (mdats + moov(offsets) if last else moov(offsets) + mdats)


CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'dinf'}

def edit_box(data, target, transform):
    """Rebuild lengths through ancestor boxes when changing one metadata box."""
    result = b''; position = 0
    while position < len(data):
        size, kind = struct.unpack_from('>I4s', data, position)
        assert size >= 8
        payload = data[position + 8:position + size]
        if kind == target:
            payload = transform(payload)
        elif kind in CONTAINERS:
            payload = edit_box(payload, target, transform)
        result += atom(kind, payload)
        position += size
    return result


def mov_fixture(mp4):
    """Move real fixture moov to the tail and rebase stco offsets; keep media bytes."""
    items, position = [], 0
    while position < len(mp4):
        size, kind = struct.unpack_from('>I4s', mp4, position)
        items.append((kind, position, mp4[position:position + size]))
        position += size
    _, old_start, moov = next(item for item in items if item[0] == b'moov')
    old_end = old_start + len(moov)
    def rebase(payload):
        count = int.from_bytes(payload[4:8], 'big')
        values = [int.from_bytes(payload[8 + 4 * i:12 + 4 * i], 'big') for i in range(count)]
        assert all(value >= old_end for value in values)
        return payload[:8] + b''.join(struct.pack('>I', value - len(moov)) for value in values)
    moov = edit_box(moov, b'stco', rebase)
    result = b''
    for kind, _, data in items:
        if kind == b'ftyp': data = data[:8] + b'qt  ' + data[12:]
        if kind != b'moov': result += data
    return result + moov

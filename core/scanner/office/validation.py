"""Static structure checks for Office documents; nothing is rendered or executed.

OOXML (docx/xlsx/pptx and their template and macro variants) is a ZIP archive:
a candidate starts at a local header whose entry name is a usual first part
and ends at the end-of-central-directory record that points back to it. Every
entry is matched against the central directory and inflated under a budget
with its CRC-32 checked; [Content_Types].xml and _rels/.rels must name an
existing main part. Legacy doc/xls/ppt files are OLE2 compound files: the
header, FAT, DIFAT, MiniFAT and directory tree are walked, and every stream's
sector chain must have the right length, stay in range and not be shared.

``validate`` returns None when the bytes are not an Office document at all
(another ZIP, an entry inside an archive that starts earlier, an unrelated
compound file), so ordinary archives do not flood the candidate list.
"""
from dataclasses import dataclass
import re
import struct
import xml.etree.ElementTree as ElementTree
import zlib

MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_XML = 1024 * 1024
MAX_DIRECTORY = 10000
MAX_DESCRIPTOR_OUTPUT = 256 * 1024 * 1024
ZIP_LOCAL = b'PK\x03\x04'
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
START_NAMES = ('[Content_Types].xml', '_rels/.rels')
START_PREFIXES = ('docProps/',)
PART_PREFIXES = ('[Content_Types].xml', '_rels/', 'docProps/', 'word/', 'xl/', 'ppt/', 'customXml/')
CT_NS = '{http://schemas.openxmlformats.org/package/2006/content-types}'
REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'
OFFICE_DOCUMENT = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
VBA_TYPE = 'application/vnd.ms-office.vbaProject'
_OX = 'application/vnd.openxmlformats-officedocument.'
MAIN_TYPES = {
    _OX + 'wordprocessingml.document.main+xml': ('docx', False),
    _OX + 'wordprocessingml.template.main+xml': ('dotx', False),
    'application/vnd.ms-word.document.macroEnabled.main+xml': ('docm', True),
    'application/vnd.ms-word.template.macroEnabledTemplate.main+xml': ('dotm', True),
    _OX + 'spreadsheetml.sheet.main+xml': ('xlsx', False),
    _OX + 'spreadsheetml.template.main+xml': ('xltx', False),
    'application/vnd.ms-excel.sheet.macroEnabled.main+xml': ('xlsm', True),
    'application/vnd.ms-excel.template.macroEnabled.main+xml': ('xltm', True),
    'application/vnd.ms-excel.sheet.binary.macroEnabled.main': ('xlsb', False),
    _OX + 'presentationml.presentation.main+xml': ('pptx', False),
    _OX + 'presentationml.slideshow.main+xml': ('ppsx', False),
    _OX + 'presentationml.template.main+xml': ('potx', False),
    'application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml': ('pptm', True),
    'application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml': ('ppsm', True),
    'application/vnd.ms-powerpoint.template.macroEnabled.main+xml': ('potm', True),
}
OFFICE_TYPES = frozenset({kind for kind, _ in MAIN_TYPES.values()} | {'doc', 'xls', 'ppt'})
FREE, END, FAT_SECT, DIF_SECT, MAX_REGULAR = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC, 0xFFFFFFFA
NO_STREAM = 0xFFFFFFFF


class Rejected(Exception):
    def __init__(self, reason='office_invalid_structure'):
        self.reason = reason


class NotOffice(Exception):
    pass


def require(condition, reason='office_invalid_structure'):
    if not condition:
        raise Rejected(reason)


@dataclass(frozen=True)
class OfficeValidation:
    size: int
    integrity: str
    file_type: str = 'ooxml'
    details: dict | None = None


def _read(stream, start, size, token):
    stream.seek(start)
    chunks, read = [], 0
    while read < size:
        token.checkpoint()
        block = stream.read(min(1024 * 1024, size - read))
        if not block:
            break
        chunks.append(block); read += len(block)
    return b''.join(chunks)


def validate(stream, start, source_size, kind, token, max_size=MAX_BYTES, max_decoded=256 * 1024 * 1024):
    available = max(0, source_size - start)
    cap = min(MAX_BYTES, max_size, available)
    limited = available > min(MAX_BYTES, max_size)
    token.checkpoint()
    try:
        if kind == 'ooxml':
            return _ooxml(stream, start, available, cap, limited, token, max_decoded)
        return _ole(stream, start, available, cap, limited, token)
    except NotOffice:
        return None


# -- OOXML ---------------------------------------------------------------------
def _local_name(header):
    if len(header) < 30 or header[:4] != ZIP_LOCAL:
        return None
    length = struct.unpack_from('<H', header, 26)[0]
    raw = header[30:30 + length]
    if len(raw) != length or not length:
        return None
    try:
        return raw.decode('utf-8' if struct.unpack_from('<H', header, 6)[0] & 0x800 else 'cp437')
    except UnicodeDecodeError:
        return None


def _guess(name):
    return {'word/': 'docx', 'xl/': 'xlsx', 'ppt/': 'pptx'}.get(next((p for p in ('word/', 'xl/', 'ppt/') if name.startswith(p)), ''), 'ooxml')


def _deflate_end(stream, at, limit, token):
    """Length of the raw deflate stream starting at ``at`` (for entries with data descriptors)."""
    engine, consumed, produced = zlib.decompressobj(-15), 0, 0
    stream.seek(at)
    while not engine.eof:
        token.checkpoint()
        if consumed >= limit:
            return None
        block = stream.read(min(1024 * 1024, limit - consumed))
        if not block:
            return None
        while block and not engine.eof:
            try:
                produced += len(engine.decompress(block, 1024 * 1024))
            except zlib.error:
                raise Rejected()
            require(produced <= MAX_DESCRIPTOR_OUTPUT, 'office_validation_limit')
            used = len(block) - len(engine.unconsumed_tail)
            consumed += used; block = engine.unconsumed_tail
        if engine.eof:
            consumed -= len(engine.unused_data)
    return consumed


def _walk(stream, start, available, cap, token):
    """Follow local entries by their sizes to the central directory; returns the archive size.

    Reads only the archive's own headers (and deflate data for entries that
    use data descriptors), never the unrelated bytes that follow it.
    """
    position, entries = start, 0
    end = start + min(available, cap)
    while True:
        token.checkpoint()
        if position + 4 > start + available:
            raise Rejected('office_truncated')
        if position + 4 > end:
            raise Rejected('office_validation_limit')
        stream.seek(position)
        head = stream.read(46)
        if head[:4] == b'PK\x01\x02':
            break
        require(head[:4] == ZIP_LOCAL and entries < MAX_ENTRIES,
                'office_validation_limit' if head[:4] == ZIP_LOCAL else 'office_invalid_structure')
        if len(head) < 30:
            raise Rejected('office_truncated')
        flags, method = struct.unpack_from('<HH', head, 6)
        packed, = struct.unpack_from('<I', head, 18)
        name_len, extra_len = struct.unpack_from('<HH', head, 26)
        body = position + 30 + name_len + extra_len
        if flags & 0x08:
            if method != 8:
                raise Rejected('office_unsupported')
            length = _deflate_end(stream, body, end - body, token)
            if length is None:
                raise Rejected('office_truncated' if end >= start + available else 'office_validation_limit')
            stream.seek(body + length)
            mark = stream.read(4)
            position = body + length + (16 if mark == b'PK\x07\x08' else 12)
        else:
            position = body + packed
        entries += 1
    # Central directory, then the end record that must point back to ``start``.
    cd = position
    while True:
        token.checkpoint()
        if position + 22 > start + available:
            raise Rejected('office_truncated')
        if position + 22 > end:
            raise Rejected('office_validation_limit')
        stream.seek(position)
        head = stream.read(46)
        if head[:4] == b'PK\x05\x06':
            break
        require(head[:4] == b'PK\x01\x02' and len(head) == 46)
        name_len, extra_len, note_len = struct.unpack_from('<HHH', head, 28)
        position += 46 + name_len + extra_len + note_len
    cd_size, cd_offset, comment = struct.unpack_from('<IIH', head, 12)
    if cd - cd_offset < start:
        raise NotOffice()          # This header is an entry inside an archive that starts earlier.
    require(cd - cd_offset == start and position - cd == cd_size)
    size = position + 22 + comment - start
    if start + size > start + available:
        raise Rejected('office_truncated')
    if size > min(available, cap):
        raise Rejected('office_validation_limit')
    return size, position - start


def _ooxml(stream, start, available, cap, limited, token, max_decoded):
    stream.seek(start)
    header = stream.read(min(30 + 512, available))
    name = _local_name(header)
    if name is None or not name.startswith(PART_PREFIXES):
        raise NotOffice()
    # Only a usual first entry is reported when the archive cannot be followed;
    # other part names may be entries of a damaged archive and stay silent.
    typical = name in START_NAMES or name.startswith(START_PREFIXES)
    try:
        size, at = _walk(stream, start, available, cap, token)
    except Rejected as error:
        if not typical:
            raise NotOffice()
        return OfficeValidation(min(available, 30 + len(name)), error.reason, _guess(name))
    except struct.error:
        if not typical:
            raise NotOffice()
        return OfficeValidation(min(available, 30 + len(name)), 'office_invalid_structure', _guess(name))
    data = _read(stream, start, size, token)
    if len(data) != size:
        return OfficeValidation(len(data), 'office_truncated', _guess(name))
    disk, cd_disk, here, total, cd_size, cd_offset, comment = struct.unpack_from('<HHHHIIH', data, at + 4)
    try:
        if disk or cd_disk or here != total or 0xFFFF in (here, total) or 0xFFFFFFFF in (cd_size, cd_offset):
            raise Rejected('office_unsupported')
        require(at < 20 or data[at - 20:at - 16] != b'PK\x06\x07', 'office_unsupported')
        return _ooxml_archive(bytes(data[:size]), at, total, cd_offset, token, max_decoded)
    except Rejected as error:
        return OfficeValidation(size, error.reason, _guess(name))
    except struct.error:
        return OfficeValidation(size, 'office_invalid_structure', _guess(name))


def _inflate(payload, method, expected, crc, budget, token):
    if method == 0:
        result = payload
    else:
        engine = zlib.decompressobj(-15)
        parts, produced = [], 0
        view = memoryview(payload)
        for index in range(0, len(view), 1024 * 1024):
            token.checkpoint()
            chunk = view[index:index + 1024 * 1024]
            while chunk:
                piece = engine.decompress(chunk, 1024 * 1024)
                produced += len(piece); require(produced <= expected and produced <= budget[0], 'office_invalid_structure'
                                                 if produced > expected else 'office_validation_limit')
                parts.append(piece); chunk = engine.unconsumed_tail
        piece = engine.flush(); produced += len(piece); parts.append(piece)
        require(engine.eof and not engine.unused_data)
        result = b''.join(parts)
    require(len(result) == expected and zlib.crc32(result) == crc)
    budget[0] -= len(result); require(budget[0] >= 0, 'office_validation_limit')
    return result


def _xml(data):
    require(len(data) <= MAX_XML, 'office_validation_limit')
    # Package parts never declare DTDs; refuse entity tricks outright.
    require(b'<!DOCTYPE' not in data and b'<!ENTITY' not in data)
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise Rejected()


def _ooxml_archive(data, eocd, total, cd_offset, token, max_decoded):
    require(1 <= total <= MAX_ENTRIES, 'office_validation_limit' if total > MAX_ENTRIES else 'office_invalid_structure')
    entries, position, end = {}, cd_offset, eocd
    spans = []
    for _ in range(total):
        token.checkpoint()
        require(position + 46 <= end and data[position:position + 4] == b'PK\x01\x02')
        (flags, method, _time, _date, crc, packed, unpacked, name_len, extra_len, note_len,
         disk, _internal, _external, local) = struct.unpack_from('<HHHHIIIHHHHHII', data, position + 8)
        name_raw = data[position + 46:position + 46 + name_len]
        position += 46 + name_len + extra_len + note_len
        require(position <= end and len(name_raw) == name_len and name_len)
        if 0xFFFFFFFF in (packed, unpacked, local):
            raise Rejected('office_unsupported')
        if flags & 0x41:
            raise Rejected('office_encrypted')
        require(method in (0, 8) and disk == 0, 'office_unsupported')
        try:
            name = name_raw.decode('utf-8' if flags & 0x800 else 'cp437')
        except UnicodeDecodeError:
            raise Rejected()
        require(name not in entries and not name.startswith('/') and '\\' not in name
                and '..' not in name.split('/'))
        require(local + 30 <= cd_offset and data[local:local + 4] == ZIP_LOCAL)
        local_name_len, local_extra = struct.unpack_from('<HH', data, local + 26)
        require(data[local + 30:local + 30 + local_name_len] == name_raw)
        local_method = struct.unpack_from('<H', data, local + 8)[0]
        require(local_method == method)
        body = local + 30 + local_name_len + local_extra
        stop = body + packed + (_descriptor(data, body + packed, crc, packed, unpacked) if flags & 0x08 else 0)
        require(stop <= cd_offset)
        spans.append((local, stop))
        entries[name] = (method, crc, packed, unpacked, body)
    require(position == end)
    spans.sort()
    require(spans[0][0] == 0 and all(a[1] <= b[0] for a, b in zip(spans, spans[1:])))
    budget = [max_decoded]
    content = {}
    for name, (method, crc, packed, unpacked, body) in entries.items():
        content[name] = _inflate(data[body:body + packed], method, unpacked, crc, budget, token)
        if not name.endswith('.xml') and not name.endswith('.rels') and name not in START_NAMES:
            content[name] = None   # Binary parts are checked, not kept.
    require('[Content_Types].xml' in content and '_rels/.rels' in content)
    types = _xml(content['[Content_Types].xml'])
    require(types.tag == CT_NS + 'Types')
    overrides = {}
    for item in types:
        if item.tag == CT_NS + 'Override':
            part, kind = item.get('PartName', ''), item.get('ContentType', '')
            require(part.startswith('/') and kind)
            overrides[part[1:]] = kind
    rels = _xml(content['_rels/.rels'])
    require(rels.tag == REL_NS + 'Relationships')
    targets = [r.get('Target', '') for r in rels if r.tag == REL_NS + 'Relationship' and r.get('Type') == OFFICE_DOCUMENT
               and r.get('TargetMode', 'Internal') == 'Internal']
    require(len(targets) == 1)
    main = targets[0].lstrip('/')
    require(main in entries and main in overrides)
    main_type = MAIN_TYPES.get(overrides[main])
    if main_type is None:
        raise Rejected('office_unsupported')
    if main.endswith('.xml'):
        _xml(content[main])
    file_type, macro = main_type
    active = macro or VBA_TYPE in overrides.values() or any(
        n.lower().endswith('vbaproject.bin') or '/activex/' in n.lower() for n in entries)
    size = eocd + 22 + struct.unpack_from('<H', data, eocd + 20)[0]
    details = {'check_version': 1, 'container': 'ooxml', 'entries': len(entries), 'main_part': main,
               'boundary_basis': 'end_of_central_directory', 'rendered': False}
    return OfficeValidation(size, 'office_active_content' if active else 'office_structure_checked', file_type, details)


def _descriptor(data, at, crc, packed, unpacked):
    if data[at:at + 4] == b'PK\x07\x08':
        require(data[at + 4:at + 16] == struct.pack('<III', crc, packed, unpacked))
        return 16
    require(data[at:at + 12] == struct.pack('<III', crc, packed, unpacked))
    return 12


# -- OLE2 compound files (doc / xls / ppt) -------------------------------------
def _ole(stream, start, available, cap, limited, token):
    stream.seek(start)
    header = stream.read(min(512, available))
    if len(header) < 512 or header[:8] != OLE_MAGIC:
        if len(header) < 512 and header[:8] == OLE_MAGIC[:len(header[:8])]:
            return OfficeValidation(len(header), 'office_truncated', 'ole')
        raise NotOffice()
    try:
        minor, major, order, shift, mini_shift = struct.unpack_from('<HHHHH', header, 24)
        dir_sectors, fat_count, first_dir, _tx, cutoff, first_mini, mini_count, first_dif, dif_count = \
            struct.unpack_from('<IIIIIIIII', header, 40)
        require(order == 0xFFFE and (major, shift) in ((3, 9), (4, 12)) and mini_shift == 6 and cutoff == 4096)
        require(major == 4 or dir_sectors == 0)
        sector = 1 << shift
        per = sector // 4
        require(1 <= fat_count <= cap // sector + 1, 'office_validation_limit' if fat_count > cap // sector + 1 else 'office_invalid_structure')

        def read_sector(number):
            require(number <= MAX_REGULAR)
            offset = (number + 1) * sector
            if offset + sector > available:
                raise Rejected('office_truncated')
            if offset + sector > cap:
                raise Rejected('office_validation_limit')
            stream.seek(start + offset)
            block = stream.read(sector)
            if len(block) != sector:
                raise Rejected('office_truncated')
            return block

        fat_sectors = list(struct.unpack_from('<109I', header, 76))[:min(fat_count, 109)]
        dif_sectors, current = [], first_dif
        while len(fat_sectors) < fat_count:
            token.checkpoint()
            require(current <= MAX_REGULAR and current not in dif_sectors and len(dif_sectors) < dif_count + 1)
            block = read_sector(current); dif_sectors.append(current)
            values = struct.unpack('<%dI' % per, block)
            fat_sectors.extend(values[:-1][:fat_count - len(fat_sectors)]); current = values[-1]
        require(len(dif_sectors) == dif_count and len(set(fat_sectors)) == fat_count)
        fat = []
        for number in fat_sectors:
            token.checkpoint()
            fat.extend(struct.unpack('<%dI' % per, read_sector(number)))
        require(all(n < len(fat) and fat[n] == FAT_SECT for n in fat_sectors)
                and all(n < len(fat) and fat[n] == DIF_SECT for n in dif_sectors))
        used = [n for n, value in enumerate(fat) if value != FREE]
        require(used)
        size = (used[-1] + 2) * sector
        if size > available:
            raise Rejected('office_truncated')
        if size > cap:
            raise Rejected('office_validation_limit')
        data = _read(stream, start, size, token)
        if len(data) != size:
            raise Rejected('office_truncated')
        owners = {n: 'fat' for n in fat_sectors + dif_sectors}

        def chain(first, owner, length=None):
            sectors, current = [], first
            while current != END:
                token.checkpoint()
                require(current < len(fat) and current <= used[-1] and current not in owners)
                owners[current] = owner; sectors.append(current)
                require(length is None or len(sectors) <= length)
                current = fat[current]
                require(current == END or current <= MAX_REGULAR)
            require(length is None or len(sectors) == length)
            return sectors

        def body(sectors):
            return b''.join(data[(n + 1) * sector:(n + 2) * sector] for n in sectors)

        directory = body(chain(first_dir, 'directory'))
        require(major == 3 or dir_sectors == len(directory) // sector)
        count = len(directory) // 128
        require(1 <= count <= MAX_DIRECTORY, 'office_validation_limit' if count > MAX_DIRECTORY else 'office_invalid_structure')
        entries = []
        for index in range(count):
            raw = directory[index * 128:(index + 1) * 128]
            name_len = struct.unpack_from('<H', raw, 64)[0]
            kind, _color, left, right, child = struct.unpack_from('<BBIII', raw, 66)
            first, low, high = struct.unpack_from('<III', raw, 116)
            require(kind in (0, 1, 2, 5) and name_len <= 64 and (kind == 0 or (name_len >= 2 and name_len % 2 == 0)))
            try:
                name = raw[:max(0, name_len - 2)].decode('utf-16-le')
            except UnicodeDecodeError:
                raise Rejected()
            entries.append((name, kind, left, right, child, first, low if major == 3 else low | high << 32))
        require(entries[0][1] == 5)
        # Walk the tree from the root; every node at most once.
        seen, children, streams, pending = {0}, [], [], [(entries[0][4], True)]
        while pending:
            token.checkpoint()
            node, top = pending.pop()
            if node == NO_STREAM:
                continue
            require(node < count and node not in seen and entries[node][1] in (1, 2))
            seen.add(node)
            name, kind, left, right, child = entries[node][:5]
            if top:
                children.append(name.lower())
            if kind == 2:
                streams.append(node)
            pending.extend(((left, top), (right, top), (child, False) if kind == 1 else (NO_STREAM, False)))
        root_first, root_size = entries[0][5], entries[0][6]
        mini_stream = body(chain(root_first, 'ministream', -(-root_size // sector) if root_size else 0)) if root_size else b''
        minifat = []
        if mini_count:
            mini_sectors = chain(first_mini, 'minifat', mini_count)
            minifat = list(struct.unpack('<%dI' % (len(mini_sectors) * per), body(mini_sectors)))
        else:
            require(first_mini == END)
        mini_owner = set()
        for node in streams:
            name, _kind, _l, _r, _c, first, length = entries[node]
            if length >= cutoff:
                chain(first, node, -(-length // sector))
            elif length:
                sectors, current = 0, first
                while current != END:
                    token.checkpoint()
                    require(current < len(minifat) and current not in mini_owner and (current + 1) * 64 <= len(mini_stream))
                    mini_owner.add(current); sectors += 1; current = minifat[current]
                    require(sectors <= -(-length // 64))
                require(sectors == -(-length // 64))
        names = set(children)
        if 'encryptedpackage' in names:
            raise Rejected('office_encrypted')
        file_type = 'doc' if 'worddocument' in names else 'xls' if names & {'workbook', 'book'} else (
            'ppt' if 'powerpoint document' in names else None)
        if file_type is None:
            raise NotOffice()
        active = bool(names & {'macros', '_vba_project_cur', 'vba'})
        details = {'check_version': 1, 'container': 'ole2', 'sector_size': sector, 'streams': len(streams),
                   'boundary_basis': 'highest_allocated_sector', 'rendered': False}
        return OfficeValidation(size, 'office_active_content' if active else 'office_structure_checked', file_type, details)
    except Rejected as error:
        return OfficeValidation(512, error.reason, 'ole')
    except struct.error:
        return OfficeValidation(512, 'office_invalid_structure', 'ole')

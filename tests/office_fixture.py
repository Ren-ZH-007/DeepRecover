"""Builders for minimal OOXML packages and OLE2 compound files used by the Office tests."""
import io
import struct
import zipfile

CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
OX = 'application/vnd.openxmlformats-officedocument.'
MAIN = {'docx': ('word/document.xml', OX + 'wordprocessingml.document.main+xml'),
        'xlsx': ('xl/workbook.xml', OX + 'spreadsheetml.sheet.main+xml'),
        'pptx': ('ppt/presentation.xml', OX + 'presentationml.presentation.main+xml'),
        'docm': ('word/document.xml', 'application/vnd.ms-word.document.macroEnabled.main+xml')}


def content_types(main, kind, extra=''):
    return (f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{CT}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/{main}" ContentType="{kind}"/>{extra}</Types>').encode()


def root_rels(target):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            f'Target="{target}"/></Relationships>').encode()


class _Unseekable(io.RawIOBase):
    """Forces zipfile to write data descriptors after each entry."""
    def __init__(self):
        self.buffer = bytearray()

    def writable(self):
        return True

    def write(self, data):
        self.buffer += data; return len(data)


def ooxml(kind='docx', *, order=None, parts=None, method=zipfile.ZIP_DEFLATED, comment=b'', descriptors=False,
          types=None, rels=None, body=None):
    main, main_type = MAIN[kind]
    files = {'[Content_Types].xml': types if types is not None else content_types(main, main_type),
             '_rels/.rels': rels if rels is not None else root_rels(main),
             'docProps/core.xml': b'<?xml version="1.0"?><coreProperties/>',
             main: body if body is not None else f'<?xml version="1.0"?><document><body>{kind} 文本 ' .encode() + b'x' * 400 + b'</body></document>'}
    files.update(parts or {})
    names = order or list(files)
    target = _Unseekable() if descriptors else io.BytesIO()
    with zipfile.ZipFile(target, 'w', method) as archive:
        archive.comment = comment
        for name in names:
            archive.writestr(name, files[name])
    return bytes(target.buffer) if descriptors else target.getvalue()


def plain_zip(names=('photo.jpg', 'readme.txt', 'nested/data.bin')):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, name.encode() * 50)
    return target.getvalue()


FREE, END, FAT_SECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
STREAMS = {'doc': {'WordDocument': b'W' * 5000, '1Table': b'T' * 300},
           'xls': {'Workbook': b'B' * 6000},
           'ppt': {'PowerPoint Document': b'P' * 4500, 'Current User': b'U' * 60},
           'msg': {'__substg1.0_0037001F': b'subject' * 10}}


def ole(kind='doc', *, streams=None, storages=(), major=3):
    """A compound file whose root holds ``streams`` (name -> bytes) and empty ``storages``."""
    sector = 512 if major == 3 else 4096
    streams = dict(STREAMS[kind] if streams is None else streams)
    small = {n: d for n, d in streams.items() if len(d) < 4096}
    large = {n: d for n, d in streams.items() if len(d) >= 4096}
    mini, mini_chains = bytearray(), {}
    for name, data in small.items():
        first = len(mini) // 64
        mini += data + b'\0' * (-len(data) % 64)
        mini_chains[name] = (first, -(-len(data) // 64))
    minifat = []
    for first, count in mini_chains.values():
        minifat.extend(list(range(first + 1, first + count)) + [END])
    minifat_bytes = struct.pack('<%dI' % len(minifat), *minifat) if minifat else b''
    minifat_bytes += b'\xff' * (-len(minifat_bytes) % sector)
    entries = 1 + len(streams) + len(storages)
    directory_sectors = -(-entries * 128 // sector)
    blobs = [('directory', directory_sectors), ('minifat', len(minifat_bytes) // sector),
             ('ministream', -(-len(mini) // sector))] + [(n, -(-len(d) // sector)) for n, d in large.items()]
    data_sectors = sum(count for _, count in blobs)
    fat_count = 1
    while fat_count * (sector // 4) < data_sectors + fat_count:
        fat_count += 1
    fat, starts, cursor = [FAT_SECT] * fat_count, {}, fat_count
    for name, count in blobs:
        starts[name] = cursor if count else END
        fat.extend(list(range(cursor + 1, cursor + count)) + [END] if count else [])
        cursor += count
    fat += [FREE] * (fat_count * (sector // 4) - len(fat))

    def entry(name, kind_code, child=FREE, right=FREE, start=END, size=0):
        raw = name.encode('utf-16-le') + b'\0\0'
        return (raw.ljust(64, b'\0') + struct.pack('<HBBIII', len(raw), kind_code, 1, FREE, right, child)
                + b'\0' * 36 + struct.pack('<IQ', start, size))
    names = list(streams) + list(storages)
    records = [entry('Root Entry', 5, child=1 if names else FREE, start=starts['ministream'], size=len(mini))]
    for index, name in enumerate(names, start=1):
        right = index + 1 if index < len(names) else FREE
        if name in storages:
            records.append(entry(name, 1, right=right))
        elif name in small:
            records.append(entry(name, 2, right=right, start=mini_chains[name][0], size=len(small[name])))
        else:
            records.append(entry(name, 2, right=right, start=starts[name], size=len(large[name])))
    directory = b''.join(records)
    directory = directory.ljust(directory_sectors * sector, b'\0')
    header = bytearray(512)
    header[:8] = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    struct.pack_into('<HHHHH', header, 24, 0x3E, major, 0xFFFE, 9 if major == 3 else 12, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0 if major == 3 else directory_sectors, fat_count, starts['directory'], 0, 4096,
                     starts['minifat'], len(minifat_bytes) // sector, END, 0)
    difat = list(range(fat_count)) + [FREE] * (109 - fat_count)
    struct.pack_into('<109I', header, 76, *difat)
    body = bytearray(bytes(header).ljust(sector, b'\0'))
    body += struct.pack('<%dI' % len(fat), *fat)
    body += directory + minifat_bytes + bytes(mini).ljust(-(-len(mini) // sector) * sector, b'\0')
    for name, data in large.items():
        body += data.ljust(-(-len(data) // sector) * sector, b'\0')
    return bytes(body)

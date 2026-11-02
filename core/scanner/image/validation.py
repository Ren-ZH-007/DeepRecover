"""Bounded, read-only container checks; no pixel decoding or recovery claims."""
from dataclasses import dataclass
import struct
import zlib


class InvalidStructure(Exception):
    pass


class IncompleteStructure(Exception):
    pass


class ValidationLimit(Exception):
    pass


@dataclass(frozen=True)
class Validation:
    size: int
    integrity: str


class Reader:
    def __init__(self, stream, start, source_size, max_size, token):
        self.stream, self.start = stream, start
        self.source_size, self.max_size, self.token = source_size, max_size, token
        stream.seek(start)

    @property
    def used(self):
        return self.stream.tell() - self.start

    def read(self, size):
        self.token.checkpoint()
        if self.used + size > self.max_size:
            raise ValidationLimit()
        if self.stream.tell() + size > self.source_size:
            raise IncompleteStructure()
        value = self.stream.read(size)
        if len(value) != size:
            raise IncompleteStructure()
        return value


def validate(stream, start, source_size, file_type, token, max_size, max_decoded):
    reader = Reader(stream, start, source_size, max_size, token)
    try:
        if file_type == 'png':
            png(reader, max_decoded)
        else:
            jpeg(reader)
        integrity = 'structure_checked'
    except IncompleteStructure:
        return Validation(min(source_size - start, max_size), 'truncated')
    except ValidationLimit:
        integrity = 'validation_limit'
    except (InvalidStructure, zlib.error):
        integrity = 'invalid_structure'
    return Validation(reader.used, integrity)


class PngData:
    """Check zlib end, exact scanline lengths and filter bytes without unfiltering.

    Only one 64 KiB decompression output is retained, even for huge dimensions.
    Adam7 pass lengths are counted without allocating a pixel canvas.
    """
    def __init__(self, width, height, depth, color, interlace, max_decoded):
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        passes = [(0, 0, 1, 1)] if not interlace else [
            (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
            (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
        self.passes = []
        for x, y, dx, dy in passes:
            w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
            if w and h:
                self.passes.append(((w * channels * depth + 7) // 8, h))
        self.expected = sum((row + 1) * height for row, height in self.passes)
        if self.expected > max_decoded:
            raise ValidationLimit()
        self.decoder = zlib.decompressobj()
        self.total = 0
        self.pass_index = 0
        self.rows_left = self.passes[0][1]
        self.bytes_left = 0

    def consume(self, data):
        self.total += len(data)
        if self.total > self.expected:
            raise InvalidStructure()
        cursor = 0
        while cursor < len(data):
            if self.pass_index >= len(self.passes):
                raise InvalidStructure()
            if self.bytes_left == 0:
                if data[cursor] > 4:
                    raise InvalidStructure()
                self.bytes_left = self.passes[self.pass_index][0]
                cursor += 1
            amount = min(self.bytes_left, len(data) - cursor)
            self.bytes_left -= amount
            cursor += amount
            if self.bytes_left == 0:
                self.rows_left -= 1
                if not self.rows_left:
                    self.pass_index += 1
                    if self.pass_index < len(self.passes):
                        self.rows_left = self.passes[self.pass_index][1]

    def feed(self, data, token):
        pending = data
        while pending:
            token.checkpoint()
            output = self.decoder.decompress(pending, 65536)
            self.consume(output)
            if self.decoder.unused_data:
                raise InvalidStructure()
            pending = self.decoder.unconsumed_tail

    def finish(self):
        if not self.decoder.eof or self.total != self.expected or self.pass_index != len(self.passes):
            raise InvalidStructure()


def png(reader, max_decoded):
    if reader.read(8) != b'\x89PNG\r\n\x1a\n':
        raise InvalidStructure()
    seen_header = seen_palette = seen_data = data_ended = False
    image = None
    color = depth = None
    while True:
        length, kind = struct.unpack('>I4s', reader.read(8))
        if length > 0x7fffffff or not all(65 <= x <= 90 or 97 <= x <= 122 for x in kind) or not 65 <= kind[2] <= 90:
            raise InvalidStructure()
        if not seen_header and kind != b'IHDR':
            raise InvalidStructure()
        if kind == b'IHDR' and (seen_header or length != 13):
            raise InvalidStructure()
        if kind == b'PLTE' and (seen_palette or seen_data or color in (0, 4) or not 0 < length <= 768 or length % 3):
            raise InvalidStructure()
        if kind == b'PLTE' and color == 3 and length // 3 > 2 ** depth:
            raise InvalidStructure()
        if kind == b'IDAT' and (data_ended or (color == 3 and not seen_palette)):
            raise InvalidStructure()
        if kind == b'IEND' and (length != 0 or not seen_data):
            raise InvalidStructure()
        if kind not in (b'IHDR', b'PLTE', b'IDAT', b'IEND') and kind[0] < 97:
            raise InvalidStructure()
        if seen_data and kind != b'IDAT':
            data_ended = True
        crc = zlib.crc32(kind)
        remaining = length
        header = b''
        while remaining:
            part = reader.read(min(65536, remaining))
            remaining -= len(part)
            crc = zlib.crc32(part, crc)
            if kind == b'IHDR':
                header += part
            elif kind == b'IDAT':
                image.feed(part, reader.token)
        expected_crc = struct.unpack('>I', reader.read(4))[0]
        if crc & 0xffffffff != expected_crc:
            raise InvalidStructure()
        if kind == b'IHDR':
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', header)
            allowed_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if not 0 < width <= 0x7fffffff or not 0 < height <= 0x7fffffff or depth not in allowed_depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise InvalidStructure()
            image = PngData(width, height, depth, color, interlace, max_decoded)
            seen_header = True
        elif kind == b'PLTE':
            seen_palette = True
        elif kind == b'IDAT':
            seen_data = True
        elif kind == b'IEND':
            image.finish()
            return


def jpeg(reader):
    if reader.read(2) != b'\xff\xd8':
        raise InvalidStructure()
    frame = None
    quantization = set()
    huffman = set()
    frame_tables = {}
    scanned_components = set()
    progressive = False
    seen_scan = seen_entropy = False
    scan_entropy = False
    restart_interval = 0
    in_scan = False
    while True:
        if in_scan:
            available = min(65536, reader.source_size - reader.stream.tell(), reader.max_size - reader.used)
            if available <= 0:
                if reader.used >= reader.max_size:
                    raise ValidationLimit()
                raise IncompleteStructure()
            position = reader.stream.tell()
            block = reader.read(available)
            at = block.find(b'\xff')
            if at < 0:
                seen_entropy = scan_entropy = True
                continue
            if at > 0:
                seen_entropy = scan_entropy = True
            reader.stream.seek(position + at)
        value = reader.read(1)[0]
        if value != 0xff:
            if not in_scan:
                raise InvalidStructure()
            seen_entropy = True
            continue
        marker = reader.read(1)[0]
        while marker == 0xff:
            marker = reader.read(1)[0]
        if in_scan and (marker == 0 or 0xd0 <= marker <= 0xd7):
            if marker == 0:
                seen_entropy = scan_entropy = True
            elif not restart_interval:
                raise InvalidStructure()
            continue
        if in_scan and not scan_entropy:
            raise InvalidStructure()
        in_scan = False
        if marker == 0xd9:
            if frame is None or not seen_scan or not seen_entropy or scanned_components != frame:
                raise InvalidStructure()
            return
        if marker in (0, 1, 0xd8) or 0xd0 <= marker <= 0xd7:
            raise InvalidStructure()
        length = struct.unpack('>H', reader.read(2))[0]
        if length < 2:
            raise InvalidStructure()
        data = reader.read(length - 2)
        if marker in (0xc0, 0xc2):
            if frame is not None or len(data) < 6:
                raise InvalidStructure()
            precision, height, width, count = struct.unpack('>BHHB', data[:6])
            if precision != 8 or not width or not height or count not in (1, 3, 4) or len(data) != 6 + 3 * count:
                raise InvalidStructure()
            frame = set(data[6::3])
            if len(frame) != count:
                raise InvalidStructure()
            for sampling, table in zip(data[7::3], data[8::3]):
                if not 1 <= sampling >> 4 <= 4 or not 1 <= sampling & 15 <= 4 or table > 3:
                    raise InvalidStructure()
            frame_tables = dict(zip(data[6::3], data[8::3]))
            progressive = marker == 0xc2
        elif marker == 0xda:
            if frame is None or len(data) < 6:
                raise InvalidStructure()
            count = data[0]
            if not 1 <= count <= len(frame) or len(data) != 4 + 2 * count:
                raise InvalidStructure()
            components = data[1:1 + 2 * count:2]
            if len(set(components)) != count or not set(components).issubset(frame):
                raise InvalidStructure()
            if any(table >> 4 > 3 or table & 15 > 3 for table in data[2:1 + 2 * count:2]):
                raise InvalidStructure()
            start, end, approx = data[-3:]
            if not progressive and (start, end, approx) != (0, 63, 0):
                raise InvalidStructure()
            if progressive and (not 0 <= start <= end <= 63 or (start == 0 and end != 0) or (start > 0 and count != 1) or approx >> 4 > 13 or approx & 15 > 13):
                raise InvalidStructure()
            if progressive and approx >> 4 and approx >> 4 != (approx & 15) + 1:
                raise InvalidStructure()
            for component, tables in zip(components, data[2:1 + 2 * count:2]):
                if frame_tables[component] not in quantization:
                    raise InvalidStructure()
                if (not progressive or start == 0) and (0, tables >> 4) not in huffman:
                    raise InvalidStructure()
                if (not progressive or start > 0) and (1, tables & 15) not in huffman:
                    raise InvalidStructure()
            scanned_components.update(components)
            seen_scan = in_scan = True
            scan_entropy = False
        elif marker == 0xdb:
            cursor = 0
            while cursor < len(data):
                table = data[cursor]
                if table >> 4 > 1 or table & 15 > 3:
                    raise InvalidStructure()
                quantization.add(table & 15)
                cursor += 1 + 64 * (1 + (table >> 4))
            if not data or cursor != len(data):
                raise InvalidStructure()
        elif marker == 0xc4:
            cursor = 0
            while cursor < len(data):
                if cursor + 17 > len(data) or data[cursor] >> 4 > 1 or data[cursor] & 15 > 3:
                    raise InvalidStructure()
                counts = data[cursor + 1:cursor + 17]
                huffman.add((data[cursor] >> 4, data[cursor] & 15))
                space = 1
                for amount in counts:
                    space = space * 2 - amount
                    if space < 0:
                        raise InvalidStructure()
                symbols = sum(counts)
                if not 0 < symbols <= 256:
                    raise InvalidStructure()
                cursor += 17 + symbols
            if not data or cursor != len(data):
                raise InvalidStructure()
        elif marker == 0xdd:
            if len(data) != 2:
                raise InvalidStructure()
            restart_interval = struct.unpack('>H', data)[0]
        elif marker not in (*range(0xe0, 0xf0), 0xfe):
            # Arithmetic/lossless/other JPEG modes are outside this milestone.
            raise ValidationLimit()

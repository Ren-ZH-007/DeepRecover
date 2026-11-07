"""Locate a self-contained MP4/MOV prefix using boxes and sample byte ranges.

This validates a conservative subset of ISO BMFF, not compressed media frames.
There is no universal MP4 end marker: the accepted range ends at the last box
needed to contain the movie metadata and all referenced samples. Trailing boxes
are not inferred. No filesystem fragmentation or deletion evidence is inferred.
"""
from dataclasses import dataclass
import struct

MAX_METADATA = 8 * 1024 * 1024
MAX_ENTRIES = 200000
MAX_BOXES = 10000
BRANDS = {b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'qt  ', b'M4V '}
# MPEG-4 audio and audiobooks: one AAC track, no video (reported as m4a audio).
AUDIO_BRANDS = {b'M4A ', b'M4B ', b'F4A ', b'F4B '}
FRAGMENTED = {b'mvex', b'moof', b'mfra', b'styp', b'sidx'}
ENCRYPTED = {b'pssh', b'encv', b'enca', b'sinf', b'senc', b'saio', b'saiz'}
EXTERNAL = {b'rmra', b'rmda', b'rdrf', b'url ', b'urn '}


class Rejected(Exception):
    def __init__(self, reason='invalid_structure'):
        self.reason = reason


@dataclass(frozen=True)
class VideoValidation:
    size: int
    integrity: str
    file_type: str = 'mp4'
    details: dict | None = None


def require(condition, reason='invalid_structure'):
    if not condition:
        raise Rejected(reason)


class Movie:
    """Parse bounded metadata; never load the media payload during validation."""
    def __init__(self, data, token, budget):
        self.data, self.token, self.budget = data, token, budget
        self.track_ids = set()
        self.samples = 0
        self.video_tracks = 0

    def uint(self, start, size=4):
        require(0 <= start and start + size <= len(self.data))
        return int.from_bytes(self.data[start:start + size], 'big')

    def boxes(self, start, end):
        result = []
        while start < end:
            self.token.checkpoint()
            self.budget[0] -= 1
            require(self.budget[0] >= 0, 'validation_limit')
            require(end - start >= 8)
            size = self.uint(start)
            kind = self.data[start + 4:start + 8]
            header = 8
            if size == 1:
                require(end - start >= 16)
                size, header = self.uint(start + 8, 8), 16
            require(size >= header and start + size <= end)
            result.append((kind, start + header, start + size))
            start += size
        return result

    def allowed(self, items, kinds):
        for kind, _, _ in items:
            if kind in FRAGMENTED:
                raise Rejected('fragmented')
            if kind in ENCRYPTED:
                raise Rejected('encrypted')
            if kind in EXTERNAL:
                raise Rejected('external_reference')
            require(kind in kinds, 'unsupported')

    @staticmethod
    def one(items, kind):
        found = [item[1:] for item in items if item[0] == kind]
        require(len(found) == 1)
        return found[0]

    def full(self, start, end, versions=(0,)):
        require(end - start >= 4 and self.data[start] in versions)
        require(self.data[start + 1:start + 4] == b'\0\0\0')
        return self.data[start]

    def table(self, items, kind, width, versions=(0,)):
        start, end = self.one(items, kind)
        self.full(start, end, versions)
        require(end - start >= 8)
        count = self.uint(start + 4)
        require(count <= MAX_ENTRIES, 'validation_limit')
        require(end - start == 8 + count * width)
        return start + 8, count

    def clock(self, items, kind):
        start, end = self.one(items, kind)
        version = self.full(start, end, (0, 1))
        minimum = (100 if kind == b'mvhd' else 24) + (12 if version else 0)
        require(end - start == minimum)
        at = start + (20 if version else 12)
        scale, duration = self.uint(at), self.uint(at + 4, 8 if version else 4)
        require(scale > 0 and duration > 0)
        return scale, duration

    def metadata(self, items):
        for kind, start, end in items:
            if kind == b'edts':
                edits = self.boxes(start, end)
                self.allowed(edits, {b'elst'})
                a, b = self.one(edits, b'elst')
                version = self.full(a, b, (0, 1))
                require(b - a >= 8)
                count = self.uint(a + 4)
                require(count <= MAX_ENTRIES, 'validation_limit')
                require(b - a == 8 + count * (20 if version else 12))
            if kind != b'udta':
                continue
            for tag, a, b in self.boxes(start, end):
                self.allowed([(tag, a, b)], {b'meta', b'name', b'cprt', b'chpl', b'free', b'WLOC'} | ({tag} if tag.startswith(b'\xa9') else set()))
                if tag == b'meta':
                    self.full(a, b)
                    metadata = self.boxes(a + 4, b)
                    self.allowed(metadata, {b'hdlr', b'keys', b'ilst', b'free'})
                    for entry, x, y in metadata:
                        if entry == b'ilst':
                            for item, c, d in self.boxes(x, y):
                                require(item not in EXTERNAL, 'external_reference')
                                require(item not in ENCRYPTED, 'encrypted')
                                self.allowed(self.boxes(c, d), {b'data', b'mean', b'name'})

    def avcc(self, start, end):
        require(end - start >= 7 and self.data[start] == 1)
        require(self.data[start + 4] & 3 != 2)
        position = start + 6
        def nal_units(count, nal_type):
            nonlocal position
            require(count > 0)
            for _ in range(count):
                self.token.checkpoint()
                require(position + 2 <= end)
                length = self.uint(position, 2); position += 2
                require(length > 0 and position + length <= end)
                require(self.data[position] & 31 == nal_type)
                position += length
        nal_units(self.data[start + 5] & 31, 7)
        require(position < end)
        count = self.data[position]; position += 1
        nal_units(count, 8)
        if position < end:
            require(self.data[start + 1] in (100, 110, 122, 144), 'unsupported')
            require(end - position >= 4)
            count = self.data[position + 3]; position += 4
            if count:
                nal_units(count, 13)
        require(position == end)

    def esds(self, start, end):
        self.full(start, end)
        def descriptor(position, bound, tag):
            require(position < bound and self.data[position] == tag)
            position += 1
            size = 0
            for _ in range(4):
                require(position < bound)
                byte = self.data[position]; position += 1
                size = (size << 7) | (byte & 127)
                if not byte & 128:
                    break
            else:
                raise Rejected()
            require(position + size <= bound)
            return position, position + size
        a, b = descriptor(start + 4, end, 3)
        require(b == end and b - a >= 3)
        require(not self.data[a + 2] & 0xe0, 'external_reference')
        c, d = descriptor(a + 3, b, 4)
        require(d - c >= 13)
        require(self.data[c] == 0x40 and self.data[c + 1] >> 2 == 5, 'unsupported')
        e, f = descriptor(c + 13, d, 5)
        require(f == d and f - e >= 2)
        g, h = descriptor(d, b, 6)
        require(h == b and h - g == 1 and self.data[g] == 2, 'unsupported')

    def samples_for_track(self, start, end):
        track = self.boxes(start, end)
        self.allowed(track, {b'tkhd', b'edts', b'mdia', b'udta', b'free'})
        self.metadata(track)
        a, b = self.one(track, b'tkhd')
        require(b - a >= 4 and self.data[a] in (0, 1))
        version = self.data[a]
        require(b - a == (96 if version else 84))
        track_id = self.uint(a + (20 if version else 12))
        require(track_id > 0 and track_id not in self.track_ids)
        self.track_ids.add(track_id)
        media = self.boxes(*self.one(track, b'mdia'))
        self.allowed(media, {b'mdhd', b'hdlr', b'minf', b'elng', b'free'})
        _, media_duration = self.clock(media, b'mdhd')
        a, b = self.one(media, b'hdlr')
        self.full(a, b)
        require(b - a >= 24)
        handler = self.data[a + 8:a + 12]
        require(handler in (b'vide', b'soun'), 'unsupported')
        if handler == b'vide':
            self.video_tracks += 1
        info = self.boxes(*self.one(media, b'minf'))
        self.allowed(info, {b'vmhd', b'smhd', b'hdlr', b'dinf', b'stbl', b'free'})
        self.one(info, b'vmhd' if handler == b'vide' else b'smhd')
        dinf = self.boxes(*self.one(info, b'dinf'))
        self.allowed(dinf, {b'dref'})
        a, b = self.one(dinf, b'dref')
        self.full(a, b)
        require(b - a >= 8)
        refs = self.boxes(a + 8, b)
        require(self.uint(a + 4) == 1 and len(refs) == 1, 'external_reference')
        kind, a, b = refs[0]
        require(kind == b'url ' and self.data[a:b] == b'\0\0\0\1', 'external_reference')
        table = self.boxes(*self.one(info, b'stbl'))
        self.allowed(table, {b'stsd', b'stts', b'stss', b'ctts', b'stsc', b'stsz', b'stco', b'co64', b'sdtp', b'sgpd', b'sbgp', b'free'})
        # All singleton table types must be unique, including optional tables.
        for kind in {item[0] for item in table} - {b'free'}:
            self.one(table, kind)
        a, b = self.one(table, b'stsd')
        self.full(a, b)
        require(b - a >= 8)
        entries = self.boxes(a + 8, b)
        require(self.uint(a + 4) == 1 and len(entries) == 1, 'unsupported')
        codec, a, b = entries[0]
        require(codec not in ENCRYPTED, 'encrypted')
        require(b - a >= 8 and self.uint(a + 6, 2) == 1, 'external_reference')
        if handler == b'vide':
            require(codec == b'avc1', 'unsupported')
            require(b - a >= 78 and self.uint(a + 24, 2) > 0 and self.uint(a + 26, 2) > 0)
            extensions = self.boxes(a + 78, b)
            self.allowed(extensions, {b'avcC', b'pasp', b'btrt', b'colr', b'clap', b'fiel', b'free'})
            self.avcc(*self.one(extensions, b'avcC'))
        else:
            require(codec == b'mp4a', 'unsupported')
            require(b - a >= 28)
            require(self.uint(a + 8, 2) == 0, 'unsupported')
            extensions = self.boxes(a + 28, b)
            self.allowed(extensions, {b'esds', b'btrt', b'free'})
            self.esds(*self.one(extensions, b'esds'))
        a, b = self.one(table, b'stsz')
        self.full(a, b)
        require(b - a >= 12)
        fixed, count = self.uint(a + 4), self.uint(a + 8)
        require(0 < count <= MAX_ENTRIES, 'validation_limit' if count > MAX_ENTRIES else 'invalid_structure')
        require(b - a == 12 + (0 if fixed else 4 * count))
        sizes = [fixed] * count if fixed else [self.uint(a + 12 + 4 * i) for i in range(count)]
        require(all(size > 0 for size in sizes))
        self.samples += count
        require(self.samples <= MAX_ENTRIES, 'validation_limit')
        for kind in (b'stts', b'ctts'):
            if kind == b'ctts' and not any(item[0] == kind for item in table):
                continue
            at, entries = self.table(table, kind, 8, (0, 1) if kind == b'ctts' else (0,))
            total = duration = 0
            for i in range(entries):
                self.token.checkpoint()
                n, value = self.uint(at + 8 * i), self.uint(at + 8 * i + 4)
                require(n > 0 and (kind != b'stts' or value > 0))
                total += n
                duration += n * value
            require(total == count)
            if kind == b'stts':
                require(duration == media_duration)
        if any(item[0] == b'stss' for item in table):
            at, entries = self.table(table, b'stss', 4)
            previous = 0
            for i in range(entries):
                self.token.checkpoint()
                current = self.uint(at + 4 * i)
                require(previous < current <= count)
                previous = current
        descriptions, group_indices = None, []
        for kind, a, b in table:
            if kind == b'sdtp':
                self.full(a, b)
                require(b - a == 4 + count)
            elif kind in (b'sgpd', b'sbgp'):
                self.full(a, b, (0, 1, 2) if kind == b'sgpd' else (0, 1))
                require(b - a >= 12)
                group = self.data[a + 4:a + 8]
                require(group != b'seig', 'encrypted')
                require(group in (b'roll', b'prol'), 'unsupported')
                if kind == b'sgpd':
                    # Only fixed two-byte roll recovery descriptions are admitted.
                    version = self.data[a]
                    require(version == 1, 'unsupported')
                    require(b - a >= 16 and self.uint(a + 8) == 2, 'unsupported')
                    n = self.uint(a + 12)
                    require(n <= MAX_ENTRIES, 'validation_limit')
                    require(b - a == 16 + 2 * n)
                    descriptions = n
                else:
                    version = self.data[a]
                    require(b - a >= (16 if version else 12))
                    n = self.uint(a + (12 if version else 8))
                    at = a + (16 if version else 12)
                    require(n <= MAX_ENTRIES, 'validation_limit')
                    require(b - at == 8 * n)
                    total = 0
                    for i in range(n):
                        self.token.checkpoint()
                        samples = self.uint(at + 8 * i)
                        require(samples > 0)
                        total += samples
                        group_indices.append(self.uint(at + 8 * i + 4))
                    require(total == count)
        require(all(index == 0 or (descriptions is not None and index <= descriptions) for index in group_indices))
        offsets_kind = [item[0] for item in table if item[0] in (b'stco', b'co64')]
        require(len(offsets_kind) == 1)
        width = 8 if offsets_kind[0] == b'co64' else 4
        at, chunks = self.table(table, offsets_kind[0], width)
        require(chunks > 0)
        offsets = [self.uint(at + width * i, width) for i in range(chunks)]
        at, n = self.table(table, b'stsc', 12)
        require(n > 0)
        mapping = []
        for i in range(n):
            self.token.checkpoint()
            first, per, description = (self.uint(at + 12 * i + j) for j in (0, 4, 8))
            require(0 < first <= chunks and per > 0 and description == 1)
            require(first > (mapping[-1][0] if mapping else 0))
            mapping.append((first, per))
        require(mapping[0][0] == 1)
        ranges, sample, entry = [], 0, 0
        for chunk, offset in enumerate(offsets, 1):
            self.token.checkpoint()
            if entry + 1 < len(mapping) and chunk == mapping[entry + 1][0]:
                entry += 1
            per = mapping[entry][1]
            require(sample + per <= count)
            length = sum(sizes[sample:sample + per]); sample += per
            ranges.append((offset, offset + length))
        require(sample == count)
        return ranges

    def parse(self):
        items = self.boxes(0, len(self.data))
        self.allowed(items, {b'mvhd', b'trak', b'udta', b'free'})
        self.metadata(items)
        self.clock(items, b'mvhd')
        tracks = [item for item in items if item[0] == b'trak']
        require(1 <= len(tracks) <= 8, 'unsupported')
        ranges = []
        for _, start, end in tracks:
            ranges.extend(self.samples_for_track(start, end))
        # One H.264 video track (plus optional AAC), or a single AAC track (MPEG-4 audio).
        require(self.video_tracks == 1 or (self.video_tracks == 0 and len(tracks) == 1), 'unsupported')
        ranges.sort()
        require(all(ranges[i - 1][1] <= ranges[i][0] for i in range(1, len(ranges))))
        return ranges


def validate(stream, start, source_size, token, max_size):
    """Return an absolute-source-bounded candidate; rejected ranges are not exportable."""
    available = max(0, source_size - start)
    seen, position, file_type, prefix = 0, 0, 'mp4', 'video_'
    budget, mdats, movie, ranges = [MAX_BOXES], [], None, None
    range_checks = 0
    def read(at, size):
        nonlocal seen
        token.checkpoint()
        require(at >= 0 and size >= 0)
        require(at + size <= max_size, 'validation_limit')
        require(at + size <= available, 'truncated')
        stream.seek(start + at)
        data = stream.read(size)
        require(len(data) == size, 'truncated')
        seen = max(seen, at + size)
        return data
    try:
        while True:
            token.checkpoint()
            budget[0] -= 1
            require(budget[0] >= 0, 'validation_limit')
            size, kind = struct.unpack('>I4s', read(position, 8))
            header = 8
            if size == 1:
                size, header = int.from_bytes(read(position + 8, 8), 'big'), 16
            require(size != 0, 'boundary_unknown')
            require(size >= header)
            end = position + size
            require(end <= max_size, 'validation_limit')
            require(end <= available, 'truncated')
            if kind in FRAGMENTED:
                raise Rejected('fragmented')
            if kind in ENCRYPTED:
                raise Rejected('encrypted')
            if kind in EXTERNAL:
                raise Rejected('external_reference')
            require(kind in {b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip'}, 'unsupported')
            if position == 0:
                require(kind == b'ftyp')
            elif kind == b'ftyp':
                raise Rejected('truncated')
            if kind == b'ftyp':
                require(8 <= size - header <= 4096 and (size - header) % 4 == 0)
                data = read(position + header, size - header)
                brands = {data[:4]} | {data[i:i + 4] for i in range(8, len(data), 4)}
                file_type = 'mov' if data[:4] == b'qt  ' else 'mp4'
                if data[:4] in AUDIO_BRANDS:
                    file_type, prefix = 'm4a', 'audio_'
                require(bool(brands & (BRANDS | AUDIO_BRANDS)), 'unsupported')
            elif kind == b'moov':
                require(movie is None)
                require(size - header <= MAX_METADATA, 'validation_limit')
                movie = Movie(read(position + header, size - header), token, budget)
                ranges = movie.parse()
                require(all(0 < a < b for a, b in ranges))
                require(all(b <= max_size for _, b in ranges), 'validation_limit')
            elif kind == b'mdat':
                require(size > header)
                mdats.append((position + header, end))
            seen = max(seen, end)
            position = end
            if ranges is not None:
                range_checks += len(ranges)
                require(range_checks <= 2000000, 'validation_limit')
                # A chunk behind this boundary must be wholly inside an mdat
                # payload, never a header, metadata box or cross-box interval.
                index = 0
                pending = False
                for a, b in ranges:
                    token.checkpoint()
                    while index < len(mdats) and mdats[index][1] <= a:
                        index += 1
                    contained = index < len(mdats) and mdats[index][0] <= a < b <= mdats[index][1]
                    if not contained:
                        require(a >= position)
                        pending = True
                if not pending:
                    require(bool(mdats))
                    details = {'check_version': 1, 'boundary_basis': 'last_required_box',
                               'tracks': len(movie.track_ids), 'samples': movie.samples,
                               'chunks': len(ranges), 'media_boxes': len(mdats),
                               'bitstream_decoded': False}
                    if movie.video_tracks == 0:
                        return VideoValidation(position, 'audio_structure_checked', 'm4a', {**details, 'format': 'm4a'})
                    return VideoValidation(position, 'video_structure_checked', 'mp4' if file_type == 'm4a' else file_type, details)
    except Rejected as error:
        return VideoValidation(min(available, max_size, max(8, seen)), prefix + error.reason, file_type)

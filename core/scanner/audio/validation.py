"""Static structure checks for WAV and MP3 audio; nothing is decoded or played.

WAV: a RIFF/WAVE file ends where its RIFF size says; the fmt and data chunks
must be present once, in order, inside it, with a self-consistent PCM, float,
A-law, mu-law or extensible format. Other RIFF forms (AVI, WebP) are not
candidates.

MP3: only files that start with an ID3v2 tag are searched for (bare MPEG frame
sync is far too common in arbitrary bytes). Frames are walked by their header
lengths with version, layer, sample rate and channel count held constant. MP3
has no file length field, so the walk is only accepted when the first frame's
Xing/Info or VBRI header records the audio byte count and the walk matches it
exactly; otherwise the boundary stays unknown and the candidate is not
exportable. A trailing ID3v1 tag is included in the range.
"""
from dataclasses import dataclass
import struct

MAX_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 10000
MAX_FRAMES = 500000
MAX_ID3_PADDING = 64 * 1024
AUDIO_TYPES = frozenset({'wav', 'mp3', 'm4a'})
KSDATAFORMAT_TAIL = b'\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71'
WAV_FORMATS = {1: 'pcm', 3: 'float', 6: 'alaw', 7: 'mulaw'}
MPEG1_RATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
MPEG2_RATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


class Rejected(Exception):
    def __init__(self, reason='invalid_structure'):
        self.reason = reason


class NotAudio(Exception):
    pass


def require(condition, reason='invalid_structure'):
    if not condition:
        raise Rejected(reason)


@dataclass(frozen=True)
class AudioValidation:
    size: int
    integrity: str
    file_type: str
    details: dict | None = None


class _Reader:
    def __init__(self, stream, start, available, cap, token):
        self.stream, self.start, self.available, self.cap, self.token = stream, start, available, cap, token

    def read(self, at, size):
        self.token.checkpoint()
        if at + size > self.available:
            raise Rejected('truncated')
        if at + size > self.cap:
            raise Rejected('validation_limit')
        self.stream.seek(self.start + at)
        data = self.stream.read(size)
        if len(data) != size:
            raise Rejected('truncated')
        return data


def validate(stream, start, source_size, kind, token, max_size=MAX_BYTES):
    available = max(0, source_size - start)
    reader = _Reader(stream, start, available, min(MAX_BYTES, max_size), token)
    try:
        if kind == 'wav':
            return _wav(reader)
        return _mp3(reader)
    except NotAudio:
        return None
    except Rejected as error:
        return AudioValidation(min(available, 12), 'audio_' + error.reason, kind)


# -- WAV ------------------------------------------------------------------------
def _wav(reader):
    try:
        head = reader.read(0, 12)
    except Rejected:
        raise NotAudio()
    if head[:4] != b'RIFF' or head[8:12] != b'WAVE':
        raise NotAudio()            # AVI, WebP and other RIFF forms.
    riff = struct.unpack_from('<I', head, 4)[0]
    if riff in (0, 0xFFFFFFFF):
        raise Rejected('boundary_unknown')   # Streaming writers that never filled in the size.
    require(riff >= 4 + 8 + 16 + 8)
    end = 8 + riff
    if end > reader.available:
        raise Rejected('truncated')
    if end > reader.cap:
        raise Rejected('validation_limit')
    position, chunks, fmt, data = 12, 0, None, None
    while position < end:
        reader.token.checkpoint()
        chunks += 1; require(chunks <= MAX_CHUNKS, 'validation_limit')
        require(position + 8 <= end)
        kind, length = struct.unpack('<4sI', reader.read(position, 8))
        body, after = position + 8, position + 8 + length
        require(after <= end)
        if kind == b'fmt ':
            require(fmt is None and data is None and length >= 16)
            fmt = _wav_format(reader.read(body, min(length, 64)), length)
        elif kind == b'data':
            require(fmt is not None and data is None and length > 0)
            data = length
        position = after + (length & 1)
    require(position in (end, end + 1) and fmt is not None and data is not None)
    # A final odd chunk's pad byte (or an odd RIFF size) extends the file by one byte.
    size = max(end + (riff & 1), position)
    if size > reader.available:
        raise Rejected('truncated')
    if size > reader.cap:
        raise Rejected('validation_limit')
    channels, rate, bits, align, encoding = fmt
    require(data % align == 0)
    details = {'check_version': 1, 'format': 'wav', 'encoding': encoding, 'channels': channels, 'sample_rate': rate,
               'bits_per_sample': bits, 'duration_seconds': round(data / (rate * align), 3),
               'boundary_basis': 'riff_size', 'decoded': False}
    return AudioValidation(size, 'audio_structure_checked', 'wav', details)


def _wav_format(raw, length):
    code, channels, rate, byte_rate, align, bits = struct.unpack_from('<HHIIHH', raw, 0)
    if code == 0xFFFE:
        require(length >= 40 and len(raw) >= 40, 'unsupported')
        extra, valid_bits, _mask = struct.unpack_from('<HHI', raw, 16)
        guid = raw[24:40]
        require(extra >= 22 and 0 < valid_bits <= bits and guid[2:] == KSDATAFORMAT_TAIL, 'unsupported')
        code = struct.unpack_from('<H', guid, 0)[0]
        require(code in (1, 3), 'unsupported')
    require(code in WAV_FORMATS, 'unsupported')
    encoding = WAV_FORMATS[code]
    allowed = {'pcm': (8, 16, 24, 32), 'float': (32, 64), 'alaw': (8,), 'mulaw': (8,)}[encoding]
    require(1 <= channels <= 32 and 1 <= rate <= 1_000_000 and bits in allowed)
    require(align == channels * bits // 8 and byte_rate == rate * align)
    return channels, rate, bits, align, encoding


# -- MP3 ------------------------------------------------------------------------
def _frame(header):
    """(version, sample_rate, mono, length, samples, protected) for a Layer III header, else None."""
    if len(header) < 4:
        return None
    h = struct.unpack('>I', header)[0]
    if (h >> 21) & 0x7FF != 0x7FF:
        return None
    version, layer, bitrate, rate_index = (h >> 19) & 3, (h >> 17) & 3, (h >> 12) & 0xF, (h >> 10) & 3
    if version == 1 or layer != 1 or bitrate in (0, 15) or rate_index == 3 or h & 3 == 2:
        return None
    rate = SAMPLE_RATES[version][rate_index]
    kbps = (MPEG1_RATES if version == 3 else MPEG2_RATES)[bitrate]
    padding = (h >> 9) & 1
    length = (144000 if version == 3 else 72000) * kbps // rate + padding
    return version, rate, ((h >> 6) & 3) == 3, length, 1152 if version == 3 else 576, not (h >> 16) & 1


def _counts(frame_bytes, version, mono, protected):
    """(frames, bytes) from a Xing/Info or VBRI header in the first frame; (None, None) if absent."""
    side = (17 if mono else 32) if version == 3 else (9 if mono else 17)
    at = 4 + (2 if protected else 0) + side
    if frame_bytes[at:at + 4] in (b'Xing', b'Info'):
        flags = struct.unpack_from('>I', frame_bytes, at + 4)[0]
        position, frames, total = at + 8, None, None
        if flags & 1:
            frames = struct.unpack_from('>I', frame_bytes, position)[0]; position += 4
        if flags & 2:
            total = struct.unpack_from('>I', frame_bytes, position)[0]
        return frames, total, 'xing_byte_count'
    if frame_bytes[36:40] == b'VBRI':
        total, frames = struct.unpack_from('>II', frame_bytes, 46)
        return frames, total, 'vbri_byte_count'
    return None, None, None


def _mp3(reader):
    try:
        tag = reader.read(0, 10)
    except Rejected:
        raise NotAudio()
    major, revision, flags = tag[3], tag[4], tag[5]
    raw = tag[6:10]
    if tag[:3] != b'ID3' or major not in (2, 3, 4) or revision == 0xFF or any(b & 0x80 for b in raw):
        raise NotAudio()
    require(flags & (0x0F if major == 4 else 0x1F) == 0)
    first = 10 + (raw[0] << 21 | raw[1] << 14 | raw[2] << 7 | raw[3]) + (10 if major == 4 and flags & 0x10 else 0)
    # Some writers pad between the tag and the first frame with zeros.
    padding = 0
    while True:
        probe = reader.read(first + padding, 4)
        if probe != b'\0\0\0\0':
            break
        padding += 4; require(padding <= MAX_ID3_PADDING)
    first += padding
    lead = _frame(reader.read(first, 4))
    require(lead is not None, 'unsupported')
    version, rate, mono, length, samples, protected = lead
    expected_frames, expected_bytes, basis = _counts(reader.read(first, min(length, 192)), version, mono, protected)
    position, frames = first, 0
    while True:
        reader.token.checkpoint()
        if position + 4 > reader.available or position + 4 > reader.cap:
            break
        header = reader.read(position, 4)
        info = _frame(header)
        if info is None or info[:3] != (version, rate, mono):
            break
        if position + info[3] > reader.available:
            raise Rejected('truncated')
        if position + info[3] > reader.cap:
            raise Rejected('validation_limit')
        frames += 1; require(frames <= MAX_FRAMES, 'validation_limit')
        position += info[3]
        if expected_bytes is not None and position - first >= expected_bytes:
            break
    require(frames >= 2)
    if expected_bytes is None:
        raise Rejected('boundary_unknown')
    if position - first < expected_bytes:
        raise Rejected('truncated' if position + 4 > reader.available else 'invalid_structure')
    require(position - first == expected_bytes)
    require(expected_frames is None or expected_frames in (frames, frames - 1))
    end = position
    if end + 128 <= reader.available and end + 128 <= reader.cap and reader.read(end, 3) == b'TAG':
        end += 128
    details = {'check_version': 1, 'format': 'mp3', 'frames': frames, 'sample_rate': rate, 'channels': 1 if mono else 2,
               'duration_seconds': round(frames * samples / rate, 3), 'boundary_basis': basis, 'decoded': False}
    return AudioValidation(end, 'audio_structure_checked', 'mp3', details)

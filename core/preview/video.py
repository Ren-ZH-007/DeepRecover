"""Read a verified local snapshot and admit self-contained MP4/MOV previews.

These checks are playback admission limits, not file recovery or a complete
container/bitstream validator. No locator is ever passed to the media backend.
"""
from dataclasses import dataclass
import struct

from core.candidate_io import CandidateAccessError, candidate_input, read_candidate

MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_DURATION_MS = 10 * 60 * 1000
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
MAX_BOXES = 10000


@dataclass(frozen=True)
class VideoPreview:
    data: bytes
    width: int
    height: int
    duration_ms: int


def _reject(message='视频容器不完整或不在本版预览支持范围内。'):
    raise CandidateAccessError(message)


def inspect_video(data, token):
    """Admit nonfragmented, self-contained H.264 MP4/MOV with bounded headers."""
    if not isinstance(data, bytes) or not 16 <= len(data) <= MAX_VIDEO_BYTES:
        _reject('视频输入为空、损坏或超过 64 MiB 预览上限。')
    view = memoryview(data)
    budget = MAX_BOXES

    def boxes(start, end):
        nonlocal budget
        result = []
        while start < end:
            token.checkpoint()
            budget -= 1
            if budget < 0 or end - start < 8:
                _reject()
            size, kind = struct.unpack_from('>I4s', view, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    _reject()
                size = struct.unpack_from('>Q', view, start + 8)[0]
                header = 16
            if size == 0:
                if kind != b'mdat':
                    _reject()
                size = end - start
            if size < header or start + size > end:
                _reject()
            result.append((kind, start + header, start + size))
            start += size
        return result

    def one(items, kind):
        found = [item for item in items if item[0] == kind]
        if len(found) != 1:
            _reject()
        return found[0][1:]

    def allowed(items, kinds):
        if any(item[0] not in kinds for item in items):
            _reject('视频包含本版不支持的分段、引用、加密或容器结构。')

    def clock(start, end):
        if end - start < 20:
            _reject()
        version = view[start]
        if version == 0:
            scale, duration = struct.unpack_from('>II', view, start + 12)
        elif version == 1 and end - start >= 32:
            scale = struct.unpack_from('>I', view, start + 20)[0]
            duration = struct.unpack_from('>Q', view, start + 24)[0]
        else:
            _reject()
        if not scale or not duration or duration * 1000 > scale * MAX_DURATION_MS:
            _reject('视频时长未知、无效或超过 10 分钟预览上限。')
        return scale, max(1, duration * 1000 // scale)

    def passive_metadata(items):
        # Do not let an otherwise valid track hide active reference boxes in
        # user metadata. Only known passive tags and flat item values pass.
        for kind, start, end in items:
            if kind == b'udta':
                tags = boxes(start, end)
                for tag, a, b in tags:
                    if tag == b'meta':
                        if b - a < 4 or bytes(view[a:a + 4]) != b'\0\0\0\0':
                            _reject()
                        metadata = boxes(a + 4, b)
                        allowed(metadata, {b'hdlr', b'keys', b'ilst', b'free'})
                        for entry, x, y in metadata:
                            if entry == b'ilst':
                                for item, c, d in boxes(x, y):
                                    if item in {b'dref', b'rmra', b'rmda', b'rdrf', b'url ', b'urn '}:
                                        _reject('视频元数据包含外部媒体引用，已拒绝预览。')
                                    allowed(boxes(c, d), {b'data', b'mean', b'name'})
                    elif not (tag.startswith(b'\xa9') or tag in {b'name', b'cprt', b'chpl', b'free', b'WLOC'}):
                        _reject('视频元数据包含本版不支持的引用或结构。')
            elif kind == b'edts':
                edits = boxes(start, end)
                allowed(edits, {b'elst'})
                for _, a, b in edits:
                    if b - a < 8 or view[a] not in (0, 1):
                        _reject()
                    count = struct.unpack_from('>I', view, a + 4)[0]
                    if count > MAX_BOXES or b - a != 8 + count * (12 if view[a] == 0 else 20):
                        _reject()

    root = boxes(0, len(data))
    allowed(root, {b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip'})
    ftyp_start, ftyp_end = one(root, b'ftyp')
    if root[0][0] != b'ftyp' or not 8 <= ftyp_end - ftyp_start <= 4096 or (ftyp_end - ftyp_start) % 4:
        _reject()
    brands = {bytes(view[ftyp_start:ftyp_start + 4])}
    brands.update(bytes(view[i:i + 4]) for i in range(ftyp_start + 8, ftyp_end, 4))
    if not brands.intersection({b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'qt  ', b'M4V '}):
        _reject('当前仅支持自包含的 MP4/MOV 视频，不支持播放列表。')
    if not any(kind == b'mdat' and end > start for kind, start, end in root):
        _reject()
    moov = boxes(*one(root, b'moov'))
    allowed(moov, {b'mvhd', b'trak', b'udta', b'free'})
    passive_metadata(moov)
    _, duration_ms = clock(*one(moov, b'mvhd'))
    tracks = [item for item in moov if item[0] == b'trak']
    if not 1 <= len(tracks) <= 4:
        _reject()
    video_dimensions = []
    for _, start, end in tracks:
        track = boxes(start, end)
        allowed(track, {b'tkhd', b'edts', b'mdia', b'udta', b'free'})
        passive_metadata(track)
        media = boxes(*one(track, b'mdia'))
        allowed(media, {b'mdhd', b'hdlr', b'minf', b'elng', b'free'})
        scale, _ = clock(*one(media, b'mdhd'))
        handler_start, handler_end = one(media, b'hdlr')
        if handler_end - handler_start < 12:
            _reject()
        handler = bytes(view[handler_start + 8:handler_start + 12])
        if handler not in (b'vide', b'soun'):
            _reject('当前不预览字幕、外部引用或其他媒体轨道。')
        media_info = boxes(*one(media, b'minf'))
        allowed(media_info, {b'vmhd', b'smhd', b'hdlr', b'dinf', b'stbl', b'free'})
        data_info = boxes(*one(media_info, b'dinf'))
        allowed(data_info, {b'dref'})
        ref_start, ref_end = one(data_info, b'dref')
        if ref_end - ref_start < 8 or bytes(view[ref_start:ref_start + 4]) != b'\0\0\0\0':
            _reject()
        references = boxes(ref_start + 8, ref_end)
        if struct.unpack_from('>I', view, ref_start + 4)[0] != 1 or len(references) != 1:
            _reject('视频包含外部媒体引用，已拒绝预览。')
        kind, ref_start, ref_end = references[0]
        if kind != b'url ' or bytes(view[ref_start:ref_end]) != b'\0\0\0\1':
            _reject('视频包含外部媒体引用，已拒绝预览。')
        sample_table = boxes(*one(media_info, b'stbl'))
        allowed(sample_table, {b'stsd', b'stts', b'stss', b'ctts', b'stsc', b'stsz', b'stco', b'co64', b'sdtp', b'sgpd', b'sbgp', b'free'})
        sample_start, sample_end = one(sample_table, b'stsd')
        if sample_end - sample_start < 8 or bytes(view[sample_start:sample_start + 4]) != b'\0\0\0\0':
            _reject()
        samples = boxes(sample_start + 8, sample_end)
        if len(samples) != 1 or struct.unpack_from('>I', view, sample_start + 4)[0] != 1:
            _reject()
        codec, sample_start, sample_end = samples[0]
        if sample_end - sample_start < 8 or struct.unpack_from('>H', view, sample_start + 6)[0] != 1:
            _reject('视频媒体引用无效。')
        if handler == b'vide':
            if codec != b'avc1' or sample_end - sample_start < 78:
                _reject('首版视频预览仅支持 H.264 编码的 MP4/MOV。')
            width, height = struct.unpack_from('>HH', view, sample_start + 24)
            if not 0 < width <= MAX_WIDTH or not 0 < height <= MAX_HEIGHT:
                _reject('视频尺寸无效或超过 1920×1080 预览上限。')
            extensions = boxes(sample_start + 78, sample_end)
            allowed(extensions, {b'avcC', b'pasp', b'btrt', b'colr', b'clap', b'fiel', b'free'})
            config_start, config_end = one(extensions, b'avcC')
            if config_end - config_start < 7 or view[config_start] != 1:
                _reject()
            video_dimensions.append((width, height))
        else:
            if codec != b'mp4a' or sample_end - sample_start < 28 or bytes(view[sample_start + 8:sample_start + 10]) != b'\0\0':
                _reject('首版仅接受无音轨或受支持 AAC 布局的视频，并以静音方式预览。')
            audio_extensions = boxes(sample_start + 28, sample_end)
            allowed(audio_extensions, {b'esds', b'btrt', b'free'})
            esds_start, esds_end = one(audio_extensions, b'esds')
            if esds_end - esds_start < 9 or bytes(view[esds_start:esds_start + 4]) != b'\0\0\0\0' or view[esds_start + 4] != 3:
                _reject()
            position, descriptor_size = esds_start + 5, 0
            for _ in range(4):
                if position >= esds_end:
                    _reject()
                byte = view[position]; position += 1
                descriptor_size = (descriptor_size << 7) | (byte & 127)
                if not byte & 128:
                    break
            else:
                _reject()
            if descriptor_size < 3 or position + descriptor_size != esds_end:
                _reject()
            if view[position + 2] & 0xe0:
                _reject('音轨包含外部引用或未支持的依赖，已拒绝预览。')
        timing_start, timing_end = one(sample_table, b'stts')
        if timing_end - timing_start < 8:
            _reject()
        entries = struct.unpack_from('>I', view, timing_start + 4)[0]
        if entries > MAX_BOXES or timing_end - timing_start != 8 + 8 * entries:
            _reject()
        sample_count = 0
        for i in range(entries):
            token.checkpoint()
            count, delta = struct.unpack_from('>II', view, timing_start + 8 + 8 * i)
            sample_count += count
            if not count or not delta or (handler == b'vide' and (delta * 60 < scale or sample_count > 36000)):
                _reject('视频采样参数超出本版预览支持范围。')
    if len(video_dimensions) != 1:
        _reject('当前仅支持单视频轨道的 MP4/MOV。')
    token.checkpoint()
    return VideoPreview(data, *video_dimensions[0], duration_ms)


def prepare_video(repository, candidate_id, data_dir, token):
    item = candidate_input(repository, candidate_id, data_dir)
    if str(item.candidate.get('file_type', '')).lower() not in ('mp4', 'mov'):
        _reject('首版视频预览仅支持 MP4/MOV，不支持其他格式或播放列表。')
    if item.size > MAX_VIDEO_BYTES:
        _reject('视频输入超过 64 MiB 预览上限，请导出后使用本机播放器查看。')
    token.checkpoint()
    data = read_candidate(repository, candidate_id, data_dir, token, max_bytes=MAX_VIDEO_BYTES)
    return inspect_video(data, token)

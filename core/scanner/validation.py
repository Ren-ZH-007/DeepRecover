"""Shared signature discovery and format-specific validation policy."""
from core.scanner.image.validation import validate as validate_image
from core.scanner.video.validation import validate as validate_video

SIGNATURES = ((b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpg'), (b'ftyp', 'mp4'))
CHECKED = frozenset({'structure_checked', 'video_structure_checked'})
INVALID = frozenset({'invalid_structure', 'video_invalid_structure'})


def checked_signature(integrity, file_type):
    return (integrity == 'structure_checked' and file_type in ('png', 'jpg', 'jpeg')) or (
        integrity == 'video_structure_checked' and file_type in ('mp4', 'mov'))


def find_signature(data, cursor):
    matches = []
    for signature, kind in SIGNATURES:
        # ISO BMFF starts four bytes before the ftyp box type. Keeping seven
        # overlap bytes is sufficient for both this header and PNG's signature.
        shift = 4 if kind == 'mp4' else 0
        at = data.find(signature, cursor + shift)
        if at >= shift:
            matches.append((at - shift, kind))
    return min(matches) if matches else None


def validate(stream, start, source_size, file_type, token, max_size, max_decoded):
    if file_type == 'mp4':
        return validate_video(stream, start, source_size, token, max_size)
    return validate_image(stream, start, source_size, file_type, token, max_size, max_decoded)

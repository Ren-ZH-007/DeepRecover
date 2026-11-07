"""Shared signature discovery and format-specific validation policy."""
from core.scanner.image.validation import validate as validate_image
from core.scanner.video.validation import validate as validate_video
from core.scanner.pdf.validation import validate as validate_pdf
from core.scanner.office.validation import OFFICE_TYPES, OLE_MAGIC, ZIP_LOCAL, validate as validate_office
from core.scanner.audio.validation import AUDIO_TYPES, validate as validate_audio

SIGNATURES = ((b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpg'), (b'ftyp', 'mp4'), (b'%PDF-', 'pdf'),
              (ZIP_LOCAL, 'ooxml'), (OLE_MAGIC, 'ole'), (b'RIFF', 'wav'), (b'ID3', 'mp3'))
CHECKED = frozenset({'structure_checked', 'video_structure_checked', 'pdf_structure_checked', 'office_structure_checked',
                     'audio_structure_checked'})
INVALID = frozenset({'invalid_structure', 'video_invalid_structure', 'pdf_invalid_structure', 'office_invalid_structure',
                     'audio_invalid_structure'})


def checked_signature(integrity, file_type):
    return (integrity == 'structure_checked' and file_type in ('png', 'jpg', 'jpeg')) or (
        integrity == 'video_structure_checked' and file_type in ('mp4', 'mov')) or (
        integrity == 'pdf_structure_checked' and file_type == 'pdf') or (
        integrity == 'office_structure_checked' and file_type in OFFICE_TYPES) or (
        integrity == 'audio_structure_checked' and file_type in AUDIO_TYPES)


def find_signature(data, cursor, cache=None):
    """Earliest signature at or after ``cursor``.

    ``cache`` (a fresh dict per ``data`` buffer) remembers each signature's
    next occurrence, so a buffer with many matches of one signature (ZIP local
    headers) is searched once per signature instead of once per match.
    """
    matches = []
    for signature, kind in SIGNATURES:
        # ISO BMFF starts four bytes before the ftyp box type. Keeping seven
        # overlap bytes is sufficient for both this header and PNG's signature.
        shift = 4 if kind == 'mp4' else 0
        at = None if cache is None else cache.get(kind)
        # A cached hit stays the first one from any later cursor up to it; -1 means none to the end.
        if at is None or (at != -1 and at < cursor + shift):
            at = data.find(signature, cursor + shift)
            if cache is not None:
                cache[kind] = at
        if at >= shift:
            matches.append((at - shift, kind))
    return min(matches) if matches else None


def validate(stream, start, source_size, file_type, token, max_size, max_decoded):
    """Format result, or None when the signature bytes are not a candidate of that format."""
    if file_type in ('wav', 'mp3'):
        return validate_audio(stream, start, source_size, file_type, token, max_size)
    if file_type in ('ooxml', 'ole'):
        return validate_office(stream, start, source_size, file_type, token, max_size, max_decoded)
    if file_type == 'pdf':
        return validate_pdf(stream, start, source_size, token, max_size)
    if file_type == 'mp4':
        return validate_video(stream, start, source_size, token, max_size)
    return validate_image(stream, start, source_size, file_type, token, max_size, max_decoded)

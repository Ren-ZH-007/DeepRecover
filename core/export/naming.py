"""Output names for verified exports.

Candidates whose name and path come from real file-system or backup metadata
keep that name and folder layout. Signature candidates only have a synthetic
offset name, so they keep an opaque random name. Every component is cleaned
for Windows, macOS and Linux, and the result never leaves the batch folder.
"""
import os
from pathlib import Path
import re
import unicodedata
from uuid import uuid4

# Sources whose ``name``/``original_path`` come from metadata, not from us.
NAMED_SOURCES = frozenset({'android_file', 'ios_backup', 'computer_deleted'})
MAX_COMPONENT_BYTES = 150
MAX_DEPTH = 8
MAX_RELATIVE_CHARS = 200
MAX_SUFFIX = 9999
RESERVED_AT_ROOT = frozenset({'manifest.json'})

_INVALID = re.compile(r'[<>:"|?*\\/\x00-\x1f\x7f]')
_DRIVE = re.compile(r'^[A-Za-z]:$')
_SYNTHETIC = re.compile(r'^\[.*\]$')   # e.g. "[$MFT record 42]" labels, not folders
_EXTENSION = re.compile(r'[a-z0-9]{1,10}')
_WINDOWS_DEVICES = frozenset({'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                              *(f'LPT{i}' for i in range(1, 10))})


class NamingError(OSError):
    pass


def _truncate(text, limit):
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text
    return data[:limit].decode('utf-8', 'ignore')


def clean_component(text):
    """Return a safe single path component, or None when nothing usable is left."""
    text = unicodedata.normalize('NFC', str(text)).strip(' ')
    if _DRIVE.match(text):
        return None
    text = _INVALID.sub('_', text).strip(' ').rstrip('. ')
    if text in ('', '.', '..') or _SYNTHETIC.match(text):
        return None
    if text.split('.', 1)[0].rstrip(' ').upper() in _WINDOWS_DEVICES:
        text = '_' + text
    if len(text.encode('utf-8')) > MAX_COMPONENT_BYTES:
        stem, dot, suffix = text.rpartition('.')
        if dot and 0 < len(suffix) <= 10 and stem:
            text = _truncate(stem, MAX_COMPONENT_BYTES - len(suffix) - 1).rstrip('. ') + '.' + suffix
        else:
            text = _truncate(text, MAX_COMPONENT_BYTES).rstrip('. ')
    return text or None


def _split(value):
    return [part for part in re.split(r'[\\/]+', str(value or '')) if part]


def relative_parts(candidate):
    """Cleaned (folders..., file name) from metadata, or None for opaque names."""
    if candidate.get('source') not in NAMED_SOURCES:
        return None
    name = clean_component(candidate.get('name', ''))
    if name is None:
        return None
    extension = str(candidate.get('file_type', '')).lower()
    if '.' not in name.lstrip('.') and _EXTENSION.fullmatch(extension) and extension != 'bin':
        name = clean_component(f'{name}.{extension}') or name
    # Every metadata source stores the file itself as the last path component.
    folders = [part for part in map(clean_component, _split(candidate.get('original_path'))[:-1])
               if part is not None][-MAX_DEPTH:]
    while folders and len('/'.join((*folders, name))) > MAX_RELATIVE_CHARS:
        folders.pop(0)
    return (*folders, name)


def _numbered(name, number):
    stem, dot, suffix = name.rpartition('.')
    if not dot or not stem:
        stem, suffix = name, ''
    label = f' ({number})'
    stem = _truncate(stem, MAX_COMPONENT_BYTES - len(label) - (len(suffix) + 1 if suffix else 0)).rstrip('. ')
    return f'{stem}{label}.{suffix}' if suffix else f'{stem}{label}'


def _taken(directory, name, root):
    return os.path.lexists(directory / name) or (directory == root and name.casefold() in RESERVED_AT_ROOT)


def _ensure_folder(root, folders):
    current = root
    for part in folders:
        current = current / part
        try:
            os.mkdir(current)
        except FileExistsError:
            pass
        if current.is_symlink() or not current.is_dir():
            raise NamingError('导出目录中存在同名文件，无法创建原始目录。')
    if not current.resolve().is_relative_to(root):
        raise NamingError('导出路径超出批次目录。')
    return current


def choose_target(batch_dir, candidate):
    """Pick a not-yet-existing output path inside ``batch_dir`` for ``candidate``.

    The caller still opens the file exclusively; this only avoids predictable
    collisions (including case-only ones on case-insensitive volumes).
    """
    root = Path(batch_dir).resolve()
    parts = relative_parts(candidate)
    if parts is None:
        extension = str(candidate.get('file_type', '')).lower()
        extension = extension if _EXTENSION.fullmatch(extension) else 'bin'
        return root / f'{uuid4().hex}.{extension}'
    directory = _ensure_folder(root, parts[:-1])
    name = parts[-1]
    if not _taken(directory, name, root):
        return directory / name
    for number in range(2, MAX_SUFFIX + 1):
        option = _numbered(name, number)
        if not _taken(directory, option, root):
            return directory / option
    raise NamingError('同名文件过多，无法生成不冲突的导出名称。')

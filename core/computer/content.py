"""Bounded logical-file reads; physical extents retain their logical order."""
import hashlib

MAX_FILE_SIZE = 8 * 1024 ** 3
MAX_EXTENTS = 8192


def checked_extents(extents, size, source_size):
    if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
        raise ValueError('电脑候选超过 8 GiB 单文件处理上限或大小无效。')
    if not isinstance(extents, (list, tuple)) or len(extents) > MAX_EXTENTS:
        raise ValueError('候选文件布局缺失或碎片数量超过上限。')
    result, total = [], 0
    for extent in extents:
        if not isinstance(extent, (list, tuple)) or len(extent) != 2:
            raise ValueError('候选文件范围格式无效。')
        offset, length = extent
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 or offset + length > source_size:
            raise ValueError('候选文件范围超出绑定来源。')
        result.append((offset, length)); total += length
    if total != size:
        raise ValueError('文件布局长度与候选大小不符。')
    ordered = sorted(result)
    if any(a + b > c for (a, b), (c, _) in zip(ordered, ordered[1:])):
        raise ValueError('候选文件的物理范围重叠。')
    return tuple(result)


def extent_chunks(reader, extents, token):
    for offset, length in extents:
        while length:
            token.checkpoint()
            amount = min(length, 1024 * 1024)
            yield reader.read_at(offset, amount)
            offset += amount; length -= amount
    token.checkpoint()


def extent_digest(reader, extents, token):
    digest = hashlib.sha256()
    for chunk in extent_chunks(reader, extents, token): digest.update(chunk)
    return digest.hexdigest()

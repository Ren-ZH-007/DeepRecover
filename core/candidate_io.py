"""Read-only, source-bound access shared by preview and export."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat

from core.scanner.validation import checked_signature


class CandidateAccessError(RuntimeError):
    pass


def snapshot(info):
    result = {key: getattr(info, 'st_' + key) for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}
    # CPython 3.12 Windows path stat exposes creation time as ctime, while
    # descriptor fstat exposes ChangeTime. Both expose the same birthtime.
    # Normalize that field while retaining volume, full file ID, size and mtime.
    if os.name == 'nt':
        result['ctime_ns'] = info.st_birthtime_ns
    return result


def range_chunks(stream, offset, size, token):
    stream.seek(offset)
    remaining = size
    while remaining:
        token.checkpoint()
        data = stream.read(min(1024 * 1024, remaining))
        if not data:
            raise CandidateAccessError('源文件已截断，无法读取记录的完整范围。')
        remaining -= len(data)
        yield data
    token.checkpoint()


def range_digest(stream, offset, size, token):
    digest = hashlib.sha256()
    for data in range_chunks(stream, offset, size, token):
        digest.update(data)
    return digest.hexdigest()


@dataclass(frozen=True)
class CandidateInput:
    candidate: dict
    source: dict
    path: Path
    offset: int
    size: int
    expected_hash: str | None
    expected_stat: dict | None
    extents: tuple | None = None

    def open_source(self):
        if self.extents is not None:
            from core.computer.io import DiskReader
            return DiskReader(self.path, self.expected_stat['kind'])
        return self.path.open('rb')

    def chunks(self, stream, token):
        from core.computer.content import extent_chunks
        return extent_chunks(stream, self.extents, token)

    def check(self, stream, original=None):
        if self.extents is not None:
            from core.computer.content import checked_extents
            from core.computer.io import DiskError
            try:
                current = stream.check(self.expected_stat)
                checked_extents(self.extents, self.size, stream.size)
                if original is not None and current != original:
                    raise CandidateAccessError('电脑来源在读取期间变化。')
                return current
            except (DiskError, ValueError, OSError) as error:
                raise CandidateAccessError(str(error)) from error
        info = os.fstat(stream.fileno())
        current = snapshot(info)
        if not stat.S_ISREG(info.st_mode) or snapshot(self.path.stat()) != current:
            raise CandidateAccessError('来源文件身份已变化，请重新采集或索引。')
        if self.expected_stat and current != self.expected_stat:
            raise CandidateAccessError('来源文件自记录后已变化，请重新采集或索引。')
        if original is not None and current != original:
            raise CandidateAccessError('来源文件在读取期间变化，操作已停止。')
        if self.offset + self.size > info.st_size or (self.candidate['source'] != 'signature_scan' and self.size != info.st_size):
            raise CandidateAccessError('来源文件大小与记录不符。')
        return current

    def verify_hash(self, digest):
        if self.expected_hash and digest != self.expected_hash:
            raise CandidateAccessError('来源 SHA-256 与记录不符，请重新采集或扫描。')


def candidate_input(repository, candidate_id, data_dir):
    candidate = repository.candidate(candidate_id)
    if candidate is None:
        raise CandidateAccessError('候选记录不存在。')
    task = repository.task(candidate['task_id'])
    if task['status'] in ('running', 'queued'):
        raise CandidateAccessError('请等待来源任务结束后再预览或导出。')
    source = repository.source(task['source_id']) if task['source_id'] else None
    if not source or candidate.get('source_id') != source['id']:
        raise CandidateAccessError('历史记录缺少来源绑定，请重新采集或索引。')
    kind = candidate['source']
    size, offset = candidate['size'], 0
    if not isinstance(size, int) or size < 0 or candidate['integrity'] == 'missing':
        raise CandidateAccessError('候选文件缺失或大小无效。')
    expected_hash = candidate.get('sha256')
    if expected_hash is not None and not re.fullmatch('[0-9a-f]{64}', expected_hash):
        raise CandidateAccessError('候选摘要格式无效。')
    expected_stat = candidate.get('source_stat')
    if kind in ('computer_deleted', 'computer_signature') and source['kind'] == 'computer':
        from core.computer.content import checked_extents
        from core.computer.service import check_access, ComputerError
        if candidate['status'] != 'candidate' or not expected_hash or not isinstance(expected_stat, dict):
            raise CandidateAccessError('电脑候选缺少完整布局或来源校验信息，请重新扫描。')
        if kind == 'computer_deleted':
            checked = (candidate['integrity'], candidate.get('deletion_state')) in (
                ('metadata_checked', 'deleted_record'), ('snapshot_checked', 'snapshot_record'))
        else:
            checked = checked_signature(candidate['integrity'], candidate['file_type'])
        if not checked:
            raise CandidateAccessError('此电脑候选的记录或布局尚未验证，不能导出为完整文件。')
        if expected_stat.get('kind') not in ('image', 'device') or type(expected_stat.get('size')) is not int:
            raise CandidateAccessError('电脑来源身份格式无效。')
        try:
            check_access(repository, [candidate_id], data_dir)
            extents = checked_extents(candidate.get('extents'), size, expected_stat['size'])
        except (ValueError, ComputerError, RuntimeError) as error:
            raise CandidateAccessError(str(error)) from error
        path = Path(source['locator'])
        return CandidateInput(candidate, source, path, extents[0][0] if extents else 0,
                              size, expected_hash, expected_stat, extents)
    elif kind == 'signature_scan' and source['kind'] == 'image':
        if not checked_signature(candidate['integrity'], candidate['file_type']) or candidate['status'] != 'candidate':
            raise CandidateAccessError('只允许预览或导出结构检查通过的扫描候选。')
        if not expected_stat or not expected_hash:
            raise CandidateAccessError('旧扫描缺少来源校验信息，请重新扫描。')
        path = Path(source['locator']).resolve()
        offset = candidate.get('offset')
        if not isinstance(offset, int) or offset < 0 or size <= 0:
            raise CandidateAccessError('扫描范围无效。')
    elif kind == 'android_file' and source['kind'] == 'android':
        if not expected_hash:
            raise CandidateAccessError('采集记录缺少副本摘要，请重新采集。')
        path = Path(candidate['local_path']).resolve()
        if not path.is_relative_to((Path(data_dir) / 'Acquisitions').resolve()):
            raise CandidateAccessError('采集文件位置已超出工作目录。')
    elif kind == 'ios_backup' and source['kind'] == 'ios_backup':
        if not expected_stat:
            raise CandidateAccessError('旧备份索引缺少来源校验信息，请重新读取索引。')
        path = Path(candidate['local_path']).resolve()
        if not path.is_relative_to(Path(source['locator']).resolve()):
            raise CandidateAccessError('备份文件路径已超出源备份。')
    else:
        raise CandidateAccessError('不支持此来源类型的预览或导出。')
    if not path.is_file():
        raise CandidateAccessError('本地来源文件不存在。')
    return CandidateInput(candidate, source, path, offset, size, expected_hash, expected_stat)


def read_candidate(repository, candidate_id, data_dir, token, max_bytes=32 * 1024 * 1024):
    item = candidate_input(repository, candidate_id, data_dir)
    if item.size > max_bytes:
        raise CandidateAccessError('图片输入超过预览大小上限。')
    try:
        with item.open_source() as stream:
            original = item.check(stream)
            data = b''.join(item.chunks(stream, token) if item.extents is not None else range_chunks(stream, item.offset, item.size, token))
            item.verify_hash(hashlib.sha256(data).hexdigest())
            item.check(stream, original)
            return data
    except CandidateAccessError:
        raise
    except (OSError, RuntimeError) as error:
        raise CandidateAccessError('无法只读访问本地来源文件。') from error

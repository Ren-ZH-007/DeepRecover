from pathlib import Path
import os
import stat
from time import monotonic

from core.scanner.validation import validate, find_signature, CHECKED, INVALID, SIGNATURES
from storage.models.candidate import Candidate, CandidateStatus
from core.candidate_io import snapshot, range_digest
from core.tasks import ScanCheckpoint


class ScanError(RuntimeError):
    pass


class SignatureScanner:
    """Sequential byte search with bounded validation and no source writes.

    Valid containers are skipped as a whole; malformed/truncated signatures
    do not hide subsequent signatures. No files are extracted by this scanner.
    """
    def __init__(self, block_size=1024 * 1024, max_file_size=64 * 1024 * 1024,
                 max_decoded=256 * 1024 * 1024, max_candidates=10000):
        if block_size < 8 or max_file_size < 8 or max_decoded < 1 or max_candidates < 1:
            raise ValueError('Invalid scan limits')
        self.block_size, self.max_file_size = block_size, max_file_size
        self.max_decoded, self.max_candidates = max_decoded, max_candidates

    @staticmethod
    def fingerprint(info):
        return snapshot(info)

    def scan(self, source, token, *, start=0, start_count=0, expected=None, checkpoint_every=None):
        """Yield candidates from ``start``; ``checkpoint_every`` seconds also yields ScanCheckpoint.

        Resuming at a checkpoint (or right after the last stored candidate)
        with ``start_count`` stored candidates yields exactly the candidates an
        uninterrupted scan would have produced from there. ``expected`` is the
        source identity recorded by the original run.
        """
        if type(start) is not int or type(start_count) is not int or start < 0 or start_count < 0:
            raise ValueError('Invalid resume position')
        source = Path(source).resolve()
        token.checkpoint()
        try:
            if not source.is_file():
                raise ScanError('请选择本地普通文件或镜像文件，不支持目录或原始块设备。')
            with source.open('rb') as search, source.open('rb') as container:
                original = os.fstat(search.fileno())
                if not stat.S_ISREG(original.st_mode) or self.fingerprint(original) != self.fingerprint(os.fstat(container.fileno())):
                    raise ScanError('源文件不可用或已变化，请重新选择稳定的本地文件。')
                if expected is not None and snapshot(original) != expected:
                    raise ScanError('源文件已变化，不能从断点继续；请重新扫描。')
                total = original.st_size
                if start > total:
                    raise ScanError('断点超出源文件长度，不能继续；请重新扫描。')
                position = skip = start
                count = start_count
                carry = b''
                identity, last_mark = snapshot(original), monotonic()
                mark = lambda at: ScanCheckpoint(at, count, total, identity, {'scanner': 'signature'})
                token.report_progress(min(start, max(total - 1, 0)), total, count)
                if checkpoint_every is not None:
                    yield mark(start)
                while position < total:
                    token.checkpoint()
                    search.seek(position)
                    block = search.read(min(self.block_size, total - position))
                    if not block:
                        raise ScanError('源文件在扫描期间发生变化，结果不完整。')
                    data = carry + block
                    base = position - len(carry)
                    cursor = max(0, skip - base)
                    while cursor < len(data):
                        token.checkpoint()
                        match = find_signature(data, cursor)
                        if match is None:
                            break
                        at, kind = match
                        offset = base + at
                        if count >= self.max_candidates:
                            raise ScanError('候选数量达到上限，扫描未完成；请缩小输入范围。')
                        validation = validate(container, offset, total, kind, token, self.max_file_size, self.max_decoded)
                        count += 1
                        token.report_progress(offset, total, count)
                        ready = validation.integrity in CHECKED
                        kind = getattr(validation, 'file_type', kind)
                        digest = range_digest(container, offset, validation.size, token) if ready else None
                        following = offset + (validation.size if ready else 1)
                        yield Candidate(source='signature_scan', name=f'offset_{offset:016x}.{kind}',
                            file_type=kind, size=validation.size, original_path=str(source), offset=offset,
                            integrity=validation.integrity, structure_details=getattr(validation, 'details', None),
                            sha256=digest, source_stat=snapshot(original),
                            status=CandidateStatus.CANDIDATE if ready else (
                                CandidateStatus.FAILED if validation.integrity in INVALID else CandidateStatus.PARTIAL),
                            scan_next=following)
                        skip = following
                        cursor = skip - base
                    position += len(block)
                    if skip > position:
                        position = skip
                        carry = b''
                    else:
                        carry = data[-7:]
                    # Keep the overlap unreported until the next search so
                    # offsets straddling blocks never make progress go back.
                    # Completion also requires the final source-identity check.
                    searched = max(skip, position - len(carry))
                    token.report_progress(min(searched, total - 1), total, count)
                    if checkpoint_every is not None and monotonic() - last_mark >= checkpoint_every:
                        last_mark = monotonic()
                        yield mark(min(searched, total))
                if self.fingerprint(original) != self.fingerprint(os.fstat(search.fileno())) or self.fingerprint(original) != self.fingerprint(source.stat()):
                    raise ScanError('源文件在扫描期间发生变化，结果不可视为完整。')
                token.checkpoint()
                token.report_progress(total, total, count)
        except OSError as error:
            raise ScanError('无法只读访问源文件，请检查路径和权限。') from error

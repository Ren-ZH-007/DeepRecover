from pathlib import Path
import os
import stat

from core.scanner.image.validation import validate
from storage.models.candidate import Candidate, CandidateStatus
from core.candidate_io import snapshot, range_digest


class ScanError(RuntimeError):
    pass


SIGNATURES = ((b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpg'))


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
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def scan(self, source, token):
        source = Path(source).resolve()
        token.checkpoint()
        try:
            if not source.is_file():
                raise ScanError('请选择本地普通文件或镜像文件，不支持目录或原始块设备。')
            with source.open('rb') as search, source.open('rb') as container:
                original = os.fstat(search.fileno())
                if not stat.S_ISREG(original.st_mode) or self.fingerprint(original) != self.fingerprint(os.fstat(container.fileno())):
                    raise ScanError('源文件不可用或已变化，请重新选择稳定的本地文件。')
                total = original.st_size
                position = skip = count = 0
                carry = b''
                token.report_progress(0, total, count)
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
                        matches = [(data.find(signature, cursor), kind) for signature, kind in SIGNATURES]
                        matches = [(at, kind) for at, kind in matches if at >= 0]
                        if not matches:
                            break
                        at, kind = min(matches)
                        offset = base + at
                        if count >= self.max_candidates:
                            raise ScanError('候选数量达到上限，扫描未完成；请缩小输入范围。')
                        validation = validate(container, offset, total, kind, token, self.max_file_size, self.max_decoded)
                        count += 1
                        token.report_progress(offset, total, count)
                        partial = validation.integrity in ('truncated', 'validation_limit')
                        digest = range_digest(container, offset, validation.size, token) if validation.integrity == 'structure_checked' else None
                        yield Candidate(source='signature_scan', name=f'offset_{offset:016x}.{kind}',
                            file_type=kind, size=validation.size, original_path=str(source), offset=offset,
                            integrity=validation.integrity,
                            sha256=digest, source_stat=snapshot(original),
                            status=CandidateStatus.PARTIAL if partial else (
                                CandidateStatus.FAILED if validation.integrity == 'invalid_structure' else CandidateStatus.CANDIDATE))
                        skip = offset + (validation.size if validation.integrity == 'structure_checked' else 1)
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
                    token.report_progress(min(max(skip, position - len(carry)), total - 1), total, count)
                if self.fingerprint(original) != self.fingerprint(os.fstat(search.fileno())) or self.fingerprint(original) != self.fingerprint(source.stat()):
                    raise ScanError('源文件在扫描期间发生变化，结果不可视为完整。')
                token.checkpoint()
                token.report_progress(total, total, count)
        except OSError as error:
            raise ScanError('无法只读访问源文件，请检查路径和权限。') from error

"""Computer recovery orchestration, with explicit deletion evidence and limits."""
from pathlib import Path

from core.computer.content import checked_extents, extent_digest, MAX_FILE_SIZE
from core.computer.devices import ensure_different_device
from core.computer.io import DiskReader, DiskError
from core.computer.partitions import Partition, PartitionError, source_partitions
from core.scanner.image.validation import validate
from core.scanner.signature.scanner import SIGNATURES
from storage.models.candidate import Candidate, CandidateStatus


class ComputerError(RuntimeError):
    pass


def inspect_source(path, kind='image'):
    try:
        with DiskReader(path, kind) as reader:
            result = source_partitions(reader)
            reader.check()
            return result
    except (OSError, ValueError) as error:
        raise ComputerError('无法验证来源范围或分区表。') from error


def check_access(repository, candidate_ids, data_dir, destination=None):
    """Read-only preflight; call before a TaskManager job can write its audit."""
    checked = set()
    for candidate_id in candidate_ids:
        candidate = repository.candidate(candidate_id)
        if candidate is None: raise ComputerError('候选记录不存在。')
        if candidate['source'] not in ('computer_deleted', 'computer_signature'): continue
        task = repository.task(candidate['task_id'])
        source = repository.source(task['source_id']) if task and task['source_id'] else None
        identity = candidate.get('source_stat')
        if not source or source['kind'] != 'computer' or candidate.get('source_id') != source['id'] or not isinstance(identity, dict):
            raise ComputerError('电脑候选缺少可靠的来源绑定，请重新扫描。')
        kind = identity.get('kind')
        if kind not in ('image', 'device'): raise ComputerError('电脑来源类型无法验证。')
        if kind == 'device' and source['locator'] not in checked:
            ensure_different_device(source['locator'], data_dir)
            if destination is not None: ensure_different_device(source['locator'], destination)
            checked.add(source['locator'])
    return True


class _Progress:
    def __init__(self, token, total): self.token, self.total, self.processed, self.count = token, total, 0, 0
    def checkpoint(self): self.token.checkpoint()
    def report_progress(self, processed, total, candidates, phase='computer'):
        amount = min(self.total - 1, processed * self.total // total) if total else 0
        self.processed = max(self.processed, amount)
        self.count = max(self.count, candidates)
        self.token.report_progress(self.processed, self.total, self.count, phase='computer')


class ComputerScanner:
    def __init__(self, data_dir=None, *, max_candidates=10000, max_records=1000000):
        if any(type(n) is not int or n < 1 for n in (max_candidates, max_records)):
            raise ValueError('无效的电脑扫描上限。')
        self.data_dir, self.max_candidates, self.max_records = data_dir, max_candidates, max_records

    def scan(self, path, token, *, kind='image', mode='deleted', partition_offset=None):
        token.checkpoint()
        if kind == 'device':
            if self.data_dir is None: raise ComputerError('扫描原始磁盘前必须指定其他物理盘上的工作目录。')
            ensure_different_device(path, self.data_dir)
        elif kind != 'image': raise ComputerError('未知电脑来源类型。')
        if mode not in ('deleted', 'signature'): raise ComputerError('未知电脑扫描模式。')
        if self.data_dir is not None and kind == 'image':
            source = Path(path).resolve(); work = Path(self.data_dir).resolve()
            if source == work / 'index.db' or source.is_relative_to(work / 'Logs'):
                raise ComputerError('不能扫描正在使用的程序数据库或日志。')
        try:
            with DiskReader(path, kind) as reader:
                original = reader.check()
                partitions = source_partitions(reader)
                if partition_offset is None:
                    partition = next((p for p in partitions if p.file_system in ('NTFS', 'FAT32', 'exFAT')), partitions[0]) if mode == 'deleted' else partitions[0]
                else:
                    partition = next((p for p in partitions if p.offset == partition_offset), None)
                    if partition is None: raise ComputerError('所选分区已变化，请重新检测来源。')
                progress = _Progress(token, partition.size)
                token.report_progress(0, partition.size, 0, phase='computer')
                if mode == 'signature': records = self._signatures(reader, partition, progress)
                else:
                    from core.computer.ntfs import scan_ntfs
                    from core.computer.fat import scan_fat32
                    from core.computer.exfat import scan_exfat
                    engine = {'NTFS': scan_ntfs, 'FAT32': scan_fat32, 'exFAT': scan_exfat}.get(partition.file_system)
                    if engine is None: raise ComputerError('此文件系统暂不支持删除记录解析；可选择签名候选扫描。')
                    limit = {'max_records' if partition.file_system == 'NTFS' else 'max_entries': self.max_records}
                    records = engine(reader, partition, progress, max_candidates=self.max_candidates, **limit)
                count = 0
                for record in records:
                    token.checkpoint()
                    if count >= self.max_candidates: raise ComputerError('电脑候选达到上限，扫描仅部分完成。')
                    size = record['size']; status = record['status']; integrity = record['integrity']
                    extents, digest = (), None
                    if status == 'candidate':
                        try: extents = checked_extents(record['extents'], size, reader.size)
                        except ValueError:
                            status, integrity = 'partial', 'layout_limit'
                        else:
                            reader.check(original)
                            digest = extent_digest(reader, extents, token)
                            reader.check(original)
                    name = record['name']
                    extension = Path(name).suffix.lstrip('.').lower() or 'bin'
                    count += 1
                    progress.count = max(count, progress.count)
                    token.report_progress(progress.processed, partition.size, progress.count, phase='computer')
                    yield Candidate(source='computer_deleted' if mode == 'deleted' else 'computer_signature',
                        name=name, file_type=extension, size=size, original_path=record['original_path'],
                        offset=extents[0][0] if extents else None, status=CandidateStatus(status), integrity=integrity,
                        sha256=digest, source_stat=original, extents=extents,
                        file_system=record.get('file_system', partition.file_system),
                        deletion_state=record.get('deletion_state', 'unknown'), record_id=record.get('record_id'))
                reader.check(original); token.checkpoint()
                token.report_progress(partition.size, partition.size, progress.count, phase='computer')
        except (OSError, ValueError) as error:
            raise ComputerError(str(error) or '电脑来源读取或文件系统解析失败。') from error

    def _signatures(self, reader, partition, token):
        position, skip, carry, count = partition.offset, partition.offset, b'', 0
        end = partition.offset + partition.size
        while position < end:
            token.checkpoint()
            block = reader.read_at(position, min(1024 * 1024, end - position))
            data, base = carry + block, position - len(carry)
            cursor = max(0, skip - base)
            while cursor < len(data):
                token.checkpoint()
                matches = [(data.find(signature, cursor), kind) for signature, kind in SIGNATURES]
                matches = [(at, kind) for at, kind in matches if at >= 0]
                if not matches: break
                at, kind = min(matches); offset = base + at
                if count >= self.max_candidates: raise ComputerError('签名候选数量达到上限，扫描仅部分完成。')
                result = validate(reader, offset, end, kind, token, 64 * 1024 * 1024, 256 * 1024 * 1024)
                count += 1
                ready = result.integrity == 'structure_checked'
                yield {'name': f'offset_{offset:016x}.{kind}', 'original_path': str(reader.path),
                    'size': result.size, 'extents': [(offset, result.size)] if ready else [],
                    'integrity': result.integrity, 'status': 'candidate' if ready else ('failed' if result.integrity == 'invalid_structure' else 'partial'),
                    'file_system': partition.file_system, 'deletion_state': 'unknown'}
                skip = offset + (result.size if ready else 1); cursor = skip - base
            position += len(block)
            if skip > position: position, carry = skip, b''
            else: carry = data[-7:]
            token.report_progress(min(max(skip, position - len(carry)) - partition.offset, partition.size - 1), partition.size, count)

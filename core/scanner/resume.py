"""Resume an interrupted signature scan into the same task, after re-verifying its source.

A scan that stopped because it reached the per-task candidate limit cannot take
more candidates, so it is continued instead: a new task scans the remaining
range from the same point, with its own limit, and records which task it continues.

Signature scans search sources sequentially and every candidate is committed
in order, so continuing from the later of (a) the last recorded checkpoint and
(b) the point right after the last committed candidate yields exactly what an
uninterrupted scan would have produced. Before continuing, the source must
still have the recorded identity and the most recent verified candidates must
still hash to their stored SHA-256, which also catches content changes that
leave device identity untouched. Deleted-record scans are not resumable.
"""
from dataclasses import dataclass
from pathlib import Path
import os

from core.candidate_io import range_digest, snapshot
from core.scanner.validation import CHECKED
from core.tasks import RESUMABLE_STATUSES

SCANNERS = {'signature_scan': 'signature', 'computer_scan': 'computer_signature'}
MAX_CANDIDATES = 10000
SPOT_CHECKS = 3
CHECKPOINT_SECONDS = 2.0


class ResumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResumePlan:
    task_id: str
    kind: str
    source_id: str
    path: str
    position: int
    candidates: int
    start: int
    end: int
    identity: dict
    params: dict
    continuation: bool = False          # a new task covers the rest; ``candidates`` is then 0
    skipped: int = 0                    # live-file hits skipped before ``position`` (0 if not recorded)

    @property
    def searched(self):
        return self.position - self.start

    @property
    def size(self):
        return self.end - self.start


def _resume_point(candidate):
    """Where the scanner continued searching after ``candidate``."""
    following = candidate.get('scan_next')
    if following is not None:
        if type(following) is not int or following < 0:
            raise ResumeError('已保存的候选缺少续扫位置，不能继续；请重新扫描。')
        return following
    offset, size = candidate.get('offset'), candidate.get('size')
    if type(offset) is not int or type(size) is not int or offset < 0 or size < 0:
        raise ResumeError('已保存的候选缺少偏移信息，不能继续；请重新扫描。')
    return offset + (size if candidate.get('integrity') in CHECKED else 1)


def plan_resume(repository, task_id, max_candidates=MAX_CANDIDATES):
    """Database-only plan; raises ResumeError with the reason a task cannot continue."""
    task = repository.task(task_id)
    if task is None:
        raise ResumeError('任务记录不存在，请刷新任务记录。')
    if task['kind'] not in SCANNERS:
        raise ResumeError('只有签名扫描可以从断点继续；其他任务请重新执行。')
    if task['status'] not in RESUMABLE_STATUSES:
        raise ResumeError('该任务已完成或仍在运行，无需从断点继续。')
    checkpoint = repository.scan_checkpoint(task_id)
    if checkpoint is None:
        raise ResumeError('这次扫描没有断点记录（旧版本任务或删除记录解析），请重新扫描。')
    params = checkpoint['params']
    if params.get('scanner') != SCANNERS[task['kind']]:
        raise ResumeError('这次扫描方式不支持从断点继续，请重新扫描。')
    source = repository.source(task['source_id']) if task['source_id'] else None
    if source is None:
        raise ResumeError('任务缺少来源记录，不能继续；请重新扫描。')
    committed = repository.candidate_count(task_id)
    if checkpoint['candidates'] > committed:
        raise ResumeError('断点与已保存的候选不一致，不能继续；请重新扫描。')
    continuation = committed >= max_candidates
    if continuation:
        following = repository.continued_by(task_id)
        if following is not None:
            raise ResumeError('已从这次扫描继续扫描剩余部分，请在任务记录中查看后续任务。')
    position, skipped = checkpoint['position'], checkpoint.get('skipped')
    last = repository.recent_candidates(task_id, 1)
    if last:
        following = _resume_point(last[0])
        if following > position or (following == position and skipped is None):
            # No hit is examined between a candidate and where searching continued after
            # it, so the candidate's count is also the count at the restart point.
            position, skipped = following, last[0].get('scan_skipped')
    if type(skipped) is not int or skipped < 0:
        skipped = 0                     # not recorded by earlier versions
    start = params.get('partition_offset', 0) if task['kind'] == 'computer_scan' else 0
    end = checkpoint['total']
    if type(start) is not int or not 0 <= start <= position <= end:
        raise ResumeError('断点超出来源范围，不能继续；请重新扫描。')
    if position >= end and continuation:
        raise ResumeError('这次扫描已经搜索到来源末尾，没有剩余部分需要继续。')
    return ResumePlan(task_id, task['kind'], source['id'], source['locator'], position, 0 if continuation else committed,
                      start, end, checkpoint['identity'], params, continuation, 0 if continuation else skipped)


def _spot_candidates(repository, plan):
    rows = [c for c in repository.recent_candidates(plan.task_id, 200)
            if c.get('sha256') and type(c.get('offset')) is int and type(c.get('size')) is int]
    return rows[:SPOT_CHECKS]


def verify_resume(repository, plan, data_dir, token=None):
    """Read-only check that the source is unchanged; returns how many candidates were re-hashed."""
    from core.tasks import CancellationToken
    token = token or CancellationToken()
    checks = _spot_candidates(repository, plan)
    changed = ResumeError('来源内容与已保存的候选不一致（磁盘或镜像可能已被写入），不能继续；请重新扫描。')
    if plan.kind == 'signature_scan':
        path = Path(plan.path)
        try:
            with path.open('rb') as stream:
                if snapshot(os.fstat(stream.fileno())) != plan.identity:
                    raise ResumeError('源文件已变化（大小、修改时间或文件编号不同），不能继续；请重新扫描。')
                for candidate in checks:
                    if range_digest(stream, candidate['offset'], candidate['size'], token) != candidate['sha256']:
                        raise changed
        except OSError as error:
            raise ResumeError('无法只读打开源文件，请确认它仍在原位置。') from error
        return len(checks)
    from core.computer.content import extent_digest
    from core.computer.devices import ensure_different_device
    from core.computer.io import DiskError, DiskReader
    from core.computer.partitions import PartitionError, source_partitions
    kind = plan.params.get('kind')
    if kind not in ('image', 'device'):
        raise ResumeError('断点记录的来源类型无效，请重新扫描。')
    try:
        if kind == 'device':
            ensure_different_device(plan.path, data_dir)
        with DiskReader(plan.path, kind) as reader:
            reader.check(plan.identity)
            partition = next((p for p in source_partitions(reader) if p.offset == plan.params.get('partition_offset')), None)
            if partition is None or partition.size != plan.params.get('partition_size'):
                raise ResumeError('所选分区已变化，不能继续；请重新扫描。')
            for candidate in checks:
                extents = tuple(tuple(extent) for extent in candidate.get('extents') or ())
                if not extents or extent_digest(reader, extents, token) != candidate['sha256']:
                    raise changed
            reader.check(plan.identity)
    except ResumeError:
        raise
    except (DiskError, PartitionError, OSError, ValueError) as error:
        raise ResumeError(f'来源身份无法确认或已变化，不能继续：{error}') from error
    return len(checks)


def resume_operation(plan, data_dir, max_candidates=MAX_CANDIDATES):
    """The task operation that continues ``plan``; the scanner re-checks identity on open.

    A continuation records the task it continues; otherwise the original
    task's own settings (including whether live-file hits are kept) carry over.
    """
    continues = plan.task_id if plan.continuation else plan.params.get('continues')
    if plan.kind == 'signature_scan':
        from core.scanner.signature.scanner import SignatureScanner
        return lambda token: SignatureScanner(max_candidates=max_candidates).scan(
            plan.path, token, start=plan.position, start_count=plan.candidates, expected=plan.identity,
            checkpoint_every=CHECKPOINT_SECONDS, continues=continues)
    from core.computer.service import ComputerScanner
    resume = {'position': plan.position, 'candidates': plan.candidates, 'identity': plan.identity,
              'partition_size': plan.params['partition_size'], 'skipped': plan.skipped}
    return lambda token: ComputerScanner(data_dir=data_dir, max_candidates=max_candidates).scan(
        plan.path, token, kind=plan.params['kind'], mode='signature', partition_offset=plan.params['partition_offset'],
        resume=resume, checkpoint_every=CHECKPOINT_SECONDS, include_live=plan.params.get('include_live', True),
        continues=continues)

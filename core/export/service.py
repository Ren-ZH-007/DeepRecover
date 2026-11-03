"""Local verified exports with exclusive output names and separate audit rows."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from core.candidate_io import CandidateAccessError, candidate_input, range_chunks, range_digest, snapshot
from core.tasks import TaskCancelled


class ExportError(RuntimeError):
    pass


def copy_verified(item, target, token):
    owned = None
    try:
        with item.path.open('rb') as source:
            original = item.check(source)
            digest = hashlib.sha256()
            with target.open('xb') as output:
                created = os.fstat(output.fileno())
                owned = (created.st_dev, created.st_ino)
                for data in range_chunks(source, item.offset, item.size, token):
                    output.write(data)
                    digest.update(data)
                output.flush()
            source_hash = digest.hexdigest()
            item.verify_hash(source_hash)
            item.check(source, original)
        with target.open('rb') as output:
            before = snapshot(os.fstat(output.fileno()))
            if snapshot(target.stat()) != before:
                raise ExportError('目标文件在校验前已被替换。')
            target_hash = range_digest(output, 0, item.size, token)
            if snapshot(os.fstat(output.fileno())) != before or snapshot(target.stat()) != before or before['size'] != item.size or source_hash != target_hash:
                raise ExportError('目标文件 SHA-256 校验失败。')
        token.checkpoint()
        return source_hash, target_hash
    except BaseException:
        if owned and target.exists():
            current = target.lstat()
            if (current.st_dev, current.st_ino) == owned:
                target.unlink(missing_ok=True)
        raise


def export_batch(repository, task_id, candidate_ids, destination, data_dir, token):
    ids = list(dict.fromkeys(candidate_ids))
    if not ids or len(ids) > 10000:
        raise ExportError('请选择 1 至 10,000 个候选。')
    destination = Path(destination).resolve()
    protected = [(Path(data_dir) / 'Acquisitions').resolve()]
    # Protect every known backup, even when this batch exports unrelated files.
    protected.extend(Path(path).resolve() for path in repository.source_locations('ios_backup'))
    for candidate_id in ids:
        candidate = repository.candidate(candidate_id)
        if candidate is None:
            raise ExportError('选中的候选不存在，请刷新结果。')
    if any(destination.is_relative_to(root) for root in protected):
        raise ExportError('导出目录不能位于源备份或采集副本目录中。')
    token.checkpoint()
    destination.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(prefix='export-', dir=destination))
    report = batch_dir / 'manifest.json'
    repository.set_task_result(task_id, report)
    exported = failed = attempted = 0
    cancelled = False
    token.report_progress(0, len(ids), 0, phase='export')
    try:
        for candidate_id in ids:
            token.checkpoint()
            candidate = repository.candidate(candidate_id)
            extension = str(candidate['file_type']).lower()
            extension = extension if re.fullmatch('[a-z0-9]{1,10}', extension) else 'bin'
            target = batch_dir / f'{uuid4().hex}.{extension}'
            source_task = repository.task(candidate['task_id'])
            source = repository.source(source_task['source_id']) if source_task['source_id'] else None
            record = repository.start_export(task_id, candidate_id, target, {**candidate, 'source_record': source})
            attempted += 1
            try:
                item = candidate_input(repository, candidate_id, data_dir)
                source_hash, target_hash = copy_verified(item, target, token)
                repository.finish_export(record, 'exported', source_sha256=source_hash, target_sha256=target_hash)
                exported += 1
            except TaskCancelled:
                repository.finish_export(record, 'cancelled', error_code='TaskCancelled')
                raise
            except (CandidateAccessError, OSError, ExportError) as error:
                repository.finish_export(record, 'failed', error_code=type(error).__name__)
                failed += 1
            token.report_progress(attempted, len(ids), exported, phase='export')
        token.checkpoint()
    except TaskCancelled:
        cancelled = True
        raise
    finally:
        records = repository.exports(task_id)
        payload = {'version': 1, 'task_id': task_id, 'selected': len(ids), 'attempted': attempted,
                   'exported': exported, 'failed': failed, 'cancelled': cancelled,
                   'not_attempted': len(ids) - attempted, 'records': records,
                   'meaning': 'Ordinary copies or candidate byte ranges; not proof of deleted-data recovery.'}
        with report.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
    if failed:
        raise ExportError('批量导出存在失败项；已校验文件保留，请查看导出记录。')
    return {'exported': exported, 'manifest': str(report)}

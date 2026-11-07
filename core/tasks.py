from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event, Lock
import logging

class TaskCancelled(Exception):
    pass


RESUMABLE_STATUSES = frozenset({'cancelled', 'interrupted', 'failed'})

@dataclass(frozen=True)
class ScanCheckpoint:
    """Resumable scan position, yielded between candidates when requested.

    ``position`` is the first source offset not yet fully searched; every
    candidate starting before it has already been yielded. The task runner
    commits pending candidates before recording it.
    """
    position: int
    candidates: int
    total: int
    identity: dict
    params: dict


@dataclass(frozen=True)
class Progress:
    processed: int
    total: int
    candidates: int
    phase: str = 'scan'

class CancellationToken:
    def __init__(self):
        self._event = Event()
        self._progress_lock = Lock()
        self._progress = None
    def cancel(self):
        self._event.set()
    def checkpoint(self):
        if self._event.is_set():
            raise TaskCancelled()

    @property
    def progress(self):
        with self._progress_lock:
            return self._progress

    def report_progress(self, processed, total, candidates, phase='scan'):
        if not 0 <= processed <= total or candidates < 0:
            raise ValueError('Invalid progress')
        with self._progress_lock:
            if self._progress and (total != self._progress.total or processed < self._progress.processed or candidates < self._progress.candidates):
                raise ValueError('Progress cannot go backwards')
            self._progress = Progress(processed, total, candidates, phase)

@dataclass
class TaskHandle:
    id: str
    token: CancellationToken
    future: object
    def cancel(self):
        self.token.cancel()

class TaskManager:
    def __init__(self, repository):
        self.repository = repository
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deeprecover")
        self.handles = []
        self._handles_lock = Lock()

    def submit(self, kind, operation, *, persist_candidates=False, source=None, batch_size=100, with_task_id=False,
               resume_task=None):
        """Run ``operation``; ``resume_task`` continues an existing task instead of creating one."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("Invalid batch size")
        if resume_task is None:
            task_id = self.repository.create_task(kind, source)
        else:
            task = self.repository.task(resume_task)
            if task is None or task['kind'] != kind or task['status'] not in RESUMABLE_STATUSES:
                raise ValueError('Task cannot be resumed')
            task_id = resume_task
        token = CancellationToken()
        def run():
            self.repository.update_task(task_id, "running")
            try:
                token.checkpoint()
                result = operation(token, task_id) if with_task_id else operation(token)
                if persist_candidates:
                    result = self.persist(task_id, result, token, source, batch_size)
                token.checkpoint()
                self.repository.update_task(task_id, "completed")
                return result
            except TaskCancelled:
                self.repository.update_task(task_id, "cancelled")
                raise
            except Exception as error:
                # Exception text can contain device paths/private contents. Log only type.
                code = type(error).__name__
                self.repository.update_task(task_id, "failed", code)
                logging.getLogger("deeprecover").warning("task_failed type=%s", code)
                raise
        handle = TaskHandle(task_id, token, self.executor.submit(run))
        with self._handles_lock:
            self.handles.append(handle)
        # Futures hold their results (including QImage pixel storage). Retain
        # only in-flight jobs; the caller owns any completed result it needs.
        handle.future.add_done_callback(self._forget_future)
        return handle

    def _forget_future(self, future):
        with self._handles_lock:
            self.handles[:] = [handle for handle in self.handles if handle.future is not future]

    def persist(self, task_id, candidates, token, source, batch_size):
        iterator = iter(candidates)
        try:
            batch, count = [], 0
            for candidate in iterator:
                if isinstance(candidate, ScanCheckpoint):
                    # Never record a position ahead of uncommitted candidates.
                    if batch:
                        self.repository.add_candidates(task_id, batch)
                        count += len(batch)
                        batch.clear()
                    self.repository.save_checkpoint(task_id, candidate)
                    token.checkpoint()
                    continue
                # Persist completed pulls before honoring cancellation at the
                # storage hand-off. Acquisition uses one file per transaction.
                if not candidate.acquired_at:
                    token.checkpoint()
                batch.append(replace(candidate, source_id=source.id) if source else candidate)
                if len(batch) >= batch_size:
                    self.repository.add_candidates(task_id, batch)
                    count += len(batch)
                    batch.clear()
                token.checkpoint()
            if batch:
                self.repository.add_candidates(task_id, batch)
                count += len(batch)
            return count
        finally:
            if hasattr(iterator, 'close'):
                iterator.close()

    def shutdown(self):
        with self._handles_lock:
            handles = list(self.handles)
        for handle in handles:
            handle.cancel()
        self.executor.shutdown(wait=True)

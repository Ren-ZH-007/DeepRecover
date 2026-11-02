from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event
import logging

class TaskCancelled(Exception):
    pass

class CancellationToken:
    def __init__(self):
        self._event = Event()
    def cancel(self):
        self._event.set()
    def checkpoint(self):
        if self._event.is_set():
            raise TaskCancelled()

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

    def submit(self, kind, operation, *, persist_candidates=False, source=None, batch_size=100):
        if not 1 <= batch_size <= 1000:
            raise ValueError("Invalid batch size")
        task_id = self.repository.create_task(kind, source)
        token = CancellationToken()
        def run():
            self.repository.update_task(task_id, "running")
            try:
                token.checkpoint()
                result = operation(token)
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
        self.handles.append(handle)
        return handle

    def persist(self, task_id, candidates, token, source, batch_size):
        iterator = iter(candidates)
        try:
            batch, count = [], 0
            for candidate in iterator:
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
        for handle in self.handles:
            handle.cancel()
        self.executor.shutdown(wait=True)

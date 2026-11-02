from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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

    def submit(self, kind, operation, *, persist_candidates=False):
        task_id = self.repository.create_task(kind)
        token = CancellationToken()
        def run():
            self.repository.update_task(task_id, "running")
            try:
                token.checkpoint()
                result = operation(token)
                token.checkpoint()
                if persist_candidates:
                    self.repository.add_candidates(task_id, result)
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

    def shutdown(self):
        for handle in self.handles:
            handle.cancel()
        self.executor.shutdown(wait=True)

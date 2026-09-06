"""Thread-safe runtime lifecycle tracking for background tasks."""

import threading
import time
from typing import Dict, Optional


class TaskCancelled(RuntimeError):
    """Raised when a task reaches a checkpoint after cancellation."""


class TaskCancellation:
    """Cancellation token owned by one registered task execution."""

    def __init__(self):
        self._cancelled = threading.Event()
        self.last_progress = time.monotonic()
        self.started = False
        self.provider_waiters = 0
        self._cancel_callbacks = []
        self._callback_lock = threading.Lock()

    def provider_wait(self, waiting):
        with self._callback_lock:
            self.provider_waiters = max(0, self.provider_waiters + (1 if waiting else -1))

    def cancel(self) -> None:
        self._cancelled.set()
        with self._callback_lock:
            callbacks = list(self._cancel_callbacks)
        for callback in callbacks:
            callback()

    def bind_future(self, future, cancelled):
        def cancel_queued():
            if future.cancel():
                cancelled()
        with self._callback_lock:
            self._cancel_callbacks.append(cancel_queued)
        if self.is_cancelled():
            cancel_queued()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise TaskCancelled("Task execution was cancelled")
        self.last_progress = time.monotonic()
        self.started = True


class TaskRuntimeRegistry:
    """Tracks at most one active execution token per task ID."""

    def __init__(self):
        self._condition = threading.Condition()
        self._tokens: Dict[str, TaskCancellation] = {}
        self._deleting = set()
        self._exports = {}
        self._export_active = set()
        self._closing = False

    def open(self):
        with self._condition:
            self._closing = False

    def close(self):
        """Reject new writers and cancel queued/running work before unlocking storage."""
        with self._condition:
            self._closing = True
            for task_id in set(self._tokens) | set(self._exports):
                self.request_cancel(task_id)

    def wait_idle(self, timeout):
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._tokens and not any(self._exports.values()), timeout)

    def begin(self, task_id: str) -> Optional[TaskCancellation]:
        with self._condition:
            if self._closing or task_id in self._tokens or task_id in self._deleting or self._exports.get(task_id):
                return None
            token = TaskCancellation()
            self._tokens[task_id] = token
            return token

    def finish(self, task_id: str, token: TaskCancellation) -> None:
        with self._condition:
            if self._tokens.get(task_id) is not token:
                return
            del self._tokens[task_id]
            self._condition.notify_all()

    def request_cancel(self, task_id: str) -> bool:
        with self._condition:
            token = self._tokens.get(task_id)
            exports = self._exports.get(task_id, {})
            if token:
                token.cancel()
            for work in list(exports.values()):
                work.cancel()
            self._condition.notify_all()
            return bool(token or exports)

    def claim_delete(self, task_id: str) -> bool:
        """Block new executions while allowing an existing token to drain."""
        with self._condition:
            if self._closing or task_id in self._deleting:
                return False
            self._deleting.add(task_id)
            return True

    def finish_delete(self, task_id: str) -> None:
        with self._condition:
            self._deleting.discard(task_id)
            self._condition.notify_all()

    def is_deleting(self, task_id: str) -> bool:
        with self._condition:
            return task_id in self._deleting

    def wait_until_stopped(self, task_id: str, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: task_id not in self._tokens and not self._exports.get(task_id),
                timeout=timeout,
            )

    def is_running(self, task_id: str) -> bool:
        with self._condition:
            return task_id in self._tokens or bool(self._exports.get(task_id))

    def generation_state(self, task_id):
        with self._condition:
            token = self._tokens.get(task_id)
            if not token: return 'idle'
            if not token.started: return 'queued'
            return 'provider_wait' if token.provider_waiters else 'running'

    def has_exports(self, task_id):
        with self._condition:
            return bool(self._exports.get(task_id))

    def is_queued(self, task_id):
        with self._condition:
            token = self._tokens.get(task_id)
            return bool(token and not token.started)

    def register_export(self, task_id, job_id):
        with self._condition:
            if self._closing or task_id in self._deleting:
                return None
            return self._exports.setdefault(task_id, {}).setdefault(job_id, TaskCancellation())

    def start_export(self, task_id, job_id):
        with self._condition:
            token = self._exports.get(task_id, {}).get(job_id)
            if token is None:
                return False
            self._condition.wait_for(lambda: token.is_cancelled() or (
                task_id not in self._tokens and task_id not in self._export_active))
            if token.is_cancelled():
                return False
            self._export_active.add(task_id)
            return True

    def finish_export(self, task_id, job_id, active=False):
        with self._condition:
            self._exports.get(task_id, {}).pop(job_id, None)
            if active:
                self._export_active.discard(task_id)
            self._condition.notify_all()

    def export_cancelled(self, task_id, job_id):
        with self._condition:
            token = self._exports.get(task_id, {}).get(job_id)
            return task_id in self._deleting or bool(token and token.is_cancelled())

    def cancel_export(self, task_id, job_id):
        with self._condition:
            token = self._exports.get(task_id, {}).get(job_id)
            if token:
                token.cancel()
            self._condition.notify_all()

    def stalled(self, task_id, timeout):
        with self._condition:
            token = self._tokens.get(task_id)
            return bool(token and token.started and time.monotonic() - token.last_progress > timeout)


task_runtime = TaskRuntimeRegistry()

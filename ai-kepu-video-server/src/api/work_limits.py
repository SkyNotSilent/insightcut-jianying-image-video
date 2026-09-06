"""Process-wide bounded workers; queued work does not create a thread per request."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore

generation_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="generation")
export_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="export")
preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-preview")
io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-io")
query_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="status-query")
render_slot = BoundedSemaphore(1)


async def run_query(function, *args, **kwargs):
    """Read-only status work never queues behind document parsing or file copies."""
    return await asyncio.get_running_loop().run_in_executor(
        query_pool, partial(function, *args, **kwargs))


async def run_io(function, *args, preview=False, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(
        preview_pool if preview else io_pool, partial(function, *args, **kwargs))


class GenerationThread:
    """Thread-compatible launcher for the executor's existing lifecycle wrappers."""
    def __init__(self, target, args=(), kwargs=None, daemon=True, **_):
        self.target, self.args, self.kwargs = target, args, kwargs or {}
        self.daemon = daemon
        self.future = None

    def start(self):
        if self.future is not None:
            raise RuntimeError("工作已经启动")
        self.future = generation_pool.submit(self.target, *self.args, **self.kwargs)
        # A queued cancellation must release its project immediately, without waiting
        # behind unrelated provider calls to reach the front of the pool.
        if self.args and hasattr(self.args[0], "bind_future"):
            token, task_id = self.args[:2]
            def cancelled():
                from src.api import task_executor
                if not task_executor.task_runtime.is_deleting(task_id):
                    row = task_executor.db_client.get_task(task_id) or {}
                    task_executor.db_client.mark_task_interrupted(task_id, row.get("current_step") or "pending", "排队任务已取消，已有内容已保留")
                    task_executor.task_manager.invalidate_task_cache(task_id)
                task_executor.task_runtime.finish(task_id, token)
            token.bind_future(self.future, cancelled)

    def is_alive(self):
        return bool(self.future and not self.future.done())

    def join(self, timeout=None):
        if self.future:
            try:
                self.future.result(timeout=timeout)
            except TimeoutError:
                pass

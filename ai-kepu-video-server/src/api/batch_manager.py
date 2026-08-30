"""Persistent FIFO scheduler for review-first batch planning."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import nullcontext
from typing import Dict, Optional

from src.database import db_client

from .error_model import classify_exception
from .models import TaskStatus
from .task_executor import task_executor
from .task_manager import task_manager
from .task_runtime import task_runtime

logger = logging.getLogger(__name__)

GLOBAL_BATCH_CONCURRENCY = 3


class BatchScheduler:
    """Coordinate durable batch items while task execution owns generation threads."""

    def __init__(
        self,
        *,
        database=None,
        manager=None,
        executor=None,
        runtime=None,
        global_concurrency: int = GLOBAL_BATCH_CONCURRENCY,
        poll_interval: float = 0.25,
        owner_id: Optional[str] = None,
    ):
        self.database = database or db_client
        self.manager = manager or task_manager
        self.executor = executor or task_executor
        self.runtime = runtime or task_runtime
        self.global_concurrency = max(1, min(3, int(global_concurrency)))
        self.poll_interval = max(0.02, float(poll_interval))
        self.owner_id = owner_id or f"batch_scheduler_{uuid.uuid4().hex}"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._launch_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._workers: Dict[str, threading.Thread] = {}
        self._has_leadership = False
        self._recovery_completed = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.set()
            self._has_leadership = False
            self._recovery_completed = False
            self._thread = threading.Thread(
                target=self._run_loop,
                name="insightcut-batch-scheduler",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def join(self, timeout: Optional[float] = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def wake(self) -> None:
        self._wake_event.set()

    def _cleanup_workers(self) -> None:
        with self._lock:
            self._workers = {
                item_id: worker
                for item_id, worker in self._workers.items()
                if worker.is_alive()
            }

    def _dispatch(self) -> None:
        self._cleanup_workers()
        while not self._stop_event.is_set():
            with self._lock:
                if len(self._workers) >= self.global_concurrency:
                    return
            item = self.database.claim_next_batch_item(
                global_concurrency=self.global_concurrency
            )
            if not item:
                return
            worker = threading.Thread(
                target=self._run_item_guarded,
                args=(item,),
                name=f"insightcut-batch-{item['item_id'][:8]}",
                daemon=True,
            )
            with self._lock:
                self._workers[item["item_id"]] = worker
            worker.start()

    def _ensure_leadership(self) -> bool:
        acquire = getattr(self.database, "try_acquire_batch_scheduler_lock", None)
        if callable(acquire):
            try:
                acquired = bool(acquire(self.owner_id))
            except Exception:
                logger.exception("批量调度器进程锁获取失败")
                acquired = False
        else:
            acquired = True
        if not acquired:
            self._has_leadership = False
            self._recovery_completed = False
            return False
        self._has_leadership = acquired
        if not self._recovery_completed:
            recover = getattr(self.database, "recover_batch_items", None)
            if callable(recover):
                recover()
            self._recovery_completed = True
        return acquired

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    if self._ensure_leadership():
                        self._dispatch()
                except Exception:
                    logger.exception("批量调度循环遇到瞬时故障，将继续重试")
                self._wake_event.wait(self.poll_interval)
                self._wake_event.clear()
        finally:
            release = getattr(self.database, "release_batch_scheduler_lock", None)
            if callable(release):
                try:
                    release(self.owner_id)
                except Exception:
                    logger.exception("批量调度器进程锁释放失败")
            self._has_leadership = False
            self._recovery_completed = False

    def _create_task(self, item: Dict) -> str:
        config = item.get("config") or {}
        proposed_task_id = uuid.uuid4().hex
        reserve = getattr(self.database, "reserve_batch_item_task", None)
        task_id = (
            reserve(item["item_id"], proposed_task_id)
            if callable(reserve)
            else proposed_task_id
        )
        if not task_id:
            raise RuntimeError("批次任务标识预留失败")
        if self.database.get_task(task_id):
            return task_id
        task_id = self.manager.create_task(
            theme=item["theme"],
            name=item.get("name") or item["theme"][:20],
            style=config.get("style") or "温暖感人",
            length=int(config.get("length") or 300),
            voice_type=config.get("voice_type"),
            ratio=config.get("ratio") or "16:9",
            tts_options=config.get("tts_options") or {},
            execution_mode="review_first",
            script_policy=config.get("script_policy") or "rewrite",
            template_id=config.get("template_id"),
            generation_options=config.get("generation_options") or {},
            subtitle_options=config.get("subtitle_options") or {},
            task_id=task_id,
        )
        if not callable(reserve) and not self.database.set_batch_item_task(
            item["item_id"], task_id
        ):
            raise RuntimeError("批次任务关联保存失败")
        return task_id

    def _start_or_resume(self, item: Dict, task_id: str) -> bool:
        row = self.database.get_task(task_id) or {}
        status = row.get("status")
        if status in {
            TaskStatus.AWAITING_CONFIRMATION.value,
            TaskStatus.AWAITING_FINALIZATION.value,
            TaskStatus.COMPLETED.value,
        }:
            return True
        if status in {TaskStatus.FAILED.value, TaskStatus.INTERRUPTED.value}:
            return self.executor.resume_task(task_id) in {"started", "already_running"}
        if status in {TaskStatus.PENDING.value, TaskStatus.PROCESSING.value}:
            if self.runtime.is_running(task_id):
                return True
            # A persisted task may have been created immediately before a crash.
            if status == TaskStatus.PENDING.value:
                return self.executor.execute_task(
                    task_id=task_id,
                    theme=row.get("theme") or item["theme"],
                    style=row.get("style") or "温暖感人",
                    length=int(row.get("length") or 300),
                    voice_type=row.get("voice_type"),
                    ratio=row.get("ratio") or "16:9",
                    input_mode="theme",
                )
        return False

    def _run_item(self, item: Dict) -> None:
        current = self.database.get_batch_item(item["item_id"])
        if not current or current.get("cancel_requested"):
            self.database.update_batch_item_status(item["item_id"], "cancelled")
            return
        with self._launch_lock:
            guard_factory = getattr(self.database, "batch_launch_guard", None)
            guard = guard_factory() if callable(guard_factory) else nullcontext()
            with guard:
                current = self.database.get_batch_item(item["item_id"])
                if not current or current.get("cancel_requested"):
                    self.database.update_batch_item_status(item["item_id"], "cancelled")
                    return
                task_id = current.get("task_id")
                if not task_id or not self.database.get_task(task_id):
                    task_id = self._create_task(current)
                current = self.database.get_batch_item(item["item_id"])
                if not current or current.get("cancel_requested"):
                    self.database.update_batch_item_status(item["item_id"], "cancelled")
                    return
                if not self._start_or_resume(current, task_id):
                    raise RuntimeError("批次项目无法启动或恢复")

        while not self._stop_event.is_set():
            current = self.database.get_batch_item(item["item_id"])
            if not current:
                return
            if current.get("cancel_requested"):
                self.executor.cancel_task(task_id, 30)
                self.database.update_batch_item_status(item["item_id"], "cancelled")
                return
            task = self.database.get_task(task_id) or {}
            status = task.get("status")
            if status in {
                TaskStatus.AWAITING_CONFIRMATION.value,
                TaskStatus.AWAITING_FINALIZATION.value,
                TaskStatus.COMPLETED.value,
            }:
                self.database.update_batch_item_status(
                    item["item_id"], "awaiting_confirmation"
                )
                return
            if status in {TaskStatus.FAILED.value, TaskStatus.INTERRUPTED.value}:
                self.database.update_batch_item_status(
                    item["item_id"],
                    "failed",
                    error=task.get("error"),
                    error_code=task.get("error_code"),
                    error_meta=task.get("error_meta"),
                )
                return
            if not self.runtime.is_running(task_id) and status not in {
                TaskStatus.PENDING.value,
                TaskStatus.PROCESSING.value,
            }:
                raise RuntimeError("批次项目异常停止")
            time.sleep(self.poll_interval)

    def _run_item_guarded(self, item: Dict) -> None:
        try:
            self._run_item(item)
        except Exception as error:
            safe = classify_exception(error, provider="batch_scheduler")
            logger.error(
                "[%s] 批次项目失败: %s", item.get("item_id"), safe.safe_message
            )
            try:
                current = self.database.get_batch_item(item["item_id"])
                if current and current.get("status") == "running":
                    self.database.update_batch_item_status(
                        item["item_id"],
                        "failed",
                        error=safe.safe_message,
                        error_code=safe.code.value,
                        error_meta=safe.metadata(),
                    )
            except Exception:
                logger.exception("[%s] 保存批次失败状态异常", item.get("item_id"))
        finally:
            self._wake_event.set()

    def cancel_batch(self, batch_id: str) -> Dict:
        with self._launch_lock:
            result = self.database.cancel_batch(batch_id)
            for running in result.get("running", []):
                task_id = running.get("task_id")
                if task_id:
                    self.runtime.request_cancel(task_id)
        self.wake()
        return self.database.get_batch(batch_id) if result else {}

    def retry_failed(self, batch_id: str) -> int:
        count = self.database.retry_failed_batch_items(batch_id)
        if count > 0:
            self.wake()
        return count


def new_batch_id() -> str:
    return f"batch_{uuid.uuid4().hex}"


def new_batch_item_id() -> str:
    return f"batch_item_{uuid.uuid4().hex}"


batch_scheduler = BatchScheduler()


__all__ = [
    "BatchScheduler",
    "GLOBAL_BATCH_CONCURRENCY",
    "batch_scheduler",
    "new_batch_id",
    "new_batch_item_id",
]

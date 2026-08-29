"""Periodic recovery for stale tasks and orphaned durable operations."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from src.database import db_client

from .models import TaskStatus
from .task_manager import task_manager
from .task_runtime import task_runtime

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 300.0
SWEEP_INTERVAL_ENV = "TASK_SWEEPER_INTERVAL_SECONDS"


def _configured_interval_seconds() -> float:
    raw_value = os.getenv(SWEEP_INTERVAL_ENV)
    if not raw_value:
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    try:
        interval = float(raw_value)
    except ValueError:
        logger.warning("%s 配置无效: %s", SWEEP_INTERVAL_ENV, raw_value)
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    if interval <= 0:
        logger.warning("%s 必须大于 0: %s", SWEEP_INTERVAL_ENV, raw_value)
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    return interval


@dataclass(frozen=True)
class SweepResult:
    scanned_tasks: int = 0
    interrupted_tasks: int = 0
    interrupted_operations: int = 0
    reconciled_tasks: int = 0
    errors: int = 0


class TaskSweeper:
    """Run blocking SQLite recovery scans in one explicitly owned daemon thread."""

    def __init__(
        self,
        *,
        manager=None,
        database=None,
        runtime=None,
        interval_seconds: Optional[float] = None,
        scan_limit: int = 200,
    ):
        self.manager = manager or task_manager
        self.database = database or db_client
        self.runtime = runtime or task_runtime
        self.interval_seconds = (
            _configured_interval_seconds()
            if interval_seconds is None
            else max(0.001, float(interval_seconds))
        )
        self.scan_limit = max(1, int(scan_limit))
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        """Start one periodic scanner; repeated starts are idempotent."""
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="insightcut-task-sweeper",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Signal the scanner to stop without blocking the caller."""
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the scanner thread and report whether it has stopped."""
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _run_loop(self) -> None:
        # Waiting before the first scan avoids racing the synchronous API path
        # that creates an operation and immediately registers its runtime token.
        while not self._stop_event.wait(self.interval_seconds):
            try:
                result = self.run_once()
                if result.interrupted_tasks or result.interrupted_operations:
                    logger.warning(
                        "后台巡检完成: 中断超时任务 %s 个，清理孤儿操作 %s 个",
                        result.interrupted_tasks,
                        result.interrupted_operations,
                    )
            except Exception:
                # One failed round must not permanently stop later recovery.
                logger.exception("后台任务巡检执行失败")

    def run_once(self) -> SweepResult:
        """Perform one synchronous, directly testable and idempotent scan."""
        rows = []
        offset = 0
        while True:
            page = self.database.list_tasks(
                status=None,
                limit=self.scan_limit,
                offset=offset,
            )
            rows.extend(page)
            if len(page) < self.scan_limit:
                break
            offset += len(page)
        interrupted_tasks = 0
        interrupted_operations = 0
        reconciled_tasks = 0
        errors = 0

        for row in rows:
            task_id = row.get("task_id")
            if not task_id:
                continue
            try:
                if self.manager.reconcile_completed_task_data(row):
                    reconciled_tasks += 1
                    continue
                active_operation = self.database.get_active_task_operation(task_id)
                if active_operation and not self.runtime.is_running(task_id):
                    interrupted = self.database.interrupt_orphaned_task_operation(task_id)
                    if interrupted:
                        operation_kind = interrupted.get("kind")
                        workflow_phase = (
                            "finalizing"
                            if operation_kind == "finalize"
                            else "generating_assets"
                        )
                        current_step = (
                            "finalize_failed"
                            if operation_kind == "finalize"
                            else "asset_repair"
                        )
                        self.database.update_task_workflow(
                            task_id,
                            workflow_phase,
                            status=TaskStatus.INTERRUPTED.value,
                            current_step=current_step,
                        )
                        self.manager.invalidate_task_cache(task_id)
                        interrupted_operations += 1
                        # The operation transition already made this task
                        # recoverable; do not also classify it as stale.
                        continue

                if self.manager.fail_stale_task_data(row):
                    interrupted_tasks += 1
            except Exception:
                errors += 1
                logger.exception("[%s] 后台巡检任务失败", task_id)

        return SweepResult(
            scanned_tasks=len(rows),
            interrupted_tasks=interrupted_tasks,
            interrupted_operations=interrupted_operations,
            reconciled_tasks=reconciled_tasks,
            errors=errors,
        )


task_sweeper = TaskSweeper()

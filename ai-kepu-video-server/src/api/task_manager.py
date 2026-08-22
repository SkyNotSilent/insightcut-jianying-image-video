"""
任务管理模块
负责任务的创建、状态跟踪、进度更新
使用本地 SQLite 持久化和内存缓存
"""

import uuid
import time
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable
from threading import Thread, Lock
from .models import (
    TaskStatus, StepStatus, TaskProgress,
    StepProgress, TaskResult, TaskResponse
)
from src.database import db_client, redis_client
from src.config import Config
from .error_model import ErrorCode, SafeError, make_safe_error
from .task_cleanup import DeletionReport, collect_task_paths, delete_task_files
from .task_runtime import task_runtime

logger = logging.getLogger(__name__)

DEFAULT_STALE_TASK_TIMEOUT_SECONDS = 30 * 60
STEP_STALE_TASK_TIMEOUT_SECONDS = {
    "pending": 10 * 60,
    "text_generation": 10 * 60,
    "image_prompt_generation": 15 * 60,
    "voiceover_generation": 30 * 60,
    "image_generation": 45 * 60,
    "draft_building": 5 * 60,
}
WAITING_WORKFLOW_PHASES = {
    "awaiting_confirmation",
    "awaiting_finalization",
}


def _stale_task_timeout_seconds(step_name: str) -> int:
    raw_value = os.getenv("TASK_STALE_TIMEOUT_SECONDS")
    if raw_value:
        try:
            return max(60, int(raw_value))
        except ValueError:
            logger.warning("TASK_STALE_TIMEOUT_SECONDS 配置无效: %s", raw_value)
    return STEP_STALE_TASK_TIMEOUT_SECONDS.get(step_name, DEFAULT_STALE_TASK_TIMEOUT_SECONDS)


def _parse_task_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None

class Task:
    """任务对象"""

    def __init__(
        self,
        task_id: str,
        theme: str,
        style: str,
        length: int,
        voice_type: Optional[str] = None,
        name: Optional[str] = None,
        ratio: str = "16:9",
        tts_options: Optional[Dict] = None,
        execution_mode: str = "full",
        workflow_phase: str = "pending",
        plan_version: int = 0,
        voice_confirmed: bool = False,
    ):
        self.task_id = task_id
        self.theme = theme
        self.name = name or theme[:20]
        self.style = style
        self.ratio = ratio or "16:9"
        self.length = length
        self.voice_type = voice_type
        self.tts_options = dict(tts_options or {})
        self.execution_mode = execution_mode or "full"
        self.workflow_phase = workflow_phase or "pending"
        self.plan_version = int(plan_version or 0)
        self.voice_confirmed = bool(voice_confirmed)
        self.status = TaskStatus.PENDING
        self.created_at = datetime.now().isoformat()
        self.error: Optional[str] = None
        self.error_code: Optional[str] = None
        self.error_meta: Optional[Dict] = None
        self.can_resume = False
        self.result: Optional[TaskResult] = None
        self.extract_path: Optional[str] = None

        # 进度跟踪
        self.current_step = "pending"
        self.steps = {
            "text_generation": StepProgress(
                name="text_generation",
                status=StepStatus.PENDING
            ),
            "image_prompt_generation": StepProgress(
                name="image_prompt_generation",
                status=StepStatus.PENDING
            ),
            "voiceover_generation": StepProgress(
                name="voiceover_generation",
                status=StepStatus.PENDING
            ),
            "image_generation": StepProgress(
                name="image_generation",
                status=StepStatus.PENDING
            ),
            "draft_building": StepProgress(
                name="draft_building",
                status=StepStatus.PENDING
            )
        }
        self.step_start_times: Dict[str, float] = {}

    def start_step(self, step_name: str):
        """开始某个步骤"""
        if step_name in self.steps:
            self.current_step = step_name
            self.steps[step_name].status = StepStatus.PROCESSING
            self.step_start_times[step_name] = time.time()
            logger.info(f"[{self.task_id}] 开始步骤: {step_name}")

            # 更新到本地数据库和内存缓存
            db_client.update_step(self.task_id, step_name, "processing")
            db_client.update_task_status(self.task_id, "processing", step_name)
            self._sync_progress_to_cache()

    def update_step_progress(self, step_name: str, progress: int, total: int):
        """更新步骤进度"""
        if step_name in self.steps:
            self.steps[step_name].progress = progress
            self.steps[step_name].total = total
            logger.debug(f"[{self.task_id}] {step_name} 进度: {progress}/{total}")

            # 更新到本地数据库和内存缓存
            db_client.update_step(self.task_id, step_name, "processing", progress, total)
            self._sync_progress_to_cache()

    def complete_step(self, step_name: str):
        """完成某个步骤"""
        if step_name in self.steps:
            self.steps[step_name].status = StepStatus.COMPLETED
            if step_name in self.step_start_times:
                duration = time.time() - self.step_start_times[step_name]
                self.steps[step_name].duration = round(duration, 2)
            logger.info(f"[{self.task_id}] 完成步骤: {step_name}")

            # 更新到本地数据库和内存缓存
            db_client.update_step(
                self.task_id, step_name, "completed",
                self.steps[step_name].progress,
                self.steps[step_name].total,
                self.steps[step_name].duration
            )
            self._sync_progress_to_cache()

    def fail_step(self, step_name: str, error: str):
        """步骤失败"""
        if step_name in self.steps:
            self.steps[step_name].status = StepStatus.FAILED
            self.error = error
            logger.error(f"[{self.task_id}] 步骤失败 {step_name}: {error}")

            # 更新到本地数据库和内存缓存
            db_client.update_step(self.task_id, step_name, "failed")
            self._sync_progress_to_cache()

    def _sync_progress_to_cache(self):
        """同步进度到内存缓存"""
        steps_dict = {
            name: {
                "name": step.name,
                "status": step.status,
                "progress": step.progress,
                "total": step.total,
                "duration": step.duration,
            }
            for name, step in self.steps.items()
        }
        redis_client.update_progress(self.task_id, self.current_step, steps_dict)

    def to_response(self) -> TaskResponse:
        """转换为响应对象"""
        progress = TaskProgress(
            current_step=self.current_step,
            steps=list(self.steps.values())
        )

        return TaskResponse(
            task_id=self.task_id,
            status=self.status,
            voice_type=self.voice_type,
            tts_options=self.tts_options or None,
            progress=progress if self.status in [
                TaskStatus.PENDING,
                TaskStatus.PROCESSING,
                TaskStatus.AWAITING_CONFIRMATION,
                TaskStatus.INTERRUPTED,
            ] else None,
            result=self.result,
            extract_path=self.extract_path,
            error=self.error,
            error_code=self.error_code,
            error_meta=self.error_meta,
            can_resume=self.can_resume,
            workflow_phase=self.workflow_phase,
            plan_version=self.plan_version,
            execution_mode=self.execution_mode,
            voice_confirmed=self.voice_confirmed,
        )


class TaskManager:
    """任务管理器"""

    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.lock = Lock()
        self.deletion_lock = Lock()
        self.deletion_claims = set()
        self.deleted_task_ids = set()
        self.deletion_reports: Dict[str, DeletionReport] = {}

    def create_task(
        self,
        theme: str,
        style: str,
        length: int,
        voice_type: Optional[str] = None,
        name: Optional[str] = None,
        ratio: str = "16:9",
        tts_options: Optional[Dict] = None,
        execution_mode: str = "full",
        script_policy: str = "rewrite",
        source_draft_id: Optional[str] = None,
        template_id: Optional[str] = None,
        generation_options: Optional[Dict] = None,
        subtitle_options: Optional[Dict] = None,
    ) -> str:
        """创建新任务"""
        task_id = uuid.uuid4().hex
        workflow_phase = "planning" if execution_mode == "review_first" else "pending"
        task = Task(
            task_id, theme, style, length, voice_type, name, ratio, tts_options,
            execution_mode=execution_mode, workflow_phase=workflow_phase,
        )

        with self.lock:
            self.tasks[task_id] = task

        db_client.create_task(
            task_id, theme, style, length, name, ratio, voice_type,
            tts_options=task.tts_options,
            execution_mode=execution_mode,
            script_policy=script_policy,
            source_draft_id=source_draft_id,
            template_id=template_id,
            generation_options=generation_options,
            subtitle_options=subtitle_options,
        )

        # 缓存到内存
        task_data = {
            "task_id": task_id,
            "theme": theme,
            "style": style,
            "ratio": ratio,
            "length": length,
            "voice_type": voice_type,
            "tts_options": task.tts_options,
            "status": "pending",
            "created_at": task.created_at,
            "execution_mode": execution_mode,
            "workflow_phase": workflow_phase,
            "plan_version": 0,
            "voice_confirmed": False,
            "source_draft_id": source_draft_id,
            "template_id": template_id,
            "generation_options": generation_options or {},
            "subtitle_options": subtitle_options or {},
        }
        redis_client.cache_task(task_id, task_data)

        logger.info(f"创建任务: {task_id}, 主题: {theme}")
        return task_id

    def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务（优先从运行时内存，然后缓存，最后本地数据库）"""
        # 1. 从内存获取
        with self.lock:
            memory_task = self.tasks.get(task_id)
        if memory_task:
            db_data = db_client.get_task(task_id)
            if db_data and self.fail_stale_task_data(db_data):
                rebuilt = self._rebuild_task_from_db(db_data)
                with self.lock:
                    if rebuilt:
                        self.tasks[task_id] = rebuilt
                    else:
                        self.tasks.pop(task_id, None)
                return rebuilt
            if db_data:
                memory_task.can_resume = self._has_recovery_checkpoint(db_data)
            return memory_task

        # 2. 从缓存获取
        cached_data = redis_client.get_task(task_id)
        if cached_data:
            if cached_data.get("status") in {
                "pending", "processing", "failed", "interrupted"
            }:
                db_data = db_client.get_task(task_id)
                if db_data:
                    self.fail_stale_task_data(db_data)
                    task = self._rebuild_task_from_db(db_data)
                    if task:
                        with self.lock:
                            self.tasks[task_id] = task
                        redis_client.cache_task(task_id, self._task_to_dict(task))
                        return task
            # 重建 Task 对象
            task = self._rebuild_task_from_cache(cached_data)
            if task:
                with self.lock:
                    self.tasks[task_id] = task
                return task

        # 3. 从本地数据库获取
        db_data = db_client.get_task(task_id)
        if db_data:
            self.fail_stale_task_data(db_data)
            task = self._rebuild_task_from_db(db_data)
            if task:
                with self.lock:
                    self.tasks[task_id] = task
                # 回写到缓存
                redis_client.cache_task(task_id, self._task_to_dict(task))
                return task

        return None

    def _has_recovery_checkpoint(self, data: dict) -> bool:
        if not data or data.get("status") not in {
            TaskStatus.FAILED.value,
            TaskStatus.INTERRUPTED.value,
        }:
            return False
        return bool(
            data.get("theme")
            or data.get("script_text")
            or db_client.get_segments(data["task_id"])
        )

    def _rebuild_task_from_cache(self, data: dict) -> Optional[Task]:
        """从缓存数据重建 Task 对象"""
        try:
            task = Task(
                data["task_id"],
                data["theme"],
                data["style"],
                data["length"],
                voice_type=data.get("voice_type"),
                ratio=data.get("ratio", "16:9"),
                tts_options=data.get("tts_options"),
                execution_mode=data.get("execution_mode", "full"),
                workflow_phase=data.get("workflow_phase", "pending"),
                plan_version=data.get("plan_version", 0),
                voice_confirmed=data.get("voice_confirmed", False),
            )
            task.status = TaskStatus(data["status"])
            task.created_at = data["created_at"]
            if "error" in data:
                task.error = (
                    (data.get("error_meta") or {}).get("safe_message")
                    or data["error"]
                )
            task.error_code = data.get("error_code")
            task.error_meta = data.get("error_meta")
            if "extract_path" in data:
                task.extract_path = data["extract_path"]
            task.can_resume = bool(data.get("can_resume"))
            if "result" in data and data["result"]:
                task.result = TaskResult(**data["result"])

            # 获取进度信息
            progress_data = redis_client.get_progress(data["task_id"])
            if progress_data:
                task.current_step = progress_data["current_step"]
                for step_name, step_data in progress_data["steps"].items():
                    if step_name in task.steps:
                        task.steps[step_name].status = StepStatus(step_data["status"])
                        task.steps[step_name].progress = step_data.get("progress")
                        task.steps[step_name].total = step_data.get("total")
                        task.steps[step_name].duration = step_data.get("duration")

            return task
        except Exception as e:
            logger.error(f"从缓存重建任务失败: {e}")
            return None

    def _rebuild_task_from_db(self, data: dict) -> Optional[Task]:
        """从数据库数据重建 Task 对象"""
        try:
            raw_tts_options = data.get("tts_options_json")
            if isinstance(raw_tts_options, str):
                try:
                    raw_tts_options = json.loads(raw_tts_options)
                except (TypeError, ValueError):
                    raw_tts_options = {}
            task = Task(
                data["task_id"],
                data["theme"],
                data["style"],
                data["length"],
                voice_type=data.get("voice_type"),
                ratio=data.get("ratio", "16:9"),
                tts_options=raw_tts_options,
                execution_mode=data.get("execution_mode", "full"),
                workflow_phase=data.get("workflow_phase", "pending"),
                plan_version=data.get("plan_version", 0),
                voice_confirmed=data.get("voice_confirmed", False),
            )
            task.status = TaskStatus(data["status"])
            task.created_at = data["created_at"].isoformat() if hasattr(data["created_at"], "isoformat") else str(data["created_at"])
            task.current_step = data.get("current_step", "pending")
            task.error = (
                (data.get("error_meta") or {}).get("safe_message")
                or data.get("error")
            )
            task.error_code = data.get("error_code")
            task.error_meta = data.get("error_meta")
            task.extract_path = data.get("extract_path")
            task.can_resume = self._has_recovery_checkpoint(data)

            # 重建结果
            if data.get("result"):
                result_data = data["result"]
                task.result = TaskResult(
                    draft_path=result_data["draft_path"],
                    draft_url=result_data.get("draft_url"),
                    video_url=result_data.get("video_url"),
                    theme=data["theme"],
                    segments_count=result_data["segments_count"],
                    total_duration=result_data.get("total_duration"),
                    created_at=task.created_at,
                )

            # 重建步骤
            if data.get("steps"):
                for step_data in data["steps"]:
                    step_name = step_data["step_name"]
                    if step_name in task.steps:
                        task.steps[step_name].status = StepStatus(step_data["status"])
                        task.steps[step_name].progress = step_data.get("progress")
                        task.steps[step_name].total = step_data.get("total")
                        task.steps[step_name].duration = step_data.get("duration")

            return task
        except Exception as e:
            logger.error(f"从数据库重建任务失败: {e}")
            return None

    def _task_to_dict(self, task: Task) -> dict:
        """将 Task 对象转换为字典"""
        return {
            "task_id": task.task_id,
            "theme": task.theme,
            "style": task.style,
            "ratio": task.ratio,
            "length": task.length,
            "voice_type": task.voice_type,
            "tts_options": task.tts_options,
            "status": task.status,
            "created_at": task.created_at,
            "error": task.error,
            "error_code": task.error_code,
            "error_meta": task.error_meta,
            "can_resume": task.can_resume,
            "extract_path": task.extract_path,
            "execution_mode": task.execution_mode,
            "workflow_phase": task.workflow_phase,
            "plan_version": task.plan_version,
            "voice_confirmed": task.voice_confirmed,
            "result": task.result.dict() if task.result else None,
        }

    def invalidate_task_cache(self, task_id: str) -> None:
        """Force the next read to rebuild the task from SQLite."""
        with self.lock:
            self.tasks.pop(task_id, None)
        redis_client.delete_task(task_id)

    def list_tasks(self, status: str = None, limit: int = 100, offset: int = 0):
        """获取任务列表，并清理已超时的非终态任务"""
        rows = db_client.list_tasks(status=status, limit=limit, offset=offset)
        changed = False
        for row in rows:
            changed = self.fail_stale_task_data(row) or changed
        if changed:
            rows = db_client.list_tasks(status=status, limit=limit, offset=offset)
        return [
            row for row in rows
            if row.get("status") != TaskStatus.DELETING.value
        ]

    def fail_stale_task_data(self, data: dict) -> bool:
        """Mark an orphaned stale task interrupted without touching live work."""
        if not data or data.get("status") not in {"pending", "processing"}:
            return False
        if data.get("workflow_phase") in WAITING_WORKFLOW_PHASES:
            return False

        task_id = data["task_id"]
        if task_runtime.is_running(task_id):
            return False

        step_name = data.get("current_step") or "pending"
        updated_at = _parse_task_datetime(data.get("updated_at") or data.get("created_at"))
        if not updated_at:
            return False

        timeout_seconds = _stale_task_timeout_seconds(step_name)
        elapsed_seconds = (datetime.now() - updated_at).total_seconds()
        if elapsed_seconds < timeout_seconds:
            return False

        error = (
            f"任务在 {step_name} 阶段超过 {timeout_seconds // 60} 分钟无进度更新，"
            "已保存现有内容，可继续生成"
        )
        safe = SafeError(
            code=ErrorCode.TIMEOUT,
            retryable=True,
            safe_message=error,
        )
        logger.warning("[%s] %s", task_id, error)
        if not db_client.mark_task_interrupted(
            task_id,
            step_name,
            error,
            error_code=safe.code.value,
            error_meta=safe.metadata(),
        ):
            return False
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task.status = TaskStatus.INTERRUPTED
                task.error = error
        redis_client.delete_task(task_id)
        data["status"] = TaskStatus.INTERRUPTED.value
        data["error"] = error
        return True

    def mark_stale_tasks_failed(self, limit: int = 200) -> int:
        """兼容旧启动调用，遗留任务现在标记为可恢复的中断状态。"""
        return self.mark_orphaned_tasks_interrupted(limit=limit)

    def mark_orphaned_tasks_interrupted(self, limit: int = 200) -> int:
        """将上一个进程遗留的运行任务标记为可恢复的中断状态。"""
        pending_rows = db_client.list_tasks(status="pending", limit=limit, offset=0)
        remaining = max(0, limit - len(pending_rows))
        processing_rows = db_client.list_tasks(
            status="processing", limit=remaining, offset=0
        ) if remaining else []
        interrupted_count = 0
        error = "服务重启导致任务中断，可继续生成"
        safe = make_safe_error(ErrorCode.UNKNOWN)

        for row in pending_rows + processing_rows:
            if row.get("workflow_phase") in WAITING_WORKFLOW_PHASES:
                continue
            task_id = row["task_id"]
            if not db_client.update_task_status(
                task_id,
                TaskStatus.INTERRUPTED.value,
                row.get("current_step"),
                error,
                error_code=safe.code.value,
                error_meta=safe.metadata(),
            ):
                continue
            with self.lock:
                self.tasks.pop(task_id, None)
            redis_client.delete_task(task_id)
            interrupted_count += 1

        return interrupted_count

    def update_task_status(self, task_id: str, status: TaskStatus):
        """更新任务状态"""
        task = self.get_task(task_id)
        if task:
            task.status = status
            if status == TaskStatus.PROCESSING:
                task.error = None
                task.error_code = None
                task.error_meta = None
            logger.info(f"[{task_id}] 状态更新: {status}")

            # 更新到本地数据库
            db_client.update_task_status(task_id, status, task.current_step, task.error)

            # 更新到内存缓存
            redis_client.cache_task(task_id, self._task_to_dict(task))

    def mark_task_interrupted(
        self,
        task_id: str,
        error: str,
        *,
        error_code: Optional[str] = None,
        error_meta: Optional[Dict] = None,
    ) -> bool:
        """Preserve checkpoints and expose the task as resumable."""
        task = self.get_task(task_id)
        current_step = task.current_step if task else None
        if not db_client.mark_task_interrupted(
            task_id,
            current_step,
            error,
            error_code=error_code,
            error_meta=error_meta,
        ):
            return False
        if task:
            task.status = TaskStatus.INTERRUPTED
            task.error = error
            task.error_code = error_code
            task.error_meta = error_meta
            redis_client.cache_task(task_id, self._task_to_dict(task))
        return True

    def set_task_result(self, task_id: str, draft_path: str, segments_count: int, draft_url: str = None, video_url: str = None):
        """设置任务结果"""
        task = self.get_task(task_id)
        if task:
            task.result = TaskResult(
                draft_path=draft_path,
                draft_url=draft_url,
                video_url=video_url,
                theme=task.theme,
                segments_count=segments_count,
                total_duration=None,
                created_at=task.created_at
            )
            logger.info(f"[{task_id}] 设置结果: {draft_path}")
            if draft_url:
                logger.info(f"[{task_id}] 草稿 URL: {draft_url}")
            if video_url:
                logger.info(f"[{task_id}] 视频 URL: {video_url}")

            # 保存到本地数据库
            db_client.save_task_result(task_id, draft_path, segments_count, draft_url, video_url)

            # 更新到内存缓存
            redis_client.cache_task(task_id, self._task_to_dict(task))

    def set_task_error(
        self,
        task_id: str,
        error: str,
        *,
        error_code: Optional[str] = None,
        error_meta: Optional[Dict] = None,
    ):
        """设置任务错误"""
        task = self.get_task(task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.error = error
            task.error_code = error_code
            task.error_meta = error_meta
            logger.error(f"[{task_id}] 任务失败: {error}")

            # 更新到本地数据库
            db_client.update_task_status(
                task_id,
                "failed",
                task.current_step,
                error,
                error_code=error_code,
                error_meta=error_meta,
            )

            # 更新到内存缓存
            redis_client.cache_task(task_id, self._task_to_dict(task))

    def update_extract_path(self, task_id: str, extract_path: str):
        """更新任务的解压路径"""
        task = self.get_task(task_id)
        if task:
            task.extract_path = extract_path
            db_client.update_extract_path(task_id, extract_path)
            redis_client.cache_task(task_id, self._task_to_dict(task))

    def delete_task(self, task_id: str) -> bool:
        """删除任务及其所有关联数据"""
        deleted = db_client.delete_task(task_id)
        if not deleted:
            return False
        with self.lock:
            self.tasks.pop(task_id, None)
        redis_client.delete_task(task_id)
        logger.info(f"任务已删除: {task_id}")
        return True

    def _allowed_storage_roots(self):
        return [Config.BASE_DIR / "output", Config.BASE_DIR / "data" / "media"]

    def _snapshot_task_paths(self, task_id: str):
        task_row = db_client.get_task(task_id)
        if not task_row:
            return None, set()
        segments = db_client.get_segments(task_id)
        assets = db_client.list_task_assets(task_id)
        return task_row, collect_task_paths(task_row, segments, assets)

    def _finish_delete(self, task_id: str, delete_files: bool, initial_paths) -> bool:
        task_row, latest_paths = self._snapshot_task_paths(task_id)
        paths = set(initial_paths) | latest_paths
        if task_row and not self.delete_task(task_id):
            raise RuntimeError(f"[{task_id}] 删除任务数据库记录失败")

        report = DeletionReport(0, 0, [], [])
        if delete_files:
            try:
                report = delete_task_files(paths, self._allowed_storage_roots())
            except Exception as exc:
                logger.exception("[%s] 任务记录已删除，但文件清理异常", task_id)
                report = DeletionReport(0, 0, [], [str(exc)])
            else:
                if report.skipped_paths:
                    logger.warning(
                        "[%s] 跳过存储根目录外的任务路径: %s",
                        task_id,
                        report.skipped_paths,
                    )
                if report.failed_paths:
                    logger.error(
                        "[%s] 任务记录已删除，但文件清理失败: %s",
                        task_id,
                        report.failed_paths,
                    )
                logger.info(
                    "[%s] 文件清理完成: files=%s directories=%s",
                    task_id,
                    report.deleted_files,
                    report.deleted_directories,
                )

        with self.deletion_lock:
            self.deleted_task_ids.add(task_id)
            self.deletion_reports[task_id] = report
        return True

    def get_deletion_report(self, task_id: str):
        with self.deletion_lock:
            return self.deletion_reports.get(task_id)

    def _run_deferred_delete(self, task_id: str, delete_files: bool, initial_paths):
        try:
            while not task_runtime.wait_until_stopped(task_id, timeout=30):
                logger.info("[%s] 等待运行任务停止后继续删除", task_id)
            self._finish_delete(task_id, delete_files, initial_paths)
        except Exception:
            logger.exception("[%s] 延迟删除任务失败", task_id)
        finally:
            with self.deletion_lock:
                self.deletion_claims.discard(task_id)
            task_runtime.finish_delete(task_id)

    def request_delete(self, task_id: str, delete_files: bool = False) -> str:
        """Claim one full deletion and defer it while execution is active."""
        with self.deletion_lock:
            if task_id in self.deleted_task_ids:
                return "deleted"
            if task_id in self.deletion_claims:
                return "deleting"
            if not task_runtime.claim_delete(task_id):
                return "deleting"
            task_row, initial_paths = self._snapshot_task_paths(task_id)
            if not task_row:
                task_runtime.finish_delete(task_id)
                return "missing"
            self.deletion_claims.add(task_id)

        try:
            if not db_client.set_task_deletion_intent(task_id, delete_files):
                raise RuntimeError(f"[{task_id}] 保存文件删除意图失败")
            if not db_client.update_task_status(
                task_id,
                TaskStatus.DELETING.value,
                task_row.get("current_step"),
                task_row.get("error"),
            ):
                raise RuntimeError(f"[{task_id}] 标记任务删除中失败")
            with self.lock:
                task = self.tasks.get(task_id)
                if task:
                    task.status = TaskStatus.DELETING
            redis_client.delete_task(task_id)

            if task_runtime.is_running(task_id):
                task_runtime.request_cancel(task_id)
                thread = Thread(
                    target=self._run_deferred_delete,
                    args=(task_id, delete_files, initial_paths),
                )
                thread.daemon = True
                thread.start()
                return "deleting"

            self._finish_delete(task_id, delete_files, initial_paths)
            return "deleted"
        except Exception:
            with self.deletion_lock:
                self.deletion_claims.discard(task_id)
            task_runtime.finish_delete(task_id)
            raise
        finally:
            if not task_runtime.is_running(task_id):
                with self.deletion_lock:
                    if task_id in self.deleted_task_ids:
                        self.deletion_claims.discard(task_id)
                        task_runtime.finish_delete(task_id)

    def complete_deleting_tasks(self, limit: int = 200) -> int:
        """Finish deletions persisted by a previous server process."""
        completed = 0
        rows = db_client.list_tasks(
            status=TaskStatus.DELETING.value,
            limit=limit,
            offset=0,
        )
        for row in rows:
            delete_files = bool(row.get("delete_files_on_delete"))
            if self.request_delete(row["task_id"], delete_files=delete_files) == "deleted":
                completed += 1
        return completed


# 全局任务管理器实例
task_manager = TaskManager()

"""
异步任务执行器
将 pipeline 包装为异步任务，支持进度回调
"""

import logging
import json
import time
import zipfile
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Any, Dict, List, Optional
from .task_manager import task_manager, TaskStatus
from .task_runtime import TaskCancellation, TaskCancelled, task_runtime
from src.core.pipeline import VideoEditorPipeline
from src.database import db_client
from src.draft.atomic_finalize import (
    FinalizeMedia,
    build_finalize_archive,
    cleanup_finalize_workspace,
    materialize_finalize_media,
    prepare_finalize_workspace,
    publish_finalize_workspace,
    validate_staged_draft,
)
from src.utils.local_uploader import LocalUploader
from src.utils.rendering import canvas_for_ratio, normalize_ratio
from src.config import Config
from src.api.error_model import ErrorCode, SafeError, classify_exception, make_safe_error

logger = logging.getLogger(__name__)


class RecoverableTaskError(RuntimeError):
    """Raised when saved checkpoints can be used to resume the task."""

    def __init__(self, message: str, safe_error: Optional[SafeError] = None):
        super().__init__(message)
        self.safe_error = safe_error


class FinalizeAssetsMissingError(RuntimeError):
    """Raised when finalize detects media loss after the request was accepted."""


def _require_checkpoint(saved: bool, description: str) -> None:
    if not saved:
        raise RecoverableTaskError(f"{description}保存失败")


def _task_subtitle_options(task_row: Dict) -> Dict:
    try:
        value = json.loads(task_row.get("subtitle_options_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _task_generation_options(task_row: Dict) -> Dict:
    """Merge a task snapshot over global defaults and clamp runtime bounds."""
    defaults = Config.generation_config()
    try:
        saved = json.loads(task_row.get("generation_options_json") or "{}")
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    merged = {**defaults, **saved}
    for key, default in (
        ("prompt_concurrency", 4),
        ("image_concurrency", 8),
        ("tts_concurrency", 1),
    ):
        merged[key] = max(1, min(8, int(merged.get(key, default) or default)))
    merged["retry_count"] = max(
        0, min(5, int(merged.get("retry_count", 2) or 0))
    )
    merged["retry_interval_seconds"] = max(
        1, min(60, int(merged.get("retry_interval_seconds", 5) or 5))
    )
    return merged


def _asset_snapshot_json(
    task_row: Dict,
    segment: Dict,
    asset_type: str,
    *,
    prompt: Optional[str] = None,
    voice_type: Optional[str] = None,
    tts_options: Optional[Dict] = None,
) -> str:
    """Capture the inputs that produced one immutable media version."""
    payload = {
        "asset_type": asset_type,
        "text": segment.get("text") or "",
        "ratio": normalize_ratio(task_row.get("ratio") or "16:9"),
        "style": task_row.get("style") or "",
        "generation_options": _task_generation_options(task_row),
    }
    if asset_type == "image":
        payload["prompt"] = prompt if prompt is not None else segment.get("image_prompt") or ""
    else:
        payload["voice_type"] = voice_type or segment.get("audio_voice_type") or task_row.get("voice_type")
        payload["tts_options"] = tts_options or {}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _accepted_image_prompt(path: Any, fallback: str) -> str:
    """Use the exact provider-accepted prompt when the adapter supplied it."""
    value = getattr(path, "submitted_prompt", None)
    return str(value or fallback or "")


def _image_generation_metadata(path: Any) -> Optional[str]:
    if not bool(getattr(path, "fallback_used", False)):
        return None
    return json.dumps(
        {
            "content_policy_fallback": True,
            "requested_prompt": str(getattr(path, "requested_prompt", "") or ""),
            "submitted_prompt": str(getattr(path, "submitted_prompt", "") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _segment_tts_options(segment: Dict) -> Dict:
    try:
        value = json.loads(segment.get("audio_tts_options_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_failure(error: BaseException, provider: Optional[str] = None) -> SafeError:
    carried = getattr(error, "safe_error", None)
    if isinstance(carried, SafeError):
        return carried
    safe = classify_exception(error, provider=provider)
    if safe.code is ErrorCode.UNKNOWN and provider in {
        "llm",
        "agnes",
        "tts",
        "doubao",
        "mimo",
    }:
        return make_safe_error(ErrorCode.PROVIDER_ERROR, provider=provider)
    if safe.code is ErrorCode.UNKNOWN and (
        provider == "local_storage"
        or (provider == "draft_builder" and isinstance(error, OSError))
    ):
        return make_safe_error(ErrorCode.DISK, provider="local_storage")
    return safe


def _safe_error_fields(error: BaseException, provider: Optional[str] = None) -> Dict:
    safe = _safe_failure(error, provider)
    return {
        "error": safe.safe_message,
        "error_code": safe.code.value,
        "error_meta": safe.metadata(),
    }


def _upload_warning(error: Exception) -> SafeError:
    safe = _safe_failure(error, provider="local_storage")
    if safe.code is not ErrorCode.DISK:
        safe = make_safe_error(ErrorCode.DISK, provider="local_storage")
    return safe


def _preserved_upload_warning(error: Optional[str]) -> Optional[str]:
    if error and error == "本地文件写入失败，请检查磁盘空间和目录权限。":
        return error
    return None


def _stops_new_dispatch(safe_error: SafeError) -> bool:
    return safe_error.code in {
        ErrorCode.AUTH,
        ErrorCode.CONFIG_MISSING,
        ErrorCode.DISK,
        ErrorCode.RATE_LIMIT,
    }


@dataclass
class ResumeWork:
    prompt_indexes: List[int]
    image_indexes: List[int]
    audio_indexes: List[int]
    media_paths: List[Optional[str]]
    voiceover_files: List[Optional[str]]


def _completed_local_path(segment: dict, asset_type: str) -> Optional[str]:
    path = segment.get(f"{asset_type}_path")
    resolved = _resolve_local_path(path)
    if (
        segment.get(f"{asset_type}_status") == "completed"
        and path
        and resolved
        and resolved.is_file()
    ):
        return path
    return None


def _resolve_local_path(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    candidate = Path(path)
    if candidate.exists():
        return candidate
    if not candidate.is_absolute():
        rooted = Config.BASE_DIR / candidate
        if rooted.exists():
            return rooted
    return candidate


def build_resume_work(segments: List[dict]) -> ResumeWork:
    prompt_indexes = []
    image_indexes = []
    audio_indexes = []
    media_paths = []
    voiceover_files = []

    for index, segment in enumerate(segments):
        if not segment.get("image_prompt"):
            prompt_indexes.append(index)
        image_path = _completed_local_path(segment, "image")
        audio_path = _completed_local_path(segment, "audio")
        media_paths.append(image_path)
        voiceover_files.append(audio_path)
        if image_path is None:
            image_indexes.append(index)
        if audio_path is None:
            audio_indexes.append(index)

    return ResumeWork(
        prompt_indexes=prompt_indexes,
        image_indexes=image_indexes,
        audio_indexes=audio_indexes,
        media_paths=media_paths,
        voiceover_files=voiceover_files,
    )


def _safe_project_name(name: str) -> str:
    safe_name = (
        (name or "")
        .strip()
        .replace(" ", "_")
        .replace("\n", "_")
        .replace("/", "_")
        .replace("\\", "_")
    )[:20]
    return "task" if safe_name in {"", ".", ".."} else safe_name


def _task_output_dir(task_id: str, draft_name: str, segments: List[dict]) -> Path:
    for segment in segments:
        for field in ("image_path", "audio_path"):
            raw_path = segment.get(field)
            if not raw_path:
                continue
            path = Path(raw_path)
            if path.parent.name in {"images", "voiceovers", "audio"}:
                resolved = _resolve_local_path(str(path)) or path
                return resolved.parent.parent
    task_root = Path("output") / task_id
    candidate = task_root / _safe_project_name(draft_name)
    try:
        candidate.resolve().relative_to(task_root.resolve())
    except ValueError:
        return task_root / "task"
    return candidate


def _bounded_concurrency(value, total: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    parsed = max(1, min(8, parsed))
    return min(parsed, max(1, total))


def _asset_counts(paths: list) -> tuple[int, int]:
    completed = sum(1 for path in paths if path)
    return completed, max(0, len(paths) - completed)


def _operation_targets_with_state(
    operation_id: str,
    state: str,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
    error_meta: Optional[Dict] = None,
) -> List[Dict]:
    operation = db_client.get_task_operation(operation_id) or {}
    targets = [dict(target) for target in operation.get("targets") or []]
    if not targets:
        targets = [{"asset_type": "draft"}]
    for target in targets:
        target["status"] = state
        target["error"] = error
        if error_code:
            target["error_code"] = error_code
            target["error_meta"] = error_meta
    return targets


class TaskExecutor:
    """任务执行器"""

    def __init__(self, pipeline_factory=VideoEditorPipeline):
        self.pipeline_factory = pipeline_factory

    def execute_task(self, task_id: str, theme: str, style: str, length: int, voice_type: Optional[str] = None, ratio: str = "16:9", input_mode: str = "script") -> bool:
        """在后台线程中执行任务"""
        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            logger.info(f"[{task_id}] 任务已在执行，跳过重复启动")
            return False

        task_row = db_client.get_task(task_id)
        original_status = (task_row or {}).get("status") or TaskStatus.PENDING.value
        original_phase = (task_row or {}).get("workflow_phase") or "pending"
        original_step = (task_row or {}).get("current_step") or "pending"
        if task_row:
            if not db_client.update_task_workflow(
                task_id,
                original_phase,
                status=TaskStatus.PROCESSING.value,
                current_step=original_step,
            ):
                task_runtime.finish(task_id, cancellation)
                return False
            task_manager.invalidate_task_cache(task_id)

        thread = Thread(
            target=self._run_registered_task,
            args=(
                cancellation,
                task_id,
                theme,
                style,
                length,
                voice_type,
                ratio,
                input_mode,
            ),
        )
        thread.daemon = True
        try:
            thread.start()
        except Exception:
            if task_row:
                db_client.update_task_workflow(
                    task_id,
                    original_phase,
                    status=original_status,
                    current_step=original_step,
                )
                task_manager.invalidate_task_cache(task_id)
            task_runtime.finish(task_id, cancellation)
            raise
        logger.info(f"[{task_id}] 启动后台任务线程")
        return True

    def _run_registered_task(
        self,
        cancellation: TaskCancellation,
        task_id: str,
        theme: str,
        style: str,
        length: int,
        voice_type: Optional[str],
        ratio: str,
        input_mode: str,
    ) -> None:
        try:
            self._run_task(
                task_id,
                theme,
                style,
                length,
                voice_type,
                ratio,
                input_mode,
                cancellation=cancellation,
            )
        finally:
            task_runtime.finish(task_id, cancellation)

    def cancel_task(self, task_id: str, timeout: float = 30) -> bool:
        if not task_runtime.request_cancel(task_id):
            return True
        return task_runtime.wait_until_stopped(task_id, timeout)

    def resume_task(self, task_id: str) -> str:
        task_row = db_client.get_task(task_id)
        if not task_row:
            return "not_recoverable"
        if task_row.get("status") == TaskStatus.COMPLETED.value:
            return "already_completed"
        if task_runtime.is_running(task_id):
            if task_row.get("status") in {
                TaskStatus.INTERRUPTED.value,
                TaskStatus.FAILED.value,
            }:
                db_client.update_task_workflow(
                    task_id,
                    task_row.get("workflow_phase") or "planning",
                    status=TaskStatus.PROCESSING.value,
                    current_step=task_row.get("current_step") or "pending",
                )
                task_manager.invalidate_task_cache(task_id)
            return "already_running"

        segments = db_client.get_segments(task_id)
        if task_row.get("status") not in {
            TaskStatus.INTERRUPTED.value,
            TaskStatus.FAILED.value,
        } or not (task_row.get("theme") or task_row.get("script_text") or segments):
            return "not_recoverable"

        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            if task_runtime.is_running(task_id):
                return "already_running"
            return "not_recoverable"
        original_status = task_row.get("status")
        original_phase = task_row.get("workflow_phase") or "planning"
        original_step = task_row.get("current_step") or "pending"
        active_phase = (
            "generating_assets"
            if original_phase in {"assets_requested", "generating_assets", "ready"}
            else "planning"
        )
        active_step = (
            original_step
            if original_step not in {"pending", "awaiting_confirmation", "completed"}
            else "image_generation" if active_phase == "generating_assets" else "text_generation"
        )
        if not db_client.update_task_workflow(
            task_id,
            active_phase,
            status=TaskStatus.PROCESSING.value,
            current_step=active_step,
        ):
            task_runtime.finish(task_id, cancellation)
            return "not_recoverable"
        task_manager.invalidate_task_cache(task_id)
        thread = Thread(
            target=self._run_registered_task,
            args=(
                cancellation,
                task_id,
                task_row["theme"],
                task_row["style"],
                task_row["length"],
                task_row.get("voice_type"),
                task_row.get("ratio", "16:9"),
                task_row.get("input_mode", "script"),
            ),
        )
        thread.daemon = True
        try:
            thread.start()
        except Exception:
            db_client.update_task_workflow(
                task_id,
                original_phase,
                status=original_status,
                current_step=original_step,
            )
            task_manager.invalidate_task_cache(task_id)
            task_runtime.finish(task_id, cancellation)
            raise
        return "started"

    def continue_task(self, task_id: str) -> str:
        """Continue a review-first task from its persisted plan checkpoint."""
        task_row = db_client.get_task(task_id)
        if not task_row or not db_client.get_segments(task_id):
            return "not_recoverable"
        if task_runtime.is_running(task_id):
            return "already_running"
        if task_row.get("execution_mode") != "review_first":
            return "not_review_first"
        if task_row.get("status") not in {
            TaskStatus.AWAITING_CONFIRMATION.value,
            TaskStatus.INTERRUPTED.value,
            TaskStatus.FAILED.value,
        }:
            return "invalid_state"

        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            return "already_running" if task_runtime.is_running(task_id) else "not_recoverable"
        original_status = task_row.get("status")
        original_phase = task_row.get("workflow_phase") or "awaiting_confirmation"
        original_step = task_row.get("current_step") or "awaiting_confirmation"
        if not db_client.update_task_workflow(
            task_id,
            "assets_requested",
            status=TaskStatus.PROCESSING.value,
            current_step="image_generation",
        ):
            task_runtime.finish(task_id, cancellation)
            return "not_recoverable"
        task_manager.invalidate_task_cache(task_id)
        thread = Thread(
            target=self._run_registered_task,
            args=(
                cancellation,
                task_id,
                task_row["theme"],
                task_row["style"],
                task_row["length"],
                task_row.get("voice_type"),
                task_row.get("ratio", "16:9"),
                task_row.get("input_mode", "script"),
            ),
        )
        thread.daemon = True
        try:
            thread.start()
        except Exception:
            db_client.update_task_workflow(
                task_id,
                original_phase,
                status=original_status,
                current_step=original_step,
            )
            task_manager.invalidate_task_cache(task_id)
            task_runtime.finish(task_id, cancellation)
            raise
        return "started"

    def retry_assets(self, task_id: str, operation_id: str, targets: List[Dict]) -> str:
        """Run only the explicitly persisted image/audio targets."""
        if not db_client.get_task(task_id) or not targets:
            return "not_recoverable"
        if task_runtime.is_running(task_id):
            return "already_running"
        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            return "already_running" if task_runtime.is_running(task_id) else "not_recoverable"
        processing_targets = []
        for target in targets:
            item = dict(target)
            item["status"] = "processing"
            item["error"] = None
            processing_targets.append(item)
        if not db_client.start_task_operation(
            operation_id,
            task_id,
            operation_targets=processing_targets,
            workflow_phase="repairing_assets",
            current_step="asset_repair",
            mark_asset_targets_processing=True,
        ):
            task_runtime.finish(task_id, cancellation)
            return "not_recoverable"
        task_manager.invalidate_task_cache(task_id)
        thread = Thread(
            target=self._run_registered_asset_retry,
            args=(cancellation, task_id, operation_id, processing_targets),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as error:
            safe = _safe_failure(error)
            failed_targets = []
            for target in processing_targets:
                item = dict(target)
                item["status"] = "failed"
                item["error"] = safe.safe_message
                item["error_code"] = safe.code.value
                item["error_meta"] = safe.metadata()
                failed_targets.append(item)
                if item.get("mode") != "replace":
                    db_client.update_segment(
                        task_id,
                        int(item["segment_index"]),
                        {
                            f"{item['asset_type']}_status": "failed",
                            f"{item['asset_type']}_error": item["error"],
                            f"{item['asset_type']}_error_code": safe.code.value,
                            f"{item['asset_type']}_error_meta": safe.metadata(),
                        },
                    )
            db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=failed_targets,
                completed_count=0,
                failed_count=len(failed_targets),
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="generating_assets",
                current_step="asset_repair",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            )
            task_runtime.finish(task_id, cancellation)
            task_manager.invalidate_task_cache(task_id)
            raise
        return "started"

    def regenerate_prompt(
        self, task_id: str, operation_id: str, target: Dict
    ) -> str:
        """Regenerate exactly one persisted image prompt without generating media."""
        if not db_client.get_task(task_id) or target.get("asset_type") != "prompt":
            return "not_recoverable"
        if task_runtime.is_running(task_id):
            return "already_running"
        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            return "already_running" if task_runtime.is_running(task_id) else "not_recoverable"
        processing_target = {
            **dict(target),
            "status": "processing",
            "error": None,
        }
        if not db_client.start_task_operation(
            operation_id,
            task_id,
            operation_targets=[processing_target],
            workflow_phase="planning",
            current_step="image_prompt_generation",
            mark_prompt_targets_processing=True,
        ):
            task_runtime.finish(task_id, cancellation)
            return "not_recoverable"
        task_manager.invalidate_task_cache(task_id)
        thread = Thread(
            target=self._run_registered_prompt_regeneration,
            args=(cancellation, task_id, operation_id, processing_target),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as error:
            safe = _safe_failure(error, provider="llm")
            failed_target = {
                **processing_target,
                "status": "failed",
                "error": safe.safe_message,
                "error_code": safe.code.value,
                "error_meta": safe.metadata(),
            }
            db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=[failed_target],
                completed_count=0,
                failed_count=1,
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="planning",
                current_step="image_prompt_generation",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            )
            task_runtime.finish(task_id, cancellation)
            task_manager.invalidate_task_cache(task_id)
            raise
        return "started"

    def _run_registered_prompt_regeneration(
        self,
        cancellation: TaskCancellation,
        task_id: str,
        operation_id: str,
        target: Dict,
    ) -> None:
        try:
            self._run_prompt_regeneration(
                task_id, operation_id, target, cancellation=cancellation
            )
        except Exception as error:
            safe = _safe_failure(error, provider="llm")
            failed_target = {
                **target,
                "status": "failed",
                "error": safe.safe_message,
                "error_code": safe.code.value,
                "error_meta": safe.metadata(),
            }
            if (
                safe.code is not ErrorCode.CONFLICT
                and target.get("segment_index") is not None
            ):
                db_client.update_segment(
                    task_id,
                    int(target["segment_index"]),
                    {
                        "prompt_status": "failed",
                        "prompt_error": safe.safe_message,
                        "prompt_error_code": safe.code.value,
                        "prompt_error_meta": safe.metadata(),
                    },
                )
            db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=[failed_target],
                completed_count=0,
                failed_count=1,
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="planning",
                current_step="image_prompt_generation",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            )
            task_manager.invalidate_task_cache(task_id)
        finally:
            task_runtime.finish(task_id, cancellation)

    def _run_prompt_regeneration(
        self,
        task_id: str,
        operation_id: str,
        target: Dict,
        cancellation: Optional[TaskCancellation] = None,
    ) -> None:
        """Execute one prompt request and persist only that segment's prompt state."""
        task_row = db_client.get_task(task_id) or {}
        segments = db_client.get_segments(task_id)
        index = int(target.get("segment_index"))
        segment = next(
            (item for item in segments if int(item.get("segment_index")) == index),
            None,
        )
        if not task_row or not segment:
            safe = make_safe_error(ErrorCode.CONFLICT)
            failed_target = {
                **target,
                "status": "failed",
                "error": safe.safe_message,
                "error_code": safe.code.value,
                "error_meta": safe.metadata(),
            }
            db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=[failed_target],
                completed_count=0,
                failed_count=1,
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="planning",
                current_step="image_prompt_generation",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            )
            task_manager.invalidate_task_cache(task_id)
            return

        parts = str(task_row.get("style") or "").split("|", 2)
        visual_style = parts[1] if len(parts) > 1 and parts[1] else "写实风格"
        visual_style_suffix = parts[2] if len(parts) > 2 and parts[2] else None
        output_dir = _task_output_dir(
            task_id,
            _safe_project_name(task_row.get("name") or task_row.get("theme") or task_id),
            segments,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        ratio = normalize_ratio(task_row.get("ratio") or "16:9")
        pipeline = self.pipeline_factory(
            theme=task_row.get("theme") or "",
            output_dir=str(output_dir),
            canvas=canvas_for_ratio(ratio),
            subtitle_options=_task_subtitle_options(task_row),
            generation_options=_task_generation_options(task_row),
        )
        try:
            if cancellation:
                cancellation.raise_if_cancelled()
            summary = str(
                task_row.get("summary")
                or task_row.get("script_text")
                or task_row.get("theme")
                or ""
            )[:500]
            prompt = pipeline.image_prompt_agent.generate_prompt(
                segment_text=segment.get("text") or "",
                summary=summary,
                style=visual_style_suffix or visual_style,
                aspect_ratio=ratio,
            )
            if cancellation:
                cancellation.raise_if_cancelled()
            prompt = str(prompt or "").strip()
            if not prompt:
                raise RuntimeError("提示词生成结果为空")
            resolved_image = _resolve_local_path(segment.get("image_path"))
            image_exists = bool(resolved_image and resolved_image.is_file())
            next_version = db_client.update_segment_plan(
                task_id,
                index,
                {
                    "image_prompt": prompt,
                    "prompt_status": "completed",
                    "prompt_error": None,
                    "prompt_error_code": None,
                    "prompt_error_meta": None,
                    "prompt_manual": 0,
                    "prompt_needs_review": 0,
                    "image_status": "stale" if image_exists else "pending",
                    "image_error": None,
                    "image_error_code": None,
                    "image_error_meta": None,
                },
                expected_plan_version=target.get("plan_version"),
            )
            if next_version == -1:
                raise RecoverableTaskError(
                    "预案已发生变化，本次提示词未保存",
                    make_safe_error(ErrorCode.CONFLICT),
                )
            if next_version is None:
                raise RecoverableTaskError("提示词保存失败")

            refreshed = db_client.get_segments(task_id)
            all_prompts_ready = bool(refreshed) and all(
                str(item.get("image_prompt") or "").strip() for item in refreshed
            )
            has_any_media = any(
                item.get("image_path") or item.get("audio_path") for item in refreshed
            )
            review_first = task_row.get("execution_mode") == "review_first"
            origin_waiting = target.get("origin_status") == TaskStatus.AWAITING_CONFIRMATION.value
            if not all_prompts_ready:
                task_status = TaskStatus.INTERRUPTED.value
                workflow_phase = "planning"
                current_step = "image_prompt_generation"
            elif review_first and (origin_waiting or not has_any_media):
                task_status = TaskStatus.AWAITING_CONFIRMATION.value
                workflow_phase = "awaiting_confirmation"
                current_step = "awaiting_confirmation"
            else:
                task_status = TaskStatus.INTERRUPTED.value
                workflow_phase = "generating_assets"
                current_step = "asset_repair"
            completed_target = {
                **target,
                "status": "completed",
                "error": None,
                "plan_version": next_version,
            }
            if not db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="completed",
                operation_targets=[completed_target],
                completed_count=1,
                failed_count=0,
                operation_error="",
                task_status=task_status,
                workflow_phase=workflow_phase,
                current_step=current_step,
                task_error=None,
                clear_result=True,
            ):
                logger.error("[%s] 提示词操作终态提交失败", task_id)
        except Exception as error:
            safe = _safe_failure(error, provider="llm")
            if safe.code is not ErrorCode.CONFLICT:
                db_client.update_segment(
                    task_id,
                    index,
                    {
                        "prompt_status": "failed",
                        "prompt_error": safe.safe_message,
                        "prompt_error_code": safe.code.value,
                        "prompt_error_meta": safe.metadata(),
                    },
                )
            failed_target = {
                **target,
                "status": "failed",
                "error": safe.safe_message,
                "error_code": safe.code.value,
                "error_meta": safe.metadata(),
            }
            if not db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=[failed_target],
                completed_count=0,
                failed_count=1,
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="planning",
                current_step="image_prompt_generation",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            ):
                logger.error("[%s] 提示词失败终态提交失败", task_id)
        finally:
            task_manager.invalidate_task_cache(task_id)

    def finalize_task(self, task_id: str, operation_id: str) -> str:
        """Build the draft from existing files without calling generation models."""
        if task_runtime.is_running(task_id):
            return "already_running"
        cancellation = task_runtime.begin(task_id)
        if cancellation is None:
            return "already_running" if task_runtime.is_running(task_id) else "not_recoverable"
        operation = db_client.get_task_operation(operation_id) or {}
        targets = [dict(target) for target in operation.get("targets") or []]
        for target in targets:
            target["status"] = "processing"
            target["error"] = None
        if not db_client.start_task_operation(
            operation_id,
            task_id,
            operation_targets=targets,
            workflow_phase="finalizing",
            current_step="draft_building",
        ):
            task_runtime.finish(task_id, cancellation)
            return "not_recoverable"
        task_manager.invalidate_task_cache(task_id)
        thread = Thread(
            target=self._run_registered_finalize,
            args=(cancellation, task_id, operation_id),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as error:
            safe = _safe_failure(error)
            db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=[
                    {
                        **target,
                        "status": "failed",
                        "error": safe.safe_message,
                        "error_code": safe.code.value,
                        "error_meta": safe.metadata(),
                    }
                    for target in targets
                ],
                completed_count=0,
                failed_count=max(1, len(targets)),
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="finalizing",
                current_step="finalize_failed",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            )
            task_runtime.finish(task_id, cancellation)
            task_manager.invalidate_task_cache(task_id)
            raise
        return "started"

    def _run_registered_asset_retry(
        self,
        cancellation: TaskCancellation,
        task_id: str,
        operation_id: str,
        targets: List[Dict],
    ) -> None:
        try:
            self._run_asset_retry(task_id, operation_id, targets, cancellation)
        except Exception as error:
            safe = _safe_failure(error)
            logger.error("[%s] 精确素材重试异常: %s", task_id, safe.safe_message)
            failed_targets = []
            for target in targets:
                item = dict(target)
                if item.get("status") != "completed":
                    item["status"] = "failed"
                    item["error"] = safe.safe_message
                    item["error_code"] = safe.code.value
                    item["error_meta"] = safe.metadata()
                    if item.get("mode") != "replace":
                        db_client.update_segment(
                            task_id,
                            int(item["segment_index"]),
                            {
                                f"{item['asset_type']}_status": "failed",
                                f"{item['asset_type']}_error": safe.safe_message,
                                f"{item['asset_type']}_error_code": safe.code.value,
                                f"{item['asset_type']}_error_meta": safe.metadata(),
                            },
                        )
                failed_targets.append(item)
            completed_count = sum(
                1 for item in failed_targets if item.get("status") == "completed"
            )
            failed_count = sum(
                1 for item in failed_targets if item.get("status") == "failed"
            )
            db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="partial_failed" if completed_count else "failed",
                operation_targets=failed_targets,
                completed_count=completed_count,
                failed_count=failed_count,
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase="generating_assets",
                current_step="asset_repair",
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
                clear_result=completed_count > 0,
            )
            task_manager.invalidate_task_cache(task_id)
        finally:
            task_runtime.finish(task_id, cancellation)

    def _run_asset_retry(
        self,
        task_id: str,
        operation_id: str,
        targets: List[Dict],
        cancellation: Optional[TaskCancellation] = None,
    ) -> None:
        task_row = db_client.get_task(task_id) or {}
        review_first = task_row.get("execution_mode") == "review_first"
        segments = db_client.get_segments(task_id)
        result_row = task_row.get("result") or {}
        previous_draft = _resolve_local_path(result_row.get("draft_path"))
        had_ready_draft = bool(previous_draft and previous_draft.exists())
        by_index = {int(item["segment_index"]): item for item in segments}
        draft_name = _safe_project_name(task_row.get("name") or task_row.get("theme") or task_id)
        output_dir = _task_output_dir(task_id, draft_name, segments)
        output_dir.mkdir(parents=True, exist_ok=True)
        ratio = normalize_ratio(task_row.get("ratio") or "16:9")
        canvas = canvas_for_ratio(ratio)
        pipeline = self.pipeline_factory(
            theme=task_row.get("theme") or "",
            output_dir=str(output_dir),
            canvas=canvas,
            subtitle_options=_task_subtitle_options(task_row),
            generation_options=_task_generation_options(task_row),
        )
        parts = str(task_row.get("style") or "").split("|", 2)
        visual_style = parts[1] if len(parts) > 1 and parts[1] else "写实风格"
        visual_style_suffix = parts[2] if len(parts) > 2 and parts[2] else None
        try:
            task_tts_options = json.loads(task_row.get("tts_options_json") or "{}")
        except (TypeError, ValueError):
            task_tts_options = {}
        generation_config = _task_generation_options(task_row)
        image_targets = [item for item in targets if item.get("asset_type") == "image"]
        audio_targets = [item for item in targets if item.get("asset_type") == "audio"]
        uploader = LocalUploader()

        def generate_target(target: Dict) -> Dict:
            if cancellation:
                cancellation.raise_if_cancelled()
            index = int(target["segment_index"])
            segment = by_index[index]
            stamp = f"{int(time.time())}_{operation_id[:8]}"
            if target["asset_type"] == "image":
                prompt = str(segment.get("image_prompt") or "").strip()
                if not prompt:
                    raise ValueError("图片缺少提示词，不能直接重试")
                path = pipeline.image_generator.generate(
                    prompt,
                    index=index,
                    style=visual_style,
                    style_suffix=visual_style_suffix,
                    filename=f"seg_{index:03d}_repair_{stamp}",
                    width=canvas["width"],
                    height=canvas["height"],
                )
                resolved = _resolve_local_path(path)
                if not resolved or not resolved.is_file():
                    raise RuntimeError("图片生成完成但本地文件不存在")
                warning = None
                try:
                    url = uploader.upload(path, f"{task_id}/images/{resolved.name}")
                except Exception as error:
                    url = None
                    warning = _upload_warning(error)
                    logger.warning("[%s] 分镜 %s 图片本地归档失败", task_id, index)
                return {
                    "path": path,
                    "url": url,
                    "prompt": _accepted_image_prompt(path, prompt),
                    "metadata_json": _image_generation_metadata(path),
                    "warning": warning,
                }

            options = dict(task_tts_options)
            if segment.get("audio_tts_options_json"):
                try:
                    options.update(json.loads(segment["audio_tts_options_json"]))
                except (TypeError, ValueError):
                    pass
            if isinstance(target.get("tts_options"), dict):
                options.update(target["tts_options"])
            voice = (
                target.get("voice_type")
                or segment.get("audio_voice_type")
                or task_row.get("voice_type")
            )
            path = pipeline.voiceover_generator.generate(
                segment.get("text") or "",
                filename=f"seg_{index:03d}_repair_{stamp}",
                voice_type=voice,
                speed_level=options.get("speed_level"),
                volume_ratio=options.get("volume_ratio"),
                style_prompt=options.get("style_prompt"),
            )
            resolved = _resolve_local_path(path)
            if not resolved or not resolved.is_file():
                raise RuntimeError("配音生成完成但本地文件不存在")
            warning = None
            try:
                url = uploader.upload(path, f"{task_id}/audio/{resolved.name}")
            except Exception as error:
                url = None
                warning = _upload_warning(error)
                logger.warning("[%s] 分镜 %s 配音本地归档失败", task_id, index)
            return {
                "path": path,
                "url": url,
                "voice": voice,
                "options": options,
                "warning": warning,
            }

        completed = 0
        failed = 0
        working_targets = [dict(item) for item in targets]
        future_map = {}
        image_executor = ThreadPoolExecutor(
            max_workers=_bounded_concurrency(
                generation_config.get("image_concurrency", 8), len(image_targets)
            )
        ) if image_targets else None
        audio_executor = ThreadPoolExecutor(
            max_workers=_bounded_concurrency(
                generation_config.get("tts_concurrency", 1), len(audio_targets)
            )
        ) if audio_targets else None
        try:
            positions_by_type = {
                asset_type: iter([
                    position
                    for position, target in enumerate(working_targets)
                    if target["asset_type"] == asset_type
                ])
                for asset_type in ("image", "audio")
            }
            halted_types = set()
            fatal_errors = {}

            def submit_next(asset_type: str) -> bool:
                if asset_type in halted_types:
                    return False
                if cancellation and cancellation.is_cancelled():
                    return False
                try:
                    position = next(positions_by_type[asset_type])
                except StopIteration:
                    return False
                executor = image_executor if asset_type == "image" else audio_executor
                future_map[
                    executor.submit(generate_target, working_targets[position])
                ] = position
                return True

            for asset_type, concurrency in (
                (
                    "image",
                    _bounded_concurrency(
                        generation_config.get("image_concurrency", 8), len(image_targets)
                    ),
                ),
                (
                    "audio",
                    _bounded_concurrency(
                        generation_config.get("tts_concurrency", 1), len(audio_targets)
                    ),
                ),
            ):
                if not (image_targets if asset_type == "image" else audio_targets):
                    continue
                for _ in range(concurrency):
                    if not submit_next(asset_type):
                        break

            while future_map:
                done, _ = wait(future_map, return_when=FIRST_COMPLETED)
                for future in done:
                    position = future_map.pop(future)
                    target = working_targets[position]
                    asset_type = target["asset_type"]
                    index = int(target["segment_index"])
                    segment = by_index[index]
                    try:
                        result = future.result()
                        if asset_type == "image":
                            storage_warning = result.get("warning")
                            updates = {
                                "image_path": result["path"],
                                "image_url": result["url"],
                                "image_status": "completed",
                                "image_error": storage_warning.safe_message if storage_warning else None,
                                "image_error_code": storage_warning.code.value if storage_warning else None,
                                "image_error_meta": storage_warning.metadata() if storage_warning else None,
                            }
                            asset_kwargs = {
                                "prompt": result["prompt"],
                                "text": segment.get("text"),
                                "metadata_json": result.get("metadata_json"),
                            }
                        else:
                            storage_warning = result.get("warning")
                            updates = {
                                "audio_path": result["path"],
                                "audio_url": result["url"],
                                "audio_status": "completed",
                                "audio_error": storage_warning.safe_message if storage_warning else None,
                                "audio_error_code": storage_warning.code.value if storage_warning else None,
                                "audio_error_meta": storage_warning.metadata() if storage_warning else None,
                                "audio_voice_type": result["voice"],
                                "audio_tts_options_json": json.dumps(result["options"], ensure_ascii=False),
                            }
                            asset_kwargs = {
                                "text": segment.get("text"),
                                "voice_type": result["voice"],
                                "metadata_json": json.dumps(
                                    {"tts_options": result["options"]}, ensure_ascii=False
                                ),
                            }
                        if not db_client.update_segment(task_id, index, updates):
                            raise RuntimeError("素材结果保存失败")
                        asset_record = db_client.save_task_asset(
                            task_id=task_id,
                            asset_type=asset_type,
                            source="regenerated" if target.get("mode") == "replace" else "generated",
                            path=result["path"],
                            url=result["url"],
                            segment_index=index,
                            label=(
                                f"AI 生成 · 分镜 {index + 1}"
                                if asset_type == "image"
                                else f"配音 · 分镜 {index + 1}"
                            ),
                            status="completed",
                            error_message=(
                                storage_warning.safe_message if storage_warning else None
                            ),
                            operation_id=operation_id,
                            snapshot_json=_asset_snapshot_json(
                                task_row,
                                segment,
                                asset_type,
                                prompt=result.get("prompt"),
                                voice_type=result.get("voice"),
                                tts_options=result.get("options"),
                            ),
                            **asset_kwargs,
                        )
                        if not asset_record:
                            raise RuntimeError("素材版本保存失败")
                        db_client.backfill_selected_asset_ids(task_id)
                        target["status"] = "completed"
                        target["error"] = None
                        target["storage_warning"] = (
                            storage_warning.metadata() if storage_warning else None
                        )
                        completed += 1
                    except TaskCancelled:
                        raise
                    except Exception as error:
                        safe = _safe_failure(
                            error,
                            "agnes" if asset_type == "image" else "tts",
                        )
                        target["status"] = "failed"
                        target["error"] = safe.safe_message
                        target["error_code"] = safe.code.value
                        target["error_meta"] = safe.metadata()
                        failed += 1
                        if _stops_new_dispatch(safe):
                            fatal_errors[asset_type] = safe
                            halted_types.add(asset_type)
                            if safe.code is ErrorCode.DISK:
                                fatal_errors.update({"image": safe, "audio": safe})
                                halted_types.update({"image", "audio"})
                        if target.get("mode") != "replace":
                            db_client.update_segment(
                                task_id,
                                index,
                                {
                                    f"{asset_type}_status": "failed",
                                    f"{asset_type}_error": safe.safe_message,
                                    f"{asset_type}_error_code": safe.code.value,
                                    f"{asset_type}_error_meta": safe.metadata(),
                                },
                            )
                    db_client.update_task_operation(
                        operation_id,
                        targets=working_targets,
                        completed_count=completed,
                        failed_count=failed,
                    )
                    submit_next(asset_type)

            # Targets not yet submitted after a system-level failure become
            # explicit recoverable failures; no provider call is made for them.
            for target in working_targets:
                asset_type = target["asset_type"]
                if target.get("status") != "processing" or asset_type not in fatal_errors:
                    continue
                safe = fatal_errors[asset_type]
                target.update({
                    "status": "failed",
                    "error": safe.safe_message,
                    "error_code": safe.code.value,
                    "error_meta": safe.metadata(),
                    "not_dispatched": True,
                })
                failed += 1
                if target.get("mode") != "replace":
                    db_client.update_segment(
                        task_id,
                        int(target["segment_index"]),
                        {
                            f"{asset_type}_status": "failed",
                            f"{asset_type}_error": safe.safe_message,
                            f"{asset_type}_error_code": safe.code.value,
                            f"{asset_type}_error_meta": safe.metadata(),
                        },
                    )
            db_client.update_task_operation(
                operation_id,
                targets=working_targets,
                completed_count=completed,
                failed_count=failed,
            )
        except TaskCancelled as error:
            safe = _safe_failure(error)
            for target in working_targets:
                if target.get("status") == "processing":
                    target["status"] = "failed"
                    target["error"] = safe.safe_message
                    target["error_code"] = safe.code.value
                    target["error_meta"] = safe.metadata()
                    if target.get("mode") != "replace":
                        db_client.update_segment(
                            task_id,
                            int(target["segment_index"]),
                            {
                                f"{target['asset_type']}_status": "failed",
                                f"{target['asset_type']}_error": safe.safe_message,
                                f"{target['asset_type']}_error_code": safe.code.value,
                                f"{target['asset_type']}_error_meta": safe.metadata(),
                            },
                        )
            failed = sum(1 for item in working_targets if item.get("status") == "failed")
        finally:
            if image_executor:
                image_executor.shutdown(wait=True)
            if audio_executor:
                audio_executor.shutdown(wait=True)

        refreshed = db_client.get_segments(task_id)
        missing = sum(
            1
            for segment in refreshed
            for asset_type in ("image", "audio")
            if _completed_local_path(segment, asset_type) is None
        )
        if cancellation and cancellation.is_cancelled():
            phase = "generating_assets"
            status = TaskStatus.INTERRUPTED.value
            current_step = "asset_repair"
            task_error = "素材修复已取消，完成内容已保存"
            operation_state = "interrupted"
            operation_error = task_error
        elif missing:
            phase = "generating_assets"
            status = TaskStatus.INTERRUPTED.value
            current_step = "asset_repair"
            task_error = f"仍有 {missing} 个素材待修复"
            operation_state = "partial_failed" if completed else "failed"
            operation_error = task_error
        elif completed:
            phase = "awaiting_finalization" if review_first else "generating_assets"
            status = (
                TaskStatus.AWAITING_FINALIZATION.value
                if review_first
                else TaskStatus.INTERRUPTED.value
            )
            current_step = "awaiting_finalization" if review_first else "draft_building"
            task_error = None
            operation_state = "partial_failed" if failed else "completed"
            operation_error = "部分替换失败，已保留旧素材" if failed else ""
        elif had_ready_draft:
            phase = "ready"
            status = TaskStatus.COMPLETED.value
            current_step = "completed"
            task_error = None
            operation_state = "failed"
            operation_error = "替换失败，已继续使用原素材"
        else:
            phase = "awaiting_finalization" if review_first else "generating_assets"
            status = (
                TaskStatus.AWAITING_FINALIZATION.value
                if review_first
                else TaskStatus.INTERRUPTED.value
            )
            current_step = "awaiting_finalization" if review_first else "draft_building"
            task_error = None
            operation_state = "failed"
            operation_error = "素材替换失败"
        first_failed_target = next(
            (item for item in working_targets if item.get("error_code")),
            None,
        )
        operation_error_code = (
            first_failed_target.get("error_code") if first_failed_target else None
        )
        operation_error_meta = (
            first_failed_target.get("error_meta") if first_failed_target else None
        )
        if not db_client.finish_task_operation(
            operation_id,
            task_id,
            operation_state=operation_state,
            operation_targets=working_targets,
            completed_count=completed,
            failed_count=failed,
            operation_error=operation_error,
            operation_error_code=operation_error_code,
            operation_error_meta=operation_error_meta,
            task_status=status,
            workflow_phase=phase,
            current_step=current_step,
            task_error=task_error,
            task_error_code=operation_error_code if task_error else None,
            task_error_meta=operation_error_meta if task_error else None,
            clear_result=completed > 0,
        ):
            logger.error("[%s] 素材操作终态提交失败", task_id)
        task_manager.invalidate_task_cache(task_id)

    def _run_registered_finalize(
        self,
        cancellation: TaskCancellation,
        task_id: str,
        operation_id: str,
    ) -> None:
        try:
            self._run_finalize_task(task_id, operation_id, cancellation)
        finally:
            task_runtime.finish(task_id, cancellation)

    def _run_finalize_task(
        self,
        task_id: str,
        operation_id: str,
        cancellation: Optional[TaskCancellation] = None,
    ) -> None:
        workspace = None
        published = False
        committed = False
        try:
            task_row = db_client.get_task(task_id) or {}
            segments = db_client.get_segments(task_id)
            if not segments:
                raise FinalizeAssetsMissingError("没有可构建的分镜")
            media = []
            missing_assets = []
            for segment in segments:
                image = _completed_local_path(segment, "image")
                audio = _completed_local_path(segment, "audio")
                segment_number = int(segment.get("segment_index") or 0) + 1
                if not image:
                    missing_assets.append(f"分镜 {segment_number} 图片")
                if not audio:
                    missing_assets.append(f"分镜 {segment_number} 配音")
                if not image or not audio:
                    continue
                media.append(FinalizeMedia(
                    segment_index=int(segment.get("segment_index") or 0),
                    image_path=_resolve_local_path(image),
                    audio_path=_resolve_local_path(audio),
                ))
            if missing_assets:
                raise FinalizeAssetsMissingError(
                    "素材缺失：" + "、".join(missing_assets)
                )
            if cancellation:
                cancellation.raise_if_cancelled()
            draft_name = _safe_project_name(task_row.get("name") or task_row.get("theme") or task_id)
            source_root = _task_output_dir(task_id, draft_name, segments)
            workspace = prepare_finalize_workspace(
                source_root, draft_name, operation_id
            )
            media_paths, audio_paths = materialize_finalize_media(workspace, media)
            canvas = canvas_for_ratio(normalize_ratio(task_row.get("ratio") or "16:9"))
            pipeline = self.pipeline_factory(
                theme=task_row.get("theme") or "",
                output_dir=str(workspace.staging_dir),
                canvas=canvas,
                subtitle_options=_task_subtitle_options(task_row),
                generation_options=_task_generation_options(task_row),
            )
            pipeline.draft_builder.build(
                segments=[segment.get("text") or "" for segment in segments],
                media_paths=media_paths,
                draft_name=draft_name,
                voiceover_files=audio_paths,
                output_dir=str(workspace.staging_dir),
            )
            validate_staged_draft(workspace.staging_dir)
            if cancellation:
                cancellation.raise_if_cancelled()
            zip_path = build_finalize_archive(workspace.staging_dir, draft_name)
            draft_url = None
            try:
                draft_url = LocalUploader().upload(
                    str(zip_path),
                    f"{task_id}/drafts/{operation_id}/{zip_path.name}",
                )
            except Exception as error:
                warning = _upload_warning(error)
                logger.warning("[%s] 草稿包本地归档失败: %s", task_id, warning.safe_message)
            if cancellation:
                cancellation.raise_if_cancelled()
            published_path = publish_finalize_workspace(workspace)
            published = True
            # Publishing and the SQLite commit form a short, non-cancellable section.
            committed = db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="completed",
                operation_targets=_operation_targets_with_state(
                    operation_id, "completed"
                ),
                completed_count=1,
                failed_count=0,
                operation_error="",
                task_status=TaskStatus.COMPLETED.value,
                workflow_phase="ready",
                current_step="completed",
                task_error=None,
                result={
                    "draft_path": str(published_path),
                    "draft_url": draft_url,
                    "video_url": None,
                    "segments_count": len(segments),
                    "total_duration": None,
                },
            )
            if not committed:
                stored_operation = db_client.get_task_operation(operation_id) or {}
                stored_task = db_client.get_task(task_id) or {}
                stored_result = stored_task.get("result") or {}
                committed = (
                    stored_operation.get("state") == "completed"
                    and stored_result.get("draft_path") == str(published_path)
                )
            if not committed:
                raise RecoverableTaskError("草稿结果提交失败，原有草稿未受影响")
        except Exception as error:
            if isinstance(error, FinalizeAssetsMissingError):
                safe = SafeError(
                    code=ErrorCode.CONFLICT,
                    retryable=False,
                    safe_message=str(error),
                )
            else:
                safe = _safe_failure(error, provider="draft_builder")
            if workspace and not committed:
                try:
                    cleanup_finalize_workspace(
                        workspace, remove_published=published
                    )
                except Exception as cleanup_error:
                    cleanup_safe = _safe_failure(cleanup_error, provider="local_storage")
                    logger.error(
                        "[%s] 未提交草稿版本清理失败: %s",
                        task_id,
                        cleanup_safe.safe_message,
                    )
            missing_assets = isinstance(error, FinalizeAssetsMissingError)
            next_phase = "generating_assets" if missing_assets else "finalizing"
            next_step = "asset_repair" if missing_assets else "finalize_failed"
            committed = db_client.finish_task_operation(
                operation_id,
                task_id,
                operation_state="failed",
                operation_targets=_operation_targets_with_state(
                    operation_id,
                    "failed",
                    safe.safe_message,
                    safe.code.value,
                    safe.metadata(),
                ),
                completed_count=0,
                failed_count=1,
                operation_error=safe.safe_message,
                operation_error_code=safe.code.value,
                operation_error_meta=safe.metadata(),
                task_status=TaskStatus.INTERRUPTED.value,
                workflow_phase=next_phase,
                current_step=next_step,
                task_error=safe.safe_message,
                task_error_code=safe.code.value,
                task_error_meta=safe.metadata(),
            )
            if not committed:
                logger.error("[%s] 草稿失败状态提交失败", task_id)
        finally:
            if workspace and not published:
                try:
                    cleanup_finalize_workspace(workspace)
                except Exception as cleanup_error:
                    cleanup_safe = _safe_failure(cleanup_error, provider="local_storage")
                    logger.warning(
                        "[%s] 草稿 staging 清理失败: %s",
                        task_id,
                        cleanup_safe.safe_message,
                    )
            task_manager.invalidate_task_cache(task_id)

    def run_inline(
        self, task_id: str, cancellation: Optional[TaskCancellation] = None
    ) -> None:
        task_row = db_client.get_task(task_id)
        if not task_row:
            return
        self._run_task(
            task_id,
            task_row["theme"],
            task_row["style"],
            task_row["length"],
            task_row.get("voice_type"),
            task_row.get("ratio", "16:9"),
            task_row.get("input_mode", "script"),
            cancellation=cancellation,
        )

    def _run_task(self, task_id: str, theme: str, style: str, length: int, voice_type: Optional[str] = None, ratio: str = "16:9", input_mode: str = "script", cancellation: Optional[TaskCancellation] = None):
        """执行任务的实际逻辑"""
        task = task_manager.get_task(task_id)
        if not task:
            logger.error(f"[{task_id}] 任务不存在")
            return
        tts_options = dict(getattr(task, "tts_options", {}) or {})

        started_at = time.time()
        pipeline = None
        segments_count = 0
        draft_path = None
        video_path = None
        image_failures = []
        voice_failures = []
        segment_db_indexes = []

        try:
            if cancellation:
                cancellation.raise_if_cancelled()

            task_row = db_client.get_task(task_id) or {}
            execution_mode = task_row.get("execution_mode", "full")
            workflow_phase = task_row.get("workflow_phase", "pending")
            if execution_mode == "review_first":
                workflow_phase = (
                    "generating_assets"
                    if workflow_phase in {"assets_requested", "generating_assets", "ready"}
                    else "planning"
                )
                task.workflow_phase = workflow_phase
                db_client.update_task_workflow(task_id, workflow_phase)

            # 更新任务状态为处理中
            task_manager.update_task_status(task_id, TaskStatus.PROCESSING)
            logger.info(f"[{task_id}] ========== 开始执行任务 ==========")
            input_mode = "theme" if input_mode == "theme" else "script"
            logger.info(f"[{task_id}] 输入长度: {len(theme)}, 输入模式: {input_mode}, 风格: {style}, 目标字数: {length}")
            ratio = normalize_ratio(ratio or getattr(task, "ratio", "16:9"))
            canvas = canvas_for_ratio(ratio)
            logger.info(f"[{task_id}] 视频比例: {ratio}, 画布: {canvas['width']}x{canvas['height']}")

            # 解析 style 字段：格式为 "文章风格|画面风格|自定义画面prompt后缀"
            parts = (style or "").split("|", 2)
            text_style = parts[0] if len(parts) > 0 and parts[0] else "温暖感人"
            visual_style = parts[1] if len(parts) > 1 and parts[1] else "写实风格"
            visual_style_suffix = parts[2] if len(parts) > 2 and parts[2] else None
            visual_prompt_style = visual_style_suffix or visual_style
            logger.info(f"[{task_id}] 文章风格: {text_style}, 画面风格: {visual_style}")

            # 创建草稿名称和目录
            task_row = db_client.get_task(task_id) or task_row
            persisted_segments = db_client.get_segments(task_id)
            draft_base = task_row.get("name") or task.name or theme[:20]
            draft_name = _safe_project_name(draft_base)
            draft_dir = _task_output_dir(task_id, draft_name, persisted_segments)
            draft_dir.mkdir(parents=True, exist_ok=True)

            # 创建 pipeline，指定草稿目录
            pipeline = self.pipeline_factory(
                theme=theme,
                output_dir=str(draft_dir),
                canvas=canvas,
                subtitle_options=_task_subtitle_options(task_row),
                generation_options=_task_generation_options(task_row),
            )

            # 步骤 1: 文案改写 / 主题生成
            logger.info(f"[{task_id}] [1/6] 开始生成/改写脚本...")
            task.start_step("text_generation")
            if task_row.get("script_text"):
                pipeline.article = task_row["script_text"]
                pipeline.summary = task_row.get("summary") or ""
            elif persisted_segments:
                pipeline.article = "\n".join(
                    row["text"] for row in persisted_segments
                )
                pipeline.summary = task_row.get("summary") or theme
                _require_checkpoint(
                    db_client.save_task_checkpoint(
                        task_id,
                        script_text=pipeline.article,
                        summary=pipeline.summary,
                        input_mode=input_mode,
                    ),
                    "旧任务脚本检查点",
                )
            elif input_mode == "script" and task_row.get("script_policy") == "verbatim":
                pipeline.article = theme
                pipeline.summary = theme[:500]
                _require_checkpoint(
                    db_client.save_task_checkpoint(
                        task_id,
                        script_text=pipeline.article,
                        summary=pipeline.summary,
                        input_mode=input_mode,
                    ),
                    "原文脚本检查点",
                )
            else:
                rewrite_result = pipeline.script_rewriter.rewrite(
                    theme,
                    style=text_style,
                    target_length=length,
                    input_mode=input_mode,
                )
                pipeline.article = rewrite_result["script"]
                pipeline.summary = rewrite_result["summary"]
                _require_checkpoint(
                    db_client.save_task_checkpoint(
                        task_id,
                        script_text=pipeline.article,
                        summary=pipeline.summary,
                        input_mode=input_mode,
                    ),
                    "脚本检查点",
                )
            logger.info(f"[{task_id}] [1/6] 脚本生成完成，共 {len(pipeline.article)} 字")
            logger.info(f"[{task_id}] 内容总结: {pipeline.summary}")
            task.complete_step("text_generation")
            if cancellation:
                cancellation.raise_if_cancelled()

            # 步骤 2: 短节奏分段，约 20 字一段，对应更密的画面切换
            logger.info(f"[{task_id}] [2/6] 开始短节奏分段...")
            task.current_step = "segmentation"
            db_client.update_task_status(task_id, "processing", "segmentation")
            if persisted_segments:
                pipeline.segments = [row["text"] for row in persisted_segments]
            else:
                pipeline.segments = pipeline.text_segmenter.split(pipeline.article)
                initial_segments = [
                    {
                        "segment_index": i,
                        "text": segment,
                        "image_prompt": "",
                        "image_status": "pending",
                        "audio_status": "pending",
                        "prompt_status": "pending",
                        "prompt_manual": 0,
                        "prompt_needs_review": 0,
                    }
                    for i, segment in enumerate(pipeline.segments)
                ]
                _require_checkpoint(
                    db_client.save_segments(task_id, initial_segments),
                    "初始分镜检查点",
                )
                persisted_segments = db_client.get_segments(task_id)
            segment_db_indexes = [
                row["segment_index"] for row in persisted_segments
            ]
            segments_count = len(pipeline.segments)
            logger.info(f"[{task_id}] [2/6] 分段完成，共 {segments_count} 段")
            generation_config = _task_generation_options(task_row)
            prompt_concurrency = _bounded_concurrency(
                generation_config.get("prompt_concurrency", 4), segments_count
            )
            tts_concurrency = _bounded_concurrency(
                generation_config.get("tts_concurrency", 1), segments_count
            )
            image_concurrency = _bounded_concurrency(
                generation_config.get("image_concurrency", 8), segments_count
            )
            logger.info(
                f"[{task_id}] 生成并发配置: 提示词={prompt_concurrency}, "
                f"配音={tts_concurrency}, 生图={image_concurrency}"
            )
            if cancellation:
                cancellation.raise_if_cancelled()

            # 步骤 3: 逐段生成图像 prompts
            logger.info(f"[{task_id}] [3/6] 开始逐段生成图像描述...")
            task.start_step("image_prompt_generation")
            resume_work = build_resume_work(persisted_segments)
            image_prompts = [row.get("image_prompt") or "" for row in persisted_segments]

            def generate_prompt_item(i: int):
                if cancellation:
                    cancellation.raise_if_cancelled()
                seg = pipeline.segments[i]
                try:
                    prompt = pipeline.image_prompt_agent.generate_prompt(
                        segment_text=seg,
                        summary=pipeline.summary,
                        style=visual_prompt_style,
                        aspect_ratio=ratio,
                    )
                    return {"status": "success", "prompt": prompt, "error": None}
                except TaskCancelled:
                    raise
                except Exception as error:
                    safe = _safe_failure(error, provider="llm")
                    return {
                        "status": "failed",
                        "prompt": "",
                        "error": safe.safe_message,
                        "safe_error": safe,
                    }

            pending_prompt_indexes = iter(resume_work.prompt_indexes)
            prompt_failures = []
            prompt_cancelled = False
            prompt_dispatch_stopped = False
            completed_prompts = segments_count - len(resume_work.prompt_indexes)

            def submit_next_prompt(executor, futures) -> bool:
                nonlocal prompt_cancelled, prompt_dispatch_stopped
                if cancellation and cancellation.is_cancelled():
                    prompt_cancelled = True
                    return False
                if prompt_dispatch_stopped:
                    return False
                try:
                    index = next(pending_prompt_indexes)
                except StopIteration:
                    return False
                _require_checkpoint(
                    db_client.update_segment(
                        task_id,
                        segment_db_indexes[index],
                        {"prompt_status": "processing", "prompt_error": None},
                    ),
                    f"分镜 {segment_db_indexes[index]} 提示词开始检查点",
                )
                futures[executor.submit(generate_prompt_item, index)] = index
                return True

            with ThreadPoolExecutor(max_workers=prompt_concurrency) as prompt_executor:
                prompt_futures = {}
                for _ in range(prompt_concurrency):
                    if not submit_next_prompt(prompt_executor, prompt_futures):
                        break

                while prompt_futures:
                    done, _ = wait(prompt_futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        i = prompt_futures.pop(future)
                        try:
                            result = future.result()
                        except TaskCancelled:
                            prompt_cancelled = True
                            result = {"status": "cancelled", "prompt": "", "error": "任务已取消"}

                        if result["status"] == "success":
                            prompt = result["prompt"]
                            image_prompts[i] = prompt
                            prompt_updates = {
                                "image_prompt": prompt,
                                "image_error": None,
                                "prompt_status": "completed",
                                "prompt_error": None,
                                "prompt_manual": 0,
                                "prompt_needs_review": 0,
                            }
                            if _completed_local_path(persisted_segments[i], "image") is None:
                                prompt_updates["image_status"] = "pending"
                            _require_checkpoint(
                                db_client.update_segment(
                                    task_id, segment_db_indexes[i], prompt_updates
                                ),
                                f"分镜 {segment_db_indexes[i]} 提示词检查点",
                            )
                        elif result["status"] == "failed":
                            safe = result["safe_error"]
                            prompt_failures.append({
                                "index": i,
                                "error": safe.safe_message,
                                "error_code": safe.code.value,
                                "error_meta": safe.metadata(),
                                "safe_error": safe,
                            })
                            if _stops_new_dispatch(safe):
                                prompt_dispatch_stopped = True
                            prompt_error_updates = {
                                "image_error": safe.safe_message,
                                "image_error_code": safe.code.value,
                                "image_error_meta": safe.metadata(),
                                "prompt_status": "failed",
                                "prompt_error": safe.safe_message,
                                "prompt_error_code": safe.code.value,
                                "prompt_error_meta": safe.metadata(),
                            }
                            if _completed_local_path(persisted_segments[i], "image") is None:
                                prompt_error_updates["image_status"] = "failed"
                            _require_checkpoint(
                                db_client.update_segment(
                                    task_id,
                                    segment_db_indexes[i],
                                    prompt_error_updates,
                                ),
                                f"分镜 {segment_db_indexes[i]} 提示词错误检查点",
                            )

                        completed_prompts += 1
                        task.update_step_progress(
                            "image_prompt_generation", completed_prompts, segments_count
                        )
                        logger.debug(
                            f"[{task_id}] 图像描述进度: {completed_prompts}/{segments_count}"
                        )

                    if not prompt_cancelled:
                        for _ in range(len(done)):
                            if not submit_next_prompt(prompt_executor, prompt_futures):
                                break

            pipeline.image_prompts = image_prompts
            if prompt_cancelled or (cancellation and cancellation.is_cancelled()):
                raise TaskCancelled("Task execution was cancelled during prompt generation")
            if prompt_failures:
                failed_numbers = "、".join(str(item["index"] + 1) for item in prompt_failures)
                logger.warning(f"[{task_id}] 部分图片提示词生成失败: {prompt_failures}")
                raise RecoverableTaskError(
                    f"图片提示词生成失败 [片段 {failed_numbers}]",
                    safe_error=prompt_failures[0]["safe_error"],
                )
            persisted_segments = db_client.get_segments(task_id)
            resume_work = build_resume_work(persisted_segments)
            for i, segment in enumerate(persisted_segments):
                segment_index = segment_db_indexes[i]
                for asset_type, missing_indexes in (
                    ("image", resume_work.image_indexes),
                    ("audio", resume_work.audio_indexes),
                ):
                    if i in missing_indexes:
                        continue
                    preserved_error = _preserved_upload_warning(
                        segment.get(f"{asset_type}_error")
                    )
                    _require_checkpoint(
                        db_client.update_segment(
                            task_id,
                            segment_index,
                            {
                                f"{asset_type}_status": "completed",
                                f"{asset_type}_error": preserved_error,
                            },
                        ),
                        f"分镜 {segment_index} 已有{asset_type}检查点",
                    )
                    try:
                        asset_record = db_client.save_task_asset(
                            task_id=task_id,
                            asset_type=asset_type,
                            source="generated",
                            path=segment.get(f"{asset_type}_path"),
                            url=segment.get(f"{asset_type}_url"),
                            segment_index=segment_index,
                            label=(
                                f"AI 生成 · 分镜 {i + 1}"
                                if asset_type == "image"
                                else f"配音 · 分镜 {i + 1}"
                            ),
                            prompt=(
                                segment.get("image_prompt")
                                if asset_type == "image"
                                else None
                            ),
                            text=segment.get("text"),
                            voice_type=(
                                segment.get("audio_voice_type") or voice_type
                                if asset_type == "audio"
                                else None
                            ),
                            metadata_json=(
                                segment.get("audio_tts_options_json")
                                or json.dumps({"tts_options": tts_options}, ensure_ascii=False)
                                if asset_type == "audio"
                                else None
                            ),
                            status="completed",
                            error_message=preserved_error,
                            snapshot_json=_asset_snapshot_json(
                                task_row,
                                segment,
                                asset_type,
                                prompt=segment.get("image_prompt"),
                                voice_type=segment.get("audio_voice_type") or voice_type,
                                tts_options=_segment_tts_options(segment) if asset_type == "audio" else None,
                            ),
                        )
                    except Exception as error:
                        raise RecoverableTaskError(
                            f"分镜 {segment_index} 已有{asset_type}资产检查点保存失败: {error}"
                        ) from error
                    if not asset_record:
                        raise RecoverableTaskError(
                            f"分镜 {segment_index} 已有{asset_type}资产检查点保存失败"
                        )
            db_client.backfill_selected_asset_ids(task_id)
            logger.info(f"[{task_id}] 已保存分镜和图片提示词，共 {len(persisted_segments)} 段")
            logger.info(f"[{task_id}] [3/6] 图像描述生成完成")
            task.complete_step("image_prompt_generation")
            if cancellation:
                cancellation.raise_if_cancelled()

            if execution_mode == "review_first" and workflow_phase == "planning":
                task.current_step = "awaiting_confirmation"
                task.workflow_phase = "awaiting_confirmation"
                task_manager.update_task_status(task_id, TaskStatus.AWAITING_CONFIRMATION)
                _require_checkpoint(
                    db_client.update_task_workflow(
                        task_id,
                        "awaiting_confirmation",
                        status=TaskStatus.AWAITING_CONFIRMATION.value,
                        current_step="awaiting_confirmation",
                    ),
                    "预案确认状态检查点",
                )
                logger.info(f"[{task_id}] 预案已完成，等待用户确认后生成素材")
                return

            # 步骤 4-5: 配音和生图互不依赖，并行执行；内部并发由模型配置页控制。
            logger.info(f"[{task_id}] [4-5/6] 开始并行生成配音和图像（共 {segments_count} 段）...")
            pipeline.voiceover_files = resume_work.voiceover_files
            pipeline.media_paths = resume_work.media_paths

            segment_audio_settings = []
            for segment in persisted_segments:
                segment_voice = segment.get("audio_voice_type") or voice_type
                segment_options = dict(tts_options)
                if segment.get("audio_voice_type") and segment.get("audio_tts_options_json"):
                    try:
                        parsed_segment_options = json.loads(segment["audio_tts_options_json"])
                        if isinstance(parsed_segment_options, dict):
                            segment_options.update(parsed_segment_options)
                    except (TypeError, ValueError):
                        logger.warning(
                            "[%s] 分镜 %s 的配音参数快照无效，改用全片参数",
                            task_id,
                            segment.get("segment_index"),
                        )
                segment_audio_settings.append((segment_voice, segment_options))

            def generate_voiceover_item(i: int, seg: str):
                logger.debug(f"[{task_id}] 配音进度: {i+1}/{segments_count}")
                try:
                    if tts_concurrency == 1 and i > 0:
                        time.sleep(0.5)
                    if cancellation:
                        cancellation.raise_if_cancelled()
                    segment_voice, segment_options = segment_audio_settings[i]
                    path = pipeline.voiceover_generator.generate(
                        seg,
                        filename=f"seg_{i:03d}",
                        voice_type=segment_voice,
                        speed_level=segment_options.get("speed_level"),
                        volume_ratio=segment_options.get("volume_ratio"),
                        style_prompt=segment_options.get("style_prompt"),
                    )
                    return i, {"status": "success", "path": path, "error": None}
                except TaskCancelled:
                    raise
                except Exception as e:
                    safe = _safe_failure(e, provider="tts")
                    logger.error(
                        "[%s] 音频生成失败 [片段 %s]: %s",
                        task_id,
                        i + 1,
                        safe.safe_message,
                    )
                    return i, {
                        "status": "failed",
                        "path": None,
                        "error": safe.safe_message,
                        "safe_error": safe,
                    }

            def generate_image_item(i: int, prompt: str):
                logger.debug(f"[{task_id}] 图像进度: {i+1}/{segments_count}")
                try:
                    if cancellation:
                        cancellation.raise_if_cancelled()
                    path = pipeline.image_generator.generate(
                        prompt,
                        index=i,
                        style=visual_style,
                        style_suffix=visual_style_suffix,
                        width=canvas["width"],
                        height=canvas["height"],
                    )
                    return i, {"status": "success", "path": path, "error": None}
                except TaskCancelled:
                    raise
                except Exception as e:
                    safe = _safe_failure(e, provider="agnes")
                    logger.error(
                        "[%s] 图片生成失败 [片段 %s]: %s",
                        task_id,
                        i + 1,
                        safe.safe_message,
                    )
                    return i, {
                        "status": "failed",
                        "path": None,
                        "error": safe.safe_message,
                        "safe_error": safe,
                    }

            local_uploader = LocalUploader()
            upload_ts = int(time.time())

            def persist_segment_asset(
                i: int,
                asset_type: str,
                path: str = None,
                url: str = None,
                error: Optional[SafeError] = None,
            ):
                segment_index = segment_db_indexes[i]
                upload_error = None
                if asset_type == "image" and path and Path(path).exists():
                    try:
                        image_ext = Path(path).suffix
                        storage_path = f"{task_id}/images/seg_{i:03d}_{upload_ts}{image_ext}"
                        url = local_uploader.upload(path, storage_path)
                    except Exception as e:
                        upload_error = _upload_warning(e)
                        logger.warning("[%s] 段落 %s 图片本地归档失败", task_id, i)
                elif asset_type == "audio" and path and Path(path).exists():
                    try:
                        audio_ext = Path(path).suffix
                        storage_path = f"{task_id}/audio/seg_{i:03d}_{upload_ts}{audio_ext}"
                        url = local_uploader.upload(path, storage_path)
                    except Exception as e:
                        upload_error = _upload_warning(e)
                        logger.warning("[%s] 段落 %s 音频本地归档失败", task_id, i)

                final_error = error or upload_error
                status = "failed" if error else ("completed" if path else "pending")
                updates = {}
                if asset_type == "image":
                    updates = {
                        "image_path": path,
                        "image_url": url,
                        "image_status": status,
                        "image_error": final_error.safe_message if final_error else None,
                        "image_error_code": final_error.code.value if final_error else None,
                        "image_error_meta": final_error.metadata() if final_error else None,
                    }
                    label = f"AI 生成 · 分镜 {i + 1}"
                    requested_prompt = image_prompts[i] if i < len(image_prompts) else ""
                    prompt = _accepted_image_prompt(path, requested_prompt)
                    voice = None
                else:
                    segment_voice, segment_options = segment_audio_settings[i]
                    updates = {
                        "audio_path": path,
                        "audio_url": url,
                        "audio_status": status,
                        "audio_error": final_error.safe_message if final_error else None,
                        "audio_error_code": final_error.code.value if final_error else None,
                        "audio_error_meta": final_error.metadata() if final_error else None,
                        "audio_voice_type": (
                            segment_voice
                            if persisted_segments[i].get("audio_voice_type")
                            else ""
                        ),
                        "audio_tts_options_json": json.dumps(segment_options, ensure_ascii=False),
                    }
                    label = f"配音 · 分镜 {i + 1}"
                    prompt = None
                    voice = segment_voice

                _require_checkpoint(
                    db_client.update_segment(task_id, segment_index, updates),
                    f"分镜 {segment_index} {asset_type}检查点",
                )
                asset_record = db_client.save_task_asset(
                    task_id=task_id,
                    asset_type=asset_type,
                    source="generated",
                    path=path,
                    url=url,
                    segment_index=segment_index,
                    label=label,
                    prompt=prompt,
                    text=pipeline.segments[i] if i < len(pipeline.segments) else None,
                    voice_type=voice,
                    metadata_json=(
                        json.dumps({"tts_options": segment_options}, ensure_ascii=False)
                        if asset_type == "audio"
                        else _image_generation_metadata(path)
                    ),
                    status=status,
                    error_message=final_error.safe_message if final_error else None,
                    snapshot_json=_asset_snapshot_json(
                        task_row,
                        persisted_segments[i],
                        asset_type,
                        prompt=prompt,
                        voice_type=voice,
                        tts_options=(segment_options if asset_type == "audio" else None),
                    ),
                )
                if not asset_record:
                    raise RecoverableTaskError(
                        f"分镜 {segment_index} {asset_type}资产检查点保存失败"
                    )
                db_client.backfill_selected_asset_ids(task_id)
                return upload_error

            # Image and TTS have independent provider limits, but an unwritable
            # local disk is shared infrastructure.  Once either lane detects a
            # disk failure, both lanes drain already-started requests and stop
            # submitting new paid work.
            shared_asset_stop = Event()
            shared_asset_error = {"error": None}

            def stop_all_asset_dispatch(safe: SafeError) -> None:
                if safe.code is not ErrorCode.DISK:
                    return
                shared_asset_error["error"] = safe
                shared_asset_stop.set()

            def generate_voiceovers():
                task.start_step("voiceover_generation")
                completed = segments_count - len(resume_work.audio_indexes)
                failed_items = []
                cancellation_pending = False
                dispatch_stopped = False
                dispatch_stop_error = None
                submitted_indexes = set()
                pending_indexes = iter(resume_work.audio_indexes)
                with ThreadPoolExecutor(max_workers=tts_concurrency) as voice_executor:
                    futures = {}

                    def submit_next():
                        if (
                            dispatch_stopped
                            or shared_asset_stop.is_set()
                            or (cancellation and cancellation.is_cancelled())
                        ):
                            return False
                        try:
                            i = next(pending_indexes)
                        except StopIteration:
                            return False
                        submitted_indexes.add(i)
                        futures[
                            voice_executor.submit(
                                generate_voiceover_item, i, pipeline.segments[i]
                            )
                        ] = i
                        return True

                    for _ in range(tts_concurrency):
                        if not submit_next():
                            break
                    while futures:
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in done:
                            i = futures.pop(future)
                            try:
                                _, result = future.result()
                            except TaskCancelled:
                                cancellation_pending = True
                                completed += 1
                                task.update_step_progress(
                                    "voiceover_generation", completed, segments_count
                                )
                                continue
                            if result["status"] == "success":
                                pipeline.voiceover_files[i] = result["path"]
                                storage_warning = persist_segment_asset(
                                    i, "audio", path=result["path"]
                                )
                                if storage_warning:
                                    dispatch_stopped = True
                                    dispatch_stop_error = storage_warning
                                    stop_all_asset_dispatch(storage_warning)
                            else:
                                safe = result["safe_error"]
                                failed_items.append({
                                    "index": i,
                                    "type": "audio",
                                    "error": safe.safe_message,
                                    "error_code": safe.code.value,
                                    "error_meta": safe.metadata(),
                                    "safe_error": safe,
                                })
                                if _stops_new_dispatch(safe):
                                    dispatch_stopped = True
                                    dispatch_stop_error = safe
                                    stop_all_asset_dispatch(safe)
                                pipeline.voiceover_files[i] = None
                                persist_segment_asset(i, "audio", error=safe)
                            completed += 1
                            task.update_step_progress(
                                "voiceover_generation", completed, segments_count
                            )
                        if not cancellation_pending:
                            for _ in range(len(done)):
                                if not submit_next():
                                    break
                stop_error = shared_asset_error["error"] or dispatch_stop_error
                if stop_error and not (
                    cancellation and cancellation.is_cancelled()
                ):
                    for i in resume_work.audio_indexes:
                        if i in submitted_indexes:
                            continue
                        failed_items.append({
                            "index": i,
                            "type": "audio",
                            "error": stop_error.safe_message,
                            "error_code": stop_error.code.value,
                            "error_meta": stop_error.metadata(),
                            "safe_error": stop_error,
                            "not_dispatched": True,
                        })
                        pipeline.voiceover_files[i] = None
                        persist_segment_asset(i, "audio", error=stop_error)
                if failed_items:
                    logger.warning(f"[{task_id}] 部分音频生成失败: {failed_items}")
                if not cancellation_pending:
                    task.complete_step("voiceover_generation")
                return failed_items, cancellation_pending

            def generate_images():
                task.start_step("image_generation")
                completed = segments_count - len(resume_work.image_indexes)
                failed_items = []
                cancellation_pending = False
                dispatch_stopped = False
                dispatch_stop_error = None
                submitted_indexes = set()
                pending_indexes = iter(resume_work.image_indexes)
                with ThreadPoolExecutor(max_workers=image_concurrency) as image_executor:
                    futures = {}

                    def submit_next():
                        if (
                            dispatch_stopped
                            or shared_asset_stop.is_set()
                            or (cancellation and cancellation.is_cancelled())
                        ):
                            return False
                        try:
                            i = next(pending_indexes)
                        except StopIteration:
                            return False
                        submitted_indexes.add(i)
                        futures[
                            image_executor.submit(generate_image_item, i, image_prompts[i])
                        ] = i
                        return True

                    for _ in range(image_concurrency):
                        if not submit_next():
                            break
                    while futures:
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in done:
                            i = futures.pop(future)
                            try:
                                _, result = future.result()
                            except TaskCancelled:
                                cancellation_pending = True
                                completed += 1
                                task.update_step_progress(
                                    "image_generation", completed, segments_count
                                )
                                continue
                            if result["status"] == "success":
                                pipeline.media_paths[i] = result["path"]
                                storage_warning = persist_segment_asset(
                                    i, "image", path=result["path"]
                                )
                                if storage_warning:
                                    dispatch_stopped = True
                                    dispatch_stop_error = storage_warning
                                    stop_all_asset_dispatch(storage_warning)
                            else:
                                safe = result["safe_error"]
                                failed_items.append({
                                    "index": i,
                                    "type": "image",
                                    "error": safe.safe_message,
                                    "error_code": safe.code.value,
                                    "error_meta": safe.metadata(),
                                    "safe_error": safe,
                                })
                                if _stops_new_dispatch(safe):
                                    dispatch_stopped = True
                                    dispatch_stop_error = safe
                                    stop_all_asset_dispatch(safe)
                                pipeline.media_paths[i] = None
                                persist_segment_asset(i, "image", error=safe)
                            completed += 1
                            task.update_step_progress(
                                "image_generation", completed, segments_count
                            )
                        if not cancellation_pending:
                            for _ in range(len(done)):
                                if not submit_next():
                                    break
                stop_error = shared_asset_error["error"] or dispatch_stop_error
                if stop_error and not (
                    cancellation and cancellation.is_cancelled()
                ):
                    for i in resume_work.image_indexes:
                        if i in submitted_indexes:
                            continue
                        failed_items.append({
                            "index": i,
                            "type": "image",
                            "error": stop_error.safe_message,
                            "error_code": stop_error.code.value,
                            "error_meta": stop_error.metadata(),
                            "safe_error": stop_error,
                            "not_dispatched": True,
                        })
                        pipeline.media_paths[i] = None
                        persist_segment_asset(i, "image", error=stop_error)
                if failed_items:
                    logger.warning(f"[{task_id}] 部分图片生成失败: {failed_items}")
                if not cancellation_pending:
                    task.complete_step("image_generation")
                return failed_items, cancellation_pending

            with ThreadPoolExecutor(max_workers=2) as executor:
                voice_future = executor.submit(generate_voiceovers)
                image_future = executor.submit(generate_images)
                voice_failures, voice_cancelled = voice_future.result()
                image_failures, image_cancelled = image_future.result()

            if cancellation:
                cancellation.raise_if_cancelled()
            if voice_cancelled or image_cancelled:
                raise TaskCancelled("Task execution was cancelled during asset generation")

            all_failures = voice_failures + image_failures
            if all_failures:
                logger.warning(f"[{task_id}] 资源生成部分失败，共 {len(all_failures)} 项: {all_failures}")
                raise RecoverableTaskError(
                    f"资源生成中断，共 {len(all_failures)} 项失败",
                    safe_error=all_failures[0]["safe_error"],
                )

            logger.info(f"[{task_id}] [4-5/6] 配音和图像生成完成")
            if cancellation:
                cancellation.raise_if_cancelled()

            if execution_mode == "review_first":
                task.current_step = "awaiting_finalization"
                task.workflow_phase = "awaiting_finalization"
                task_manager.update_task_status(
                    task_id, TaskStatus.AWAITING_FINALIZATION
                )
                _require_checkpoint(
                    db_client.update_task_workflow(
                        task_id,
                        "awaiting_finalization",
                        status=TaskStatus.AWAITING_FINALIZATION.value,
                        current_step="awaiting_finalization",
                    ),
                    "素材完成状态检查点",
                )
                logger.info(
                    f"[{task_id}] 图片与配音已齐全，等待用户确认后构建生产草稿"
                )
                return

            # 步骤 6: 草稿构建
            logger.info(f"[{task_id}] [6/6] 开始构建剪映草稿...")
            task.start_step("draft_building")
            draft_path = pipeline.draft_builder.build(
                segments=pipeline.segments,
                media_paths=pipeline.media_paths,
                draft_name=draft_name,
                voiceover_files=pipeline.voiceover_files,
                output_dir=str(draft_dir),
            )
            logger.info(f"[{task_id}] [6/6] 草稿构建完成")
            task.complete_step("draft_building")
            if cancellation:
                cancellation.raise_if_cancelled()

            # 检查草稿目录内容
            logger.debug(f"[{task_id}] 检查草稿目录内容: {draft_dir}")
            logger.debug(f"[{task_id}] draft_path 返回值: {draft_path}")
            for item in draft_dir.rglob("*"):
                if item.is_file():
                    logger.debug(f"[{task_id}]   文件: {item.relative_to(draft_dir)} ({item.stat().st_size} bytes)")
                elif item.is_dir():
                    logger.debug(f"[{task_id}]   目录: {item.relative_to(draft_dir)}/")

            # 步骤 6: 打包并保存到本地媒体目录
            logger.info(f"[{task_id}] [6/6] 开始打包并保存到本地媒体目录...")
            zip_path = None
            draft_url = None

            try:
                # 创建临时目录用于打包
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_draft = Path(temp_dir) / draft_name
                    temp_draft.mkdir(parents=True, exist_ok=True)

                    logger.debug(f"[{task_id}] 创建临时打包目录: {temp_draft}")

                    # 复制所有文件到临时目录
                    logger.debug(f"[{task_id}] 复制草稿文件到临时目录...")
                    for item in draft_dir.rglob("*"):
                        if item.is_file():
                            rel_path = item.relative_to(draft_dir)
                            dest = temp_draft / rel_path
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, dest)
                            logger.debug(f"[{task_id}]   复制: {rel_path}")

                    # 打包临时目录
                    zip_path = draft_dir / f"{draft_name}.zip"
                    logger.info(f"[{task_id}] 开始打包: {temp_draft} -> {zip_path}")

                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for file_path in temp_draft.rglob("*"):
                            if file_path.is_file():
                                # 使用相对于临时草稿目录的路径
                                arcname = file_path.relative_to(temp_draft)
                                zf.write(file_path, arcname)
                                logger.debug(f"[{task_id}]   添加: {arcname}")

                    zip_size = zip_path.stat().st_size / 1024 / 1024
                    logger.info(f"[{task_id}] 打包完成，大小: {zip_size:.2f} MB")

                # 保存草稿包到本地媒体目录
                try:
                    draft_url = LocalUploader().upload(str(zip_path))
                    logger.info(f"[{task_id}] 草稿包保存成功: {draft_url}")
                except Exception as e:
                    safe = _upload_warning(e)
                    logger.warning(
                        "[%s] 草稿包本地归档失败（不影响本地草稿）: %s",
                        task_id,
                        safe.safe_message,
                    )

            except Exception as e:
                safe = _safe_failure(e, provider="local_storage")
                logger.warning(
                    "[%s] 打包失败（不影响草稿）: %s",
                    task_id,
                    safe.safe_message,
                )

            if cancellation:
                cancellation.raise_if_cancelled()

            # 完整 MP4 改为用户按需异步渲染，不再阻塞默认任务完成。
            video_path = None
            video_url = None

            # 保存段落数据到数据库
            logger.info(f"[{task_id}] 保存段落数据到数据库...")

            # 构建失败信息映射
            failure_map = {}
            for failure in all_failures:
                idx = failure["index"]
                ftype = failure["type"]
                if idx not in failure_map:
                    failure_map[idx] = {}
                failure_map[idx][ftype] = failure["error"]

            segments_data = []

            for i, seg_text in enumerate(pipeline.segments):
                segment_index = segment_db_indexes[i]
                image_path = pipeline.media_paths[i] if i < len(pipeline.media_paths) else None
                audio_path = pipeline.voiceover_files[i] if i < len(pipeline.voiceover_files) else None

                # 获取失败信息
                image_error = failure_map.get(i, {}).get("image")
                audio_error = failure_map.get(i, {}).get("audio")
                image_status = "failed" if image_error else ("completed" if image_path else "pending")
                audio_status = "failed" if audio_error else ("completed" if audio_path else "pending")

                image_url = None
                audio_url = None

                segment_voice, segment_options = segment_audio_settings[i]
                seg_data = {
                    'segment_index': segment_index,
                    'text': seg_text,
                    'image_prompt': image_prompts[i] if i < len(image_prompts) else "",
                    'image_path': image_path,
                    'image_url': image_url,
                    'image_status': image_status,
                    'image_error': image_error,
                    'audio_path': audio_path,
                    'audio_url': audio_url,
                    'audio_status': audio_status,
                    'audio_error': audio_error,
                    'audio_voice_type': (
                        segment_voice
                        if persisted_segments[i].get('audio_voice_type')
                        else ''
                    ),
                    'audio_tts_options_json': json.dumps(segment_options, ensure_ascii=False),
                }
                segments_data.append(seg_data)

                # 保存图片资源（包括失败状态）- 容错处理
                try:
                    db_client.save_task_asset(
                        task_id=task_id,
                        asset_type="image",
                        source="generated",
                        path=image_path,
                        url=image_url,
                        segment_index=segment_index,
                        label=f"AI 生成 · 分镜 {i + 1}",
                        prompt=seg_data["image_prompt"],
                        text=seg_text,
                        status=image_status,
                        error_message=image_error,
                        snapshot_json=_asset_snapshot_json(
                            task_row,
                            seg_data,
                            "image",
                            prompt=seg_data["image_prompt"],
                        ),
                    )
                except Exception as e:
                    safe = _safe_failure(e, provider="local_storage")
                    logger.warning(
                        "[%s] 保存图片资源失败 (段落 %s): %s",
                        task_id,
                        i,
                        safe.safe_message,
                    )

                # 保存音频资源（包括失败状态）- 容错处理
                try:
                    db_client.save_task_asset(
                        task_id=task_id,
                        asset_type="audio",
                        source="generated",
                        path=audio_path,
                        url=audio_url,
                        segment_index=segment_index,
                        label=f"配音 · 分镜 {i + 1}",
                        text=seg_text,
                        voice_type=segment_voice,
                        metadata_json=json.dumps({"tts_options": segment_options}, ensure_ascii=False),
                        status=audio_status,
                        error_message=audio_error,
                        snapshot_json=_asset_snapshot_json(
                            task_row,
                            seg_data,
                            "audio",
                            voice_type=segment_voice,
                            tts_options=segment_options,
                        ),
                    )
                except Exception as e:
                    safe = _safe_failure(e, provider="local_storage")
                    logger.warning(
                        "[%s] 保存音频资源失败 (段落 %s): %s",
                        task_id,
                        i,
                        safe.safe_message,
                    )

            _require_checkpoint(
                db_client.save_segments(task_id, segments_data),
                "最终分镜检查点",
            )
            db_client.backfill_selected_asset_ids(task_id)
            logger.info(f"[{task_id}] 段落数据保存成功，共 {len(segments_data)} 段")

            # 设置任务结果
            task_manager.set_task_result(task_id, draft_path, segments_count, draft_url, video_url)
            task.workflow_phase = "ready"
            db_client.update_task_workflow(task_id, "ready")
            task_manager.update_task_status(task_id, TaskStatus.COMPLETED)

            image_ok, image_failed = _asset_counts(pipeline.media_paths)
            audio_ok, audio_failed = _asset_counts(pipeline.voiceover_files)
            elapsed = time.time() - started_at
            logger.info(f"[{task_id}] ========== 任务完成 ==========")
            logger.info(
                f"[{task_id}] 摘要: 段落={segments_count}, 图片={image_ok}成功/{image_failed}失败, "
                f"音频={audio_ok}成功/{audio_failed}失败, 草稿={draft_path}, 视频=按需渲染, 耗时={elapsed:.1f}s"
            )

        except TaskCancelled as error:
            safe = _safe_failure(error)
            logger.info(f"[{task_id}] 任务已在阶段检查点取消")
            task_manager.mark_task_interrupted(
                task_id,
                safe.safe_message,
                error_code=safe.code.value,
                error_meta=safe.metadata(),
            )
        except RecoverableTaskError as error:
            safe = _safe_failure(error)
            logger.warning(
                "[%s] 任务在可恢复检查点中断: %s",
                task_id,
                safe.safe_message,
            )
            task_manager.mark_task_interrupted(
                task_id,
                safe.safe_message,
                error_code=safe.code.value,
                error_meta=safe.metadata(),
            )
        except Exception as e:
            safe = _safe_failure(e)
            elapsed = time.time() - started_at
            image_ok = audio_ok = image_failed = audio_failed = 0
            if pipeline:
                image_ok, image_failed = _asset_counts(getattr(pipeline, "media_paths", []) or [])
                audio_ok, audio_failed = _asset_counts(getattr(pipeline, "voiceover_files", []) or [])
                try:
                    if getattr(pipeline, "segments", None):
                        partial_segments = []
                        image_prompts = getattr(pipeline, "image_prompts", []) or []
                        media_paths = getattr(pipeline, "media_paths", []) or []
                        voiceover_files = getattr(pipeline, "voiceover_files", []) or []
                        for i, seg_text in enumerate(pipeline.segments):
                            segment_index = (
                                segment_db_indexes[i]
                                if i < len(segment_db_indexes)
                                else i
                            )
                            image_path = media_paths[i] if i < len(media_paths) else None
                            audio_path = voiceover_files[i] if i < len(voiceover_files) else None
                            partial_segments.append({
                                "segment_index": segment_index,
                                "text": seg_text,
                                "image_prompt": image_prompts[i] if i < len(image_prompts) else "",
                                "image_path": image_path,
                                "image_status": "completed" if image_path else "pending",
                                "audio_path": audio_path,
                                "audio_status": "completed" if audio_path else "pending",
                            })
                        if partial_segments:
                            db_client.save_segments(task_id, partial_segments)
                            logger.info(f"[{task_id}] 失败前已保存阶段性分镜，共 {len(partial_segments)} 段")
                except Exception as save_error:
                    save_safe = _safe_failure(save_error, provider="local_storage")
                    logger.warning(
                        "[%s] 失败后保存阶段性分镜失败: %s",
                        task_id,
                        save_safe.safe_message,
                    )
            logger.error(f"[{task_id}] ========== 任务失败 ==========")
            logger.error(f"[{task_id}] 错误类型: {type(e).__name__}")
            logger.error(f"[{task_id}] 错误分类: {safe.code.value}")
            logger.error(
                f"[{task_id}] 失败摘要: 段落={segments_count}, 图片={image_ok}成功/{image_failed}失败, "
                f"音频={audio_ok}成功/{audio_failed}失败, 草稿={draft_path}, 视频={video_path}, 耗时={elapsed:.1f}s"
            )
            logger.error(f"[{task_id}] 详细异常信息已脱敏，不记录 provider 原始响应")

            task_manager.set_task_error(
                task_id,
                safe.safe_message,
                error_code=safe.code.value,
                error_meta=safe.metadata(),
            )
            # 标记当前步骤失败
            if task.current_step in task.steps:
                task.fail_step(task.current_step, safe.safe_message)


# 全局任务执行器实例
task_executor = TaskExecutor()

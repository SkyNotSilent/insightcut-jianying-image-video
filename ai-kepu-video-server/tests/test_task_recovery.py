import errno
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from src.api import task_executor as task_executor_module
from src.api import task_manager as task_manager_module
from src.api.models import TaskStatus
from src.api.task_executor import TaskExecutor
from src.api.task_manager import Task, TaskManager
from src.api.task_runtime import TaskCancellation, TaskCancelled, TaskRuntimeRegistry
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


@pytest.fixture
def task_manager(temp_db, monkeypatch):
    monkeypatch.setattr(task_manager_module, "db_client", temp_db)
    return TaskManager()


@pytest.fixture
def executor_db(temp_db, task_manager, monkeypatch):
    monkeypatch.setattr(task_executor_module, "db_client", temp_db, raising=False)
    monkeypatch.setattr(task_executor_module, "task_manager", task_manager)
    return temp_db


class FakeScriptRewriter:
    def __init__(self, cancellation=None):
        self.calls = 0
        self.cancellation = cancellation

    def rewrite(self, *args, **kwargs):
        self.calls += 1
        if self.cancellation:
            self.cancellation.cancel()
        return {"script": "第一段。第二段。", "summary": "摘要"}


class FakeTextSegmenter:
    def __init__(self):
        self.calls = 0

    def split(self, article):
        self.calls += 1
        return ["第一段", "第二段"]


class FakePromptAgent:
    def __init__(self, failure_call=None, before_call=None):
        self.calls = 0
        self.failure_call = failure_call
        self.before_call = before_call

    def generate_prompt(self, **kwargs):
        self.calls += 1
        if self.before_call:
            self.before_call(self.calls)
        if self.calls == self.failure_call:
            raise RuntimeError("prompt failed")
        return f"prompt-{self.calls - 1}"


class ConcurrentPromptAgent:
    def __init__(self, fail_text=None, delay=0.02, expected_concurrency=None):
        self.fail_text = fail_text
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.start_barrier = (
            threading.Barrier(expected_concurrency)
            if expected_concurrency
            else None
        )

    def generate_prompt(self, **kwargs):
        segment_text = kwargs["segment_text"]
        with self.lock:
            self.calls.append(segment_text)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.start_barrier:
                try:
                    self.start_barrier.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            time.sleep(self.delay)
            if segment_text == self.fail_text:
                raise RuntimeError("prompt failed")
            return f"prompt:{segment_text}"
        finally:
            with self.lock:
                self.active -= 1


class FakeAssetGenerator:
    def __init__(self, output_dir, suffix, fail=False):
        self.output_dir = Path(output_dir)
        self.suffix = suffix
        self.fail = fail
        self.calls = []
        self.call_kwargs = []
        self.on_generate = None
        self.cancel_indexes = set()

    def generate(self, value, **kwargs):
        index = kwargs.get("index")
        if index is None:
            index = next(
                int(part)
                for part in kwargs["filename"].split("_")
                if part.isdigit()
            )
        self.calls.append(index)
        self.call_kwargs.append({"value": value, **kwargs})
        if index in self.cancel_indexes:
            raise TaskCancelled(f"cancelled item {index}")
        if self.fail:
            raise RuntimeError(f"{self.suffix} failed")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"seg_{index:03d}.{self.suffix}"
        path.write_bytes(self.suffix.encode())
        if self.on_generate:
            self.on_generate(index)
        return str(path)


class FakeDraftBuilder:
    def __init__(self, error=None, write_partial=False):
        self.calls = 0
        self.error = error
        self.write_partial = write_partial

    def build(self, **kwargs):
        self.calls += 1
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if self.error:
            if self.write_partial:
                (output_dir / "draft_content.json").write_text(
                    '{"partial":', encoding="utf-8"
                )
            raise self.error

        def material_path(path):
            candidate = Path(path)
            try:
                return candidate.relative_to(output_dir).as_posix()
            except ValueError:
                return str(candidate)

        media_paths = [
            material_path(path)
            for path in kwargs.get("media_paths") or []
        ]
        audio_paths = [
            material_path(path)
            for path in kwargs.get("voiceover_files") or []
        ]
        (output_dir / "draft_content.json").write_text(
            json.dumps(
                {
                    "tracks": [{
                        "id": "track-1",
                        "type": "video",
                        "segments": [
                            {"target_timerange": {"start": index, "duration": 1}}
                            for index, _path in enumerate(media_paths)
                        ],
                    }],
                    "materials": {
                        "videos": [{"path": path} for path in media_paths],
                        "audios": [{"path": path} for path in audio_paths],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (output_dir / "draft_meta_info.json").write_text(
            json.dumps({"draft_name": kwargs.get("draft_name")}, ensure_ascii=False),
            encoding="utf-8",
        )
        return str(output_dir)


class FakeUploader:
    def upload(self, path, storage_path=None):
        return f"/media/{storage_path or Path(path).name}"


class FailingUploader:
    def upload(self, path, storage_path=None):
        raise OSError("copy failed")


class UnauthorizedProviderError(RuntimeError):
    status_code = 401


class UnauthorizedImageGenerator(FakeAssetGenerator):
    def generate(self, value, **kwargs):
        index = kwargs.get("index", 0)
        self.calls.append(index)
        raise UnauthorizedProviderError("Authorization: Bearer secret-provider-key")


class UnauthorizedPromptAgent:
    def __init__(self):
        self.calls = 0

    def generate_prompt(self, **_kwargs):
        self.calls += 1
        raise UnauthorizedProviderError(
            "Authorization: Bearer secret-prompt-provider-key"
        )


class RateLimitedProviderError(RuntimeError):
    status_code = 429
    headers = {"Retry-After": "13"}


class RateLimitedImageGenerator(FakeAssetGenerator):
    def generate(self, value, **kwargs):
        index = kwargs.get("index", 0)
        self.calls.append(index)
        raise RateLimitedProviderError("provider rate limit")


class RateLimitedVoiceGenerator(FakeAssetGenerator):
    def generate(self, value, **kwargs):
        index = next(
            int(part) for part in kwargs["filename"].split("_") if part.isdigit()
        )
        self.calls.append(index)
        raise RateLimitedProviderError("provider rate limit")


class SuccessThenUnauthorizedImageGenerator(FakeAssetGenerator):
    def generate(self, value, **kwargs):
        index = kwargs.get("index", 0)
        if index > 0:
            self.calls.append(index)
            raise UnauthorizedProviderError("Authorization: Bearer secret-provider-key")
        return super().generate(value, **kwargs)


class DiskFullImageGenerator(FakeAssetGenerator):
    def generate(self, value, **kwargs):
        index = kwargs.get("index", 0)
        self.calls.append(index)
        raise OSError(errno.ENOSPC, "disk full")


class FakeFFmpegExporter:
    def __init__(self, **kwargs):
        pass

    def export(self, **kwargs):
        output_path = Path(kwargs["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")


class FakePipeline:
    def __init__(self, output_dir, prompt_failure_call=None, fail_assets=False,
                 cancellation=None, before_prompt_call=None):
        self.output_dir = output_dir
        self.script_rewriter = FakeScriptRewriter(cancellation=cancellation)
        self.text_segmenter = FakeTextSegmenter()
        self.image_prompt_agent = FakePromptAgent(
            prompt_failure_call, before_call=before_prompt_call
        )
        self.image_generator = FakeAssetGenerator(
            Path(output_dir) / "images", "png", fail=fail_assets
        )
        self.voiceover_generator = FakeAssetGenerator(
            Path(output_dir) / "voiceovers", "wav", fail=fail_assets
        )
        self.draft_builder = FakeDraftBuilder()
        self.article = ""
        self.summary = ""
        self.segments = []
        self.image_prompts = []
        self.media_paths = []
        self.voiceover_files = []


def create_task(db, task_id="task-1", status="pending", theme="原始内容"):
    db.create_task(
        task_id, theme, "知识科普|电影质感", 100, name="恢复测试"
    )
    if status != "pending":
        db.update_task_status(task_id, status, "image_generation")


def create_asset_resume_task(db, task_id: str, count: int = 5):
    create_task(db, task_id=task_id, status="interrupted")
    script = "。".join(f"第 {index + 1} 段" for index in range(count)) + "。"
    db.save_task_checkpoint(
        task_id,
        script_text=script,
        summary="摘要",
        input_mode="script",
    )
    db.save_segments(task_id, [
        {
            "segment_index": index,
            "text": f"第 {index + 1} 段",
            "image_prompt": f"prompt-{index}",
            "image_status": "pending",
            "audio_status": "pending",
        }
        for index in range(count)
    ])


def test_checkpoint_round_trip(temp_db):
    temp_db.create_task("task-1", "主题", "知识科普|电影质感", 100)

    assert temp_db.save_task_checkpoint(
        "task-1", script_text="完整脚本", summary="摘要", input_mode="theme"
    )

    row = temp_db.get_task("task-1")
    assert row["script_text"] == "完整脚本"
    assert row["summary"] == "摘要"
    assert row["input_mode"] == "theme"


def test_checkpoint_migration_preserves_existing_task(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL UNIQUE,
            name TEXT,
            theme TEXT NOT NULL,
            style TEXT NOT NULL DEFAULT '温暖感人',
            length INTEGER NOT NULL DEFAULT 300,
            ratio TEXT NOT NULL DEFAULT '16:9',
            voice_type TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            current_step TEXT DEFAULT 'pending',
            error TEXT,
            extract_path TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            completed_at TEXT
        );
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO tasks (task_id, theme, style, length)
        VALUES ('legacy-task', '已有任务', '知识科普|电影质感', 100);
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", db_path)

    row = SQLiteClient().get_task("legacy-task")

    assert row["theme"] == "已有任务"
    assert row["script_text"] is None
    assert row["summary"] is None
    assert row["input_mode"] == "script"


def test_checkpoint_migration_recovers_from_partial_column_application(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "partial-migration.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL UNIQUE,
            name TEXT,
            theme TEXT NOT NULL,
            style TEXT NOT NULL DEFAULT '温暖感人',
            length INTEGER NOT NULL DEFAULT 300,
            ratio TEXT NOT NULL DEFAULT '16:9',
            voice_type TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            current_step TEXT DEFAULT 'pending',
            error TEXT,
            extract_path TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            completed_at TEXT,
            script_text TEXT
        );
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO tasks (task_id, theme, style, length, script_text)
        VALUES (
            'partial-task', '部分迁移任务', '知识科普|电影质感', 100, '已保存脚本'
        );
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", db_path)

    client = SQLiteClient()
    row = client.get_task("partial-task")

    assert row is not None
    assert row["theme"] == "部分迁移任务"
    assert row["script_text"] == "已保存脚本"
    assert row["summary"] is None
    assert row["input_mode"] == "script"
    with client.get_connection() as connection:
        columns = {
            column["name"] for column in connection.execute("PRAGMA table_info(tasks)")
        }
        marker = connection.execute(
            "SELECT version FROM schema_migrations WHERE version=?",
            ("20260711_task_recovery_checkpoints",),
        ).fetchone()
    assert {"script_text", "summary", "input_mode"}.issubset(columns)
    assert marker["version"] == "20260711_task_recovery_checkpoints"


def test_legacy_script_backfill_is_ordered_idempotent_and_preserves_canonical_text(
    temp_db
):
    temp_db.create_task("legacy", "旧任务", "知识科普|电影质感", 100)
    temp_db.save_segments("legacy", [
        {"segment_index": 9, "text": "第二段。"},
        {"segment_index": 3, "text": "第一段。"},
    ])
    temp_db.create_task("canonical", "新任务", "知识科普|电影质感", 100)
    temp_db.save_task_checkpoint("canonical", script_text="已保存的原稿。")
    temp_db.save_segments("canonical", [
        {"segment_index": 0, "text": "不能覆盖原稿。"},
    ])

    with temp_db.get_connection() as connection:
        first = temp_db._backfill_legacy_script_text(connection.cursor())
        second = temp_db._backfill_legacy_script_text(connection.cursor())

    assert first == 1
    assert second == 0
    assert temp_db.get_task("legacy")["script_text"] == "第一段。\n第二段。"
    assert temp_db.get_task("legacy")["script_source"] == "reconstructed_segments"
    assert temp_db.get_task("canonical")["script_text"] == "已保存的原稿。"
    assert temp_db.get_task("canonical")["script_source"] is None


def test_segment_checkpoint_updates_only_allowed_fields(temp_db):
    temp_db.create_task("task-1", "主题", "知识科普|电影质感", 100)
    temp_db.save_segments(
        "task-1",
        [{"segment_index": 0, "text": "第一段", "image_status": "pending"}],
    )

    assert temp_db.update_segment_checkpoint(
        "task-1",
        0,
        image_prompt="画面提示词",
        image_status="completed",
        unsupported="ignored",
    )

    row = temp_db.get_segments("task-1")[0]
    assert row["image_prompt"] == "画面提示词"
    assert row["image_status"] == "completed"


def test_orphaned_processing_task_becomes_interrupted(task_manager, temp_db):
    temp_db.create_task("task-1", "主题", "知识科普|电影质感", 100)
    temp_db.update_task_status("task-1", "processing", "image_generation")
    temp_db.save_segments(
        "task-1",
        [{"segment_index": 0, "text": "已保存分镜", "image_status": "completed"}],
    )
    asset = temp_db.save_task_asset(
        "task-1", "image", "generated", segment_index=0, path="saved.png"
    )

    assert task_manager.mark_orphaned_tasks_interrupted() == 1

    row = temp_db.get_task("task-1")
    assert row["status"] == "interrupted"
    assert row["current_step"] == "image_generation"
    assert row["error"] == "服务重启导致任务中断，可继续生成"
    assert temp_db.get_segments("task-1")[0]["text"] == "已保存分镜"
    assert temp_db.list_task_assets("task-1")[0]["asset_id"] == asset["asset_id"]


def test_interrupted_status_keeps_progress_in_task_response():
    task = Task("task-1", "主题", "知识科普|电影质感", 100)
    task.status = TaskStatus.INTERRUPTED

    assert TaskStatus.DELETING.value == "deleting"
    assert task.to_response().progress is not None


def test_deleting_tasks_are_hidden_from_default_and_explicit_lists(
    task_manager, temp_db
):
    temp_db.create_task("visible-task", "可见任务", "知识科普|电影质感", 100)
    temp_db.update_task_status("visible-task", "completed", "video_synthesis")
    temp_db.create_task("deleting-task", "删除中任务", "知识科普|电影质感", 100)
    temp_db.update_task_status("deleting-task", "deleting", "image_generation")

    assert [row["task_id"] for row in task_manager.list_tasks()] == ["visible-task"]
    assert task_manager.list_tasks(status="deleting") == []
    assert [
        row["task_id"] for row in temp_db.list_tasks(status="deleting")
    ] == ["deleting-task"]


def test_deleting_tasks_do_not_consume_default_pagination(task_manager, temp_db):
    tasks = [
        ("visible-task", "completed", "2026-07-11 12:00:00"),
        ("deleting-task-1", "deleting", "2026-07-11 12:02:00"),
        ("deleting-task-2", "deleting", "2026-07-11 12:01:00"),
    ]
    for task_id, status, created_at in tasks:
        temp_db.create_task(task_id, task_id, "知识科普|电影质感", 100)
        temp_db.update_task_status(task_id, status, "image_generation")
        with temp_db.get_connection() as connection:
            connection.execute(
                "UPDATE tasks SET created_at=? WHERE task_id=?",
                (created_at, task_id),
            )

    rows = task_manager.list_tasks(limit=1, offset=0)

    assert [row["task_id"] for row in rows] == ["visible-task"]


def test_resume_selection_skips_valid_completed_assets(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    segments = [
        {
            "segment_index": 0,
            "image_prompt": "已有提示词",
            "image_path": str(image),
            "image_status": "completed",
        },
        {
            "segment_index": 1,
            "image_prompt": "",
            "image_path": None,
            "image_status": "pending",
        },
    ]

    work = task_executor_module.build_resume_work(segments)

    assert work.prompt_indexes == [1]
    assert work.image_indexes == [1]


def test_completed_status_with_missing_file_is_regenerated(tmp_path):
    segments = [
        {
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "提示词",
            "image_path": str(tmp_path / "missing.png"),
            "image_status": "completed",
            "audio_path": str(tmp_path / "missing.wav"),
            "audio_status": "completed",
        }
    ]

    work = task_executor_module.build_resume_work(segments)

    assert work.image_indexes == [0]
    assert work.audio_indexes == [0]
    assert work.media_paths == [None]
    assert work.voiceover_files == [None]


def test_scoped_asset_retry_calls_only_the_missing_image_and_preserves_other_rows(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "scoped-retry",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    images_dir = project_dir / "images"
    audio_dir = project_dir / "voiceovers"
    images_dir.mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    segments = []
    for index in range(3):
        image_path = images_dir / f"segment_{index:03d}.png"
        audio_path = audio_dir / f"seg_{index:03d}.wav"
        audio_path.write_bytes(b"audio")
        if index != 1:
            image_path.write_bytes(b"image")
        segments.append({
            "segment_index": index,
            "text": f"第 {index + 1} 段",
            "image_prompt": f"prompt-{index}",
            "image_path": str(image_path) if index != 1 else None,
            "image_status": "completed" if index != 1 else "failed",
            "image_error": None if index != 1 else "限流失败",
            "audio_path": str(audio_path),
            "audio_status": "completed",
        })
    executor_db.save_segments("scoped-retry", segments)
    executor_db.update_task_workflow(
        "scoped-retry",
        "generating_assets",
        status="interrupted",
        current_step="image_generation",
    )
    with executor_db.get_connection() as connection:
        for index in range(3):
            connection.execute(
                "UPDATE task_segments SET updated_at=? WHERE task_id=? AND segment_index=?",
                (f"2026-08-18 20:00:0{index}", "scoped-retry", index),
            )
    target = [{
        "segment_index": 1,
        "asset_type": "image",
        "mode": "retry",
        "status": "processing",
        "error": None,
    }]
    operation = executor_db.create_task_operation(
        "scoped-retry", "retry_assets", "retry-key", "snapshot", target
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"image_concurrency": 8, "tts_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        "scoped-retry", operation["operation_id"], target, TaskCancellation()
    )

    rows = executor_db.get_segments("scoped-retry")
    assert pipeline.image_generator.calls == [1]
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.draft_builder.calls == 0
    assert rows[0]["updated_at"] == "2026-08-18 20:00:00"
    assert rows[2]["updated_at"] == "2026-08-18 20:00:02"
    assert rows[0]["image_path"] == str(images_dir / "segment_000.png")
    assert rows[2]["audio_path"] == str(audio_dir / "seg_002.wav")
    assert rows[1]["image_status"] == "completed"
    assert executor_db.get_task_operation(operation["operation_id"])["state"] == "completed"
    assert executor_db.get_task("scoped-retry")["status"] == "awaiting_finalization"


def test_asset_auth_failure_stops_new_dispatch_and_persists_safe_targets(
    executor_db, tmp_path, monkeypatch
):
    task_id = "auth-stop-dispatch"
    executor_db.create_task(
        task_id,
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    targets = []
    segments = []
    for index in range(5):
        audio = tmp_path / "audio" / f"seg_{index:03d}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"wav")
        segments.append({
            "segment_index": index,
            "text": f"第 {index + 1} 段",
            "image_prompt": f"prompt-{index}",
            "image_status": "pending",
            "audio_path": str(audio),
            "audio_status": "completed",
        })
        targets.append({
            "segment_index": index,
            "asset_type": "image",
            "status": "processing",
        })
    executor_db.save_segments(task_id, segments)
    operation = executor_db.create_task_operation(
        task_id,
        "retry_assets",
        "auth-stop-key",
        "snapshot",
        targets,
    )["operation"]
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.image_generator = UnauthorizedImageGenerator(
        tmp_path / "generated" / "images", "png"
    )
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "image_concurrency": 1,
            "tts_concurrency": 1,
            "prompt_concurrency": 1,
        },
    )
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        task_id,
        operation["operation_id"],
        targets,
        TaskCancellation(),
    )

    assert pipeline.image_generator.calls == [0]
    stored_operation = executor_db.get_task_operation(operation["operation_id"])
    assert stored_operation["state"] == "failed"
    assert stored_operation["error_code"] == "auth"
    assert stored_operation["failed_count"] == 5
    assert sum(bool(item.get("not_dispatched")) for item in stored_operation["targets"]) == 4
    assert all(item.get("error_code") == "auth" for item in stored_operation["targets"])
    serialized = json.dumps(stored_operation, ensure_ascii=False)
    assert "secret-provider-key" not in serialized
    rows = executor_db.get_segments(task_id)
    assert all(row["image_error_code"] == "auth" for row in rows)


def test_prompt_auth_failure_stops_new_prompt_dispatch(
    executor_db, tmp_path, monkeypatch
):
    task_id = "prompt-auth-stop"
    executor_db.create_task(
        task_id,
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    executor_db.save_task_checkpoint(
        task_id,
        script_text="第一段。第二段。第三段。第四段。第五段。",
        summary="摘要",
        input_mode="script",
    )
    executor_db.save_segments(task_id, [
        {
            "segment_index": index,
            "text": f"第 {index + 1} 段",
            "image_prompt": "",
            "prompt_status": "pending",
            "image_status": "pending",
            "audio_status": "pending",
        }
        for index in range(5)
    ])
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.image_prompt_agent = UnauthorizedPromptAgent()
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "prompt_concurrency": 1,
            "image_concurrency": 1,
            "tts_concurrency": 1,
        },
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline).run_inline(
        task_id, cancellation=TaskCancellation()
    )

    assert pipeline.image_prompt_agent.calls == 1
    rows = executor_db.get_segments(task_id)
    assert rows[0]["prompt_status"] == "failed"
    assert rows[0]["prompt_error_code"] == "auth"
    assert all(row["prompt_status"] == "pending" for row in rows[1:])
    task = executor_db.get_task(task_id)
    assert task["status"] == "interrupted"
    assert task["error_code"] == "auth"
    assert "secret-prompt-provider-key" not in json.dumps(task, ensure_ascii=False)


def test_initial_image_rate_limit_pauses_only_images_and_keeps_tts_running(
    executor_db, tmp_path, monkeypatch
):
    task_id = "initial-image-rate-limit"
    create_asset_resume_task(executor_db, task_id)
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.image_generator = RateLimitedImageGenerator(
        tmp_path / "generated" / "images", "png"
    )
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "prompt_concurrency": 1,
            "image_concurrency": 1,
            "tts_concurrency": 1,
        },
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline).run_inline(
        task_id, cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments(task_id)
    assert pipeline.image_generator.calls == [0]
    assert pipeline.voiceover_generator.calls == [0, 1, 2, 3, 4]
    assert all(row["image_status"] == "failed" for row in rows)
    assert all(row["image_error_code"] == "rate_limit" for row in rows)
    assert all(
        row["image_error_meta"]["retry_after_seconds"] == 13 for row in rows
    )
    assert all(row["audio_status"] == "completed" for row in rows)
    assert executor_db.get_task(task_id)["error_code"] == "rate_limit"
    assert pipeline.draft_builder.calls == 0


def test_initial_tts_rate_limit_pauses_only_tts_and_keeps_images_running(
    executor_db, tmp_path, monkeypatch
):
    task_id = "initial-tts-rate-limit"
    create_asset_resume_task(executor_db, task_id)
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.voiceover_generator = RateLimitedVoiceGenerator(
        tmp_path / "generated" / "voiceovers", "wav"
    )
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "prompt_concurrency": 1,
            "image_concurrency": 1,
            "tts_concurrency": 1,
        },
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline).run_inline(
        task_id, cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments(task_id)
    assert pipeline.voiceover_generator.calls == [0]
    assert pipeline.image_generator.calls == [0, 1, 2, 3, 4]
    assert all(row["audio_status"] == "failed" for row in rows)
    assert all(row["audio_error_code"] == "rate_limit" for row in rows)
    assert all(
        row["audio_error_meta"]["retry_after_seconds"] == 13 for row in rows
    )
    assert all(row["image_status"] == "completed" for row in rows)
    assert executor_db.get_task(task_id)["error_code"] == "rate_limit"
    assert pipeline.draft_builder.calls == 0


def test_initial_image_auth_stops_new_images_and_preserves_completed_assets(
    executor_db, tmp_path, monkeypatch
):
    task_id = "initial-image-auth"
    create_asset_resume_task(executor_db, task_id)
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.image_generator = SuccessThenUnauthorizedImageGenerator(
        tmp_path / "generated" / "images", "png"
    )
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "prompt_concurrency": 1,
            "image_concurrency": 1,
            "tts_concurrency": 1,
        },
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline).run_inline(
        task_id, cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments(task_id)
    assert pipeline.image_generator.calls == [0, 1]
    assert pipeline.voiceover_generator.calls == [0, 1, 2, 3, 4]
    assert rows[0]["image_status"] == "completed"
    assert Path(rows[0]["image_path"]).is_file()
    assert all(row["image_status"] == "failed" for row in rows[1:])
    assert all(row["image_error_code"] == "auth" for row in rows[1:])
    assert all(row["audio_status"] == "completed" for row in rows)
    serialized = json.dumps(
        {
            "task": executor_db.get_task(task_id),
            "segments": rows,
        },
        ensure_ascii=False,
    )
    assert "secret-provider-key" not in serialized
    assert pipeline.draft_builder.calls == 0


def test_initial_disk_failure_stops_both_asset_lanes_after_started_work_drains(
    executor_db, tmp_path, monkeypatch
):
    task_id = "initial-shared-disk-stop"
    create_asset_resume_task(executor_db, task_id)
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    voice_started = threading.Event()

    def hold_first_voice(_index):
        voice_started.set()
        time.sleep(0.05)

    pipeline.voiceover_generator.on_generate = hold_first_voice

    def disk_full_after_voice_started(_value, **kwargs):
        index = kwargs.get("index", 0)
        pipeline.image_generator.calls.append(index)
        assert voice_started.wait(timeout=1)
        raise OSError(errno.ENOSPC, "disk full")

    pipeline.image_generator.generate = disk_full_after_voice_started
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "prompt_concurrency": 1,
            "image_concurrency": 1,
            "tts_concurrency": 1,
        },
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline).run_inline(
        task_id, cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments(task_id)
    assert pipeline.image_generator.calls == [0]
    assert pipeline.voiceover_generator.calls == [0]
    assert rows[0]["image_status"] == "failed"
    assert rows[0]["image_error_code"] == "disk"
    assert rows[0]["audio_status"] == "completed"
    assert all(row["image_error_code"] == "disk" for row in rows[1:])
    assert all(row["audio_error_code"] == "disk" for row in rows[1:])
    assert executor_db.get_task(task_id)["error_code"] == "disk"
    assert pipeline.draft_builder.calls == 0


def test_29_segment_retry_changes_only_the_twelfth_missing_image(
    executor_db, tmp_path, monkeypatch
):
    task_id = "twenty-nine-segment-retry"
    executor_db.create_task(
        task_id,
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project-29"
    images_dir = project_dir / "images"
    audio_dir = project_dir / "voiceovers"
    images_dir.mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    segments = []
    original_mtimes = {}
    for index in range(29):
        image_path = images_dir / f"segment_{index:03d}.png"
        audio_path = audio_dir / f"seg_{index:03d}.wav"
        if index != 11:
            image_path.write_bytes(f"image-{index}".encode())
            original_mtimes[(index, "image")] = image_path.stat().st_mtime_ns
        audio_path.write_bytes(f"audio-{index}".encode())
        original_mtimes[(index, "audio")] = audio_path.stat().st_mtime_ns
        segments.append({
            "segment_index": index,
            "text": f"第 {index + 1} 段",
            "image_prompt": f"prompt-{index}",
            "image_path": str(image_path) if index != 11 else None,
            "image_status": "completed" if index != 11 else "failed",
            "image_error": None if index != 11 else "provider error",
            "audio_path": str(audio_path),
            "audio_status": "completed",
        })
    executor_db.save_segments(task_id, segments)
    expected_timestamps = {}
    with executor_db.get_connection() as connection:
        for index in range(29):
            timestamp = f"2026-08-19 12:00:{index:02d}"
            expected_timestamps[index] = timestamp
            connection.execute(
                "UPDATE task_segments SET updated_at=? WHERE task_id=? AND segment_index=?",
                (timestamp, task_id, index),
            )
    target = [{
        "segment_index": 11,
        "asset_type": "image",
        "mode": "retry",
        "status": "processing",
        "error": None,
    }]
    operation = executor_db.create_task_operation(
        task_id, "retry_assets", "twenty-nine-retry-key", "snapshot", target
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"image_concurrency": 8, "tts_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        task_id, operation["operation_id"], target, TaskCancellation()
    )

    rows = executor_db.get_segments(task_id)
    assert pipeline.image_generator.calls == [11]
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.draft_builder.calls == 0
    assert rows[11]["image_status"] == "completed"
    assert Path(rows[11]["image_path"]).is_file()
    for row in rows:
        index = row["segment_index"]
        if index == 11:
            assert row["audio_path"] == str(audio_dir / "seg_011.wav")
            assert (audio_dir / "seg_011.wav").stat().st_mtime_ns == original_mtimes[(11, "audio")]
            continue
        assert row["image_path"] == str(images_dir / f"segment_{index:03d}.png")
        assert row["audio_path"] == str(audio_dir / f"seg_{index:03d}.wav")
        assert row["updated_at"] == expected_timestamps[index]
        assert Path(row["image_path"]).stat().st_mtime_ns == original_mtimes[(index, "image")]
        assert Path(row["audio_path"]).stat().st_mtime_ns == original_mtimes[(index, "audio")]


def test_mixed_asset_retry_calls_only_each_requested_provider(
    executor_db, tmp_path, monkeypatch
):
    task_id = "mixed-asset-retry"
    executor_db.create_task(
        task_id,
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "mixed-project"
    images_dir = project_dir / "images"
    audio_dir = project_dir / "voiceovers"
    images_dir.mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    image_one = images_dir / "segment_001.png"
    image_two = images_dir / "segment_002.png"
    audio_zero = audio_dir / "seg_000.wav"
    audio_two = audio_dir / "seg_002.wav"
    for path, content in (
        (image_one, b"image-one"),
        (image_two, b"image-two"),
        (audio_zero, b"audio-zero"),
        (audio_two, b"audio-two"),
    ):
        path.write_bytes(content)
    executor_db.save_segments(task_id, [
        {
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "prompt-0",
            "image_status": "failed",
            "audio_path": str(audio_zero),
            "audio_status": "completed",
        },
        {
            "segment_index": 1,
            "text": "第二段",
            "image_prompt": "prompt-1",
            "image_path": str(image_one),
            "image_status": "completed",
            "audio_status": "failed",
        },
        {
            "segment_index": 2,
            "text": "第三段",
            "image_prompt": "prompt-2",
            "image_path": str(image_two),
            "image_status": "completed",
            "audio_path": str(audio_two),
            "audio_status": "completed",
        },
    ])
    with executor_db.get_connection() as connection:
        connection.execute(
            "UPDATE task_segments SET updated_at=? WHERE task_id=? AND segment_index=2",
            ("2026-08-19 13:00:00", task_id),
        )
    untouched_image_mtime = image_two.stat().st_mtime_ns
    untouched_audio_mtime = audio_two.stat().st_mtime_ns
    targets = [
        {
            "segment_index": 0,
            "asset_type": "image",
            "mode": "retry",
            "status": "processing",
            "error": None,
        },
        {
            "segment_index": 1,
            "asset_type": "audio",
            "mode": "retry",
            "status": "processing",
            "error": None,
        },
    ]
    operation = executor_db.create_task_operation(
        task_id, "retry_assets", "mixed-retry-key", "snapshot", targets
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"image_concurrency": 8, "tts_concurrency": 2},
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        task_id, operation["operation_id"], targets, TaskCancellation()
    )

    rows = executor_db.get_segments(task_id)
    assert pipeline.image_generator.calls == [0]
    assert pipeline.voiceover_generator.calls == [1]
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.draft_builder.calls == 0
    assert rows[0]["image_status"] == "completed"
    assert rows[1]["audio_status"] == "completed"
    assert rows[2]["updated_at"] == "2026-08-19 13:00:00"
    assert image_two.stat().st_mtime_ns == untouched_image_mtime
    assert audio_two.stat().st_mtime_ns == untouched_audio_mtime


def test_full_asset_retry_keeps_legacy_mode_and_exposes_finalize_without_migrating(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "full-scoped-retry",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="full",
    )
    project_dir = tmp_path / "project"
    audio = project_dir / "voiceovers" / "seg_000.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    executor_db.save_segments("full-scoped-retry", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_status": "failed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    executor_db.update_task_workflow(
        "full-scoped-retry",
        "generating_assets",
        status="interrupted",
        current_step="image_generation",
    )
    target = [{
        "segment_index": 0,
        "asset_type": "image",
        "mode": "retry",
        "status": "processing",
        "error": None,
    }]
    operation = executor_db.create_task_operation(
        "full-scoped-retry", "retry_assets", "full-retry-key", "snapshot", target
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"image_concurrency": 8, "tts_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        "full-scoped-retry", operation["operation_id"], target, TaskCancellation()
    )

    task = executor_db.get_task("full-scoped-retry")
    assert task["execution_mode"] == "full"
    assert task["status"] == "interrupted"
    assert task["workflow_phase"] == "generating_assets"
    assert task["current_step"] == "draft_building"
    assert pipeline.image_generator.calls == [0]
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 0


def test_stale_update_replaces_only_the_selected_asset_and_keeps_old_file(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "stale-update",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    images = project_dir / "images"
    audio_dir = project_dir / "voiceovers"
    images.mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    old_image = images / "old-000.png"
    untouched_image = images / "segment-001.png"
    audio0 = audio_dir / "segment-000.wav"
    audio1 = audio_dir / "segment-001.wav"
    old_image.write_bytes(b"old-image")
    untouched_image.write_bytes(b"untouched-image")
    audio0.write_bytes(b"audio-0")
    audio1.write_bytes(b"audio-1")
    executor_db.save_segments("stale-update", [
        {
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "new-prompt",
            "image_path": str(old_image),
            "image_status": "stale",
            "audio_path": str(audio0),
            "audio_status": "completed",
        },
        {
            "segment_index": 1,
            "text": "第二段",
            "image_prompt": "prompt-1",
            "image_path": str(untouched_image),
            "image_status": "completed",
            "audio_path": str(audio1),
            "audio_status": "completed",
        },
    ])
    executor_db.save_task_result("stale-update", str(project_dir), 2)
    with executor_db.get_connection() as connection:
        connection.execute(
            "UPDATE task_segments SET updated_at=? WHERE task_id=? AND segment_index=1",
            ("2026-08-19 12:34:56", "stale-update"),
        )
    untouched_mtime = untouched_image.stat().st_mtime_ns
    target = [{
        "segment_index": 0,
        "asset_type": "image",
        "mode": "replace",
        "status": "processing",
        "error": None,
    }]
    operation = executor_db.create_task_operation(
        "stale-update", "retry_assets", "stale-update-key", "snapshot", target
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"image_concurrency": 8, "tts_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        "stale-update", operation["operation_id"], target, TaskCancellation()
    )

    rows = executor_db.get_segments("stale-update")
    task = executor_db.get_task("stale-update")
    assert pipeline.image_generator.calls == [0]
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.draft_builder.calls == 0
    assert rows[0]["image_status"] == "completed"
    assert rows[0]["image_path"] != str(old_image)
    assert Path(rows[0]["image_path"]).is_file()
    assert old_image.read_bytes() == b"old-image"
    assert rows[1]["image_path"] == str(untouched_image)
    assert rows[1]["updated_at"] == "2026-08-19 12:34:56"
    assert untouched_image.stat().st_mtime_ns == untouched_mtime
    assert task["status"] == "awaiting_finalization"
    assert task.get("result") is None


def test_failed_replacement_keeps_the_previous_asset_and_ready_draft(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "replace-failure",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    image = project_dir / "images" / "segment_000.png"
    audio = project_dir / "voiceovers" / "seg_000.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"old-image")
    audio.write_bytes(b"audio")
    executor_db.save_segments("replace-failure", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    executor_db.save_task_result("replace-failure", str(project_dir), 1)
    executor_db.update_task_workflow(
        "replace-failure", "ready", status="completed", current_step="completed"
    )
    target = [{
        "segment_index": 0,
        "asset_type": "image",
        "mode": "replace",
        "status": "processing",
        "error": None,
    }]
    operation = executor_db.create_task_operation(
        "replace-failure", "retry_assets", "replace-key", "snapshot", target
    )["operation"]
    pipeline = FakePipeline(str(project_dir), fail_assets=True)
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"image_concurrency": 8, "tts_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_asset_retry(
        "replace-failure", operation["operation_id"], target, TaskCancellation()
    )

    row = executor_db.get_segments("replace-failure")[0]
    assert row["image_path"] == str(image)
    assert row["image_status"] == "completed"
    assert image.read_bytes() == b"old-image"
    assert executor_db.get_task("replace-failure")["status"] == "completed"
    assert executor_db.get_task("replace-failure")["result"]["draft_path"] == str(project_dir)
    assert executor_db.get_task_operation(operation["operation_id"])["state"] == "failed"


def test_finalize_uses_existing_assets_without_generation_calls(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "finalize-only",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    image = project_dir / "images" / "segment_000.png"
    audio = project_dir / "voiceovers" / "seg_000.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    executor_db.save_segments("finalize-only", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    executor_db.update_task_workflow(
        "finalize-only",
        "awaiting_finalization",
        status="awaiting_finalization",
        current_step="awaiting_finalization",
    )
    operation = executor_db.create_task_operation(
        "finalize-only",
        "finalize",
        "finalize-key",
        "snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_finalize_task(
        "finalize-only", operation["operation_id"], TaskCancellation()
    )

    published_dir = (
        project_dir
        / ".finalize"
        / "versions"
        / operation["operation_id"]
        / "主题"
    )
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.script_rewriter.calls == 0
    assert pipeline.text_segmenter.calls == 0
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 1
    assert executor_db.get_task_operation(operation["operation_id"])["state"] == "completed"
    stored_task = executor_db.get_task("finalize-only")
    assert stored_task["status"] == "completed"
    assert stored_task["result"]["draft_path"] == str(published_dir)
    assert published_dir.is_dir()
    assert (published_dir / "draft_content.json").is_file()
    assert (published_dir / "draft_meta_info.json").is_file()
    assert (published_dir / "主题.zip").is_file()
    assert (published_dir / "images" / "segment_000.png").read_bytes() == b"image"
    assert (published_dir / "voiceovers" / "seg_000.wav").read_bytes() == b"audio"
    published_content = json.loads(
        (published_dir / "draft_content.json").read_text(encoding="utf-8")
    )
    for material_type in ("videos", "audios"):
        for material in published_content["materials"][material_type]:
            assert (published_dir / material["path"]).is_file()
    assert not (
        project_dir / ".finalize" / "staging" / operation["operation_id"]
    ).exists()


def test_finalize_with_a_missing_asset_routes_to_exact_asset_repair(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "finalize-missing-asset",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    image = project_dir / "images" / "segment_000.png"
    audio = project_dir / "voiceovers" / "seg_000.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    executor_db.save_segments("finalize-missing-asset", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    operation = executor_db.create_task_operation(
        "finalize-missing-asset",
        "finalize",
        "finalize-missing-key",
        "snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    image.unlink()
    pipeline = FakePipeline(str(project_dir))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_finalize_task(
        "finalize-missing-asset", operation["operation_id"], TaskCancellation()
    )

    task = executor_db.get_task("finalize-missing-asset")
    stored_operation = executor_db.get_task_operation(operation["operation_id"])
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 0
    assert task["status"] == "interrupted"
    assert task["current_step"] == "asset_repair"
    assert "分镜 1 图片" in stored_operation["error"]
    assert stored_operation["state"] == "failed"
    assert not (project_dir / ".finalize").exists()


def test_finalize_builder_failure_exposes_finalize_failed_without_losing_assets(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "finalize-builder-failure",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    image = project_dir / "images" / "segment_000.png"
    audio = project_dir / "voiceovers" / "seg_000.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    old_draft = project_dir / "existing-draft"
    old_draft.mkdir()
    old_marker = old_draft / "draft_content.json"
    old_marker.write_text('{"old": true}', encoding="utf-8")
    assert executor_db.save_task_result(
        "finalize-builder-failure",
        str(old_draft),
        1,
        draft_url="/media/old-draft.zip",
    )
    executor_db.save_segments("finalize-builder-failure", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    operation = executor_db.create_task_operation(
        "finalize-builder-failure",
        "finalize",
        "finalize-builder-key",
        "snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    pipeline = FakePipeline(str(project_dir))
    pipeline.draft_builder = FakeDraftBuilder(
        OSError("disk full"), write_partial=True
    )
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_finalize_task(
        "finalize-builder-failure", operation["operation_id"], TaskCancellation()
    )

    task = executor_db.get_task("finalize-builder-failure")
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 1
    assert image.read_bytes() == b"image"
    assert audio.read_bytes() == b"audio"
    assert old_marker.read_text(encoding="utf-8") == '{"old": true}'
    assert task["status"] == "interrupted"
    assert task["current_step"] == "finalize_failed"
    assert task["result"]["draft_path"] == str(old_draft)
    assert task["result"]["draft_url"] == "/media/old-draft.zip"
    assert executor_db.get_task_operation(operation["operation_id"])["state"] == "failed"
    assert not (
        project_dir / ".finalize" / "versions" / operation["operation_id"]
    ).exists()
    assert not (
        project_dir / ".finalize" / "staging" / operation["operation_id"]
    ).exists()


def test_finalize_zip_failure_retains_previous_published_draft_and_result(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "finalize-zip-failure",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    project_dir = tmp_path / "project"
    image = project_dir / "images" / "segment_000.png"
    audio = project_dir / "voiceovers" / "seg_000.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    old_draft = project_dir / "existing-draft"
    old_draft.mkdir()
    old_marker = old_draft / "draft_content.json"
    old_marker.write_text('{"old": true}', encoding="utf-8")
    assert executor_db.save_task_result(
        "finalize-zip-failure",
        str(old_draft),
        1,
        draft_url="/media/old-draft.zip",
    )
    executor_db.save_segments("finalize-zip-failure", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    operation = executor_db.create_task_operation(
        "finalize-zip-failure",
        "finalize",
        "finalize-zip-key",
        "snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    pipeline = FakePipeline(str(project_dir))

    def fail_after_partial_archive(draft_dir, draft_name):
        (Path(draft_dir) / f".{draft_name}.zip.tmp").write_bytes(b"partial zip")
        raise OSError("zip write failed")

    monkeypatch.setattr(
        task_executor_module, "build_finalize_archive", fail_after_partial_archive
    )
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)._run_finalize_task(
        "finalize-zip-failure", operation["operation_id"], TaskCancellation()
    )

    task = executor_db.get_task("finalize-zip-failure")
    stored_operation = executor_db.get_task_operation(operation["operation_id"])
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 1
    assert image.read_bytes() == b"image"
    assert audio.read_bytes() == b"audio"
    assert old_marker.read_text(encoding="utf-8") == '{"old": true}'
    assert task["status"] == "interrupted"
    assert task["current_step"] == "finalize_failed"
    assert task["result"]["draft_path"] == str(old_draft)
    assert task["result"]["draft_url"] == "/media/old-draft.zip"
    assert stored_operation["state"] == "failed"
    assert stored_operation["error_code"] == "disk"
    assert stored_operation["error"] == stored_operation["error_meta"]["safe_message"]
    assert not (
        project_dir / ".finalize" / "versions" / operation["operation_id"]
    ).exists()
    assert not (
        project_dir / ".finalize" / "staging" / operation["operation_id"]
    ).exists()


def test_finish_task_operation_rolls_back_operation_task_and_result_together(
    temp_db,
):
    temp_db.create_task(
        "atomic-finish-rollback",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.update_task_workflow(
        "atomic-finish-rollback",
        "awaiting_finalization",
        status="awaiting_finalization",
        current_step="awaiting_finalization",
    )
    assert temp_db.save_task_result(
        "atomic-finish-rollback",
        "/old/draft",
        1,
        draft_url="/media/old.zip",
    )
    operation = temp_db.create_task_operation(
        "atomic-finish-rollback",
        "finalize",
        "atomic-finish-key",
        "snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    connection = sqlite3.connect(str(sqlite_client_module.DB_PATH))
    connection.executescript(
        """
        CREATE TRIGGER reject_atomic_finish
        BEFORE UPDATE OF current_step ON tasks
        WHEN NEW.task_id = 'atomic-finish-rollback'
             AND NEW.current_step = 'completed'
        BEGIN
            SELECT RAISE(ABORT, 'forced task finish failure');
        END;
        """
    )
    connection.commit()
    connection.close()

    committed = temp_db.finish_task_operation(
        operation["operation_id"],
        "atomic-finish-rollback",
        operation_state="completed",
        operation_targets=[{"asset_type": "draft", "status": "completed"}],
        completed_count=1,
        failed_count=0,
        operation_error="",
        task_status="completed",
        workflow_phase="ready",
        current_step="completed",
        task_error=None,
        result={
            "draft_path": "/new/draft",
            "draft_url": "/media/new.zip",
            "video_url": None,
            "segments_count": 1,
            "total_duration": None,
        },
    )

    assert committed is False
    stored_operation = temp_db.get_task_operation(operation["operation_id"])
    stored_task = temp_db.get_task("atomic-finish-rollback")
    assert stored_operation["state"] == "pending"
    assert stored_operation["targets"] == [
        {"asset_type": "draft", "status": "running"}
    ]
    assert stored_operation["completed_count"] == 0
    assert stored_operation["failed_count"] == 0
    assert stored_task["status"] == "awaiting_finalization"
    assert stored_task["workflow_phase"] == "awaiting_finalization"
    assert stored_task["current_step"] == "awaiting_finalization"
    assert stored_task["result"]["draft_path"] == "/old/draft"
    assert stored_task["result"]["draft_url"] == "/media/old.zip"


def test_task_operation_deduplication_and_restart_preserve_completed_replacements(
    temp_db, tmp_path
):
    temp_db.create_task("operation-restart", "主题", "知识科普|电影质感", 100)
    image = tmp_path / "ready.png"
    audio = tmp_path / "ready.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    temp_db.save_segments("operation-restart", [
        {
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "prompt-0",
            "image_path": str(image),
            "image_status": "completed",
            "audio_path": str(audio),
            "audio_status": "completed",
        },
        {
            "segment_index": 1,
            "text": "第二段",
            "image_prompt": "prompt-1",
            "image_status": "processing",
            "audio_path": str(audio),
            "audio_status": "completed",
        },
    ])
    targets = [
        {"segment_index": 0, "asset_type": "image", "mode": "replace", "status": "processing"},
        {"segment_index": 1, "asset_type": "image", "mode": "retry", "status": "processing"},
    ]

    created = temp_db.create_task_operation(
        "operation-restart", "retry_assets", "same-request", "snapshot", targets
    )
    duplicate = temp_db.create_task_operation(
        "operation-restart", "retry_assets", "same-request", "snapshot", targets
    )
    conflict = temp_db.create_task_operation(
        "operation-restart", "retry_assets", "other-request", "snapshot", targets
    )
    interrupted = temp_db.interrupt_orphaned_task_operation("operation-restart")
    retried = temp_db.create_task_operation(
        "operation-restart", "retry_assets", "same-request", "snapshot", targets
    )

    rows = temp_db.get_segments("operation-restart")
    assert created["outcome"] == "created"
    assert duplicate["outcome"] == "duplicate"
    assert conflict["outcome"] == "conflict"
    assert interrupted["state"] == "interrupted"
    assert retried["outcome"] == "created"
    assert rows[0]["image_status"] == "completed"
    assert rows[0]["image_path"] == str(image)
    assert rows[1]["image_status"] == "failed"


def test_each_prompt_is_persisted_before_the_next_model_call(
    executor_db, monkeypatch
):
    create_task(executor_db)
    pipeline = FakePipeline(
        output_dir="unused", prompt_failure_call=2
    )
    executor = TaskExecutor(pipeline_factory=lambda **kwargs: pipeline)
    cancellation = TaskCancellation()

    executor.run_inline("task-1", cancellation=cancellation)

    rows = executor_db.get_segments("task-1")
    assert rows[0]["image_prompt"] == "prompt-0"
    assert rows[1]["image_prompt"] in {None, ""}
    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.draft_builder.calls == 0


def test_script_and_initial_segments_are_saved_before_prompt_generation(
    executor_db
):
    create_task(executor_db)

    def assert_initial_rows(call_number):
        rows = executor_db.get_segments("task-1")
        assert call_number == 1
        assert [row["text"] for row in rows] == ["第一段", "第二段"]
        assert [row["image_status"] for row in rows] == ["pending", "pending"]
        assert [row["audio_status"] for row in rows] == ["pending", "pending"]

    pipeline = FakePipeline(
        output_dir="unused",
        prompt_failure_call=1,
        before_prompt_call=assert_initial_rows,
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    task = executor_db.get_task("task-1")
    rows = executor_db.get_segments("task-1")
    assert task["script_text"] == "第一段。第二段。"
    assert task["summary"] == "摘要"
    assert [row["text"] for row in rows] == ["第一段", "第二段"]


def test_resume_reuses_segments_and_submits_only_missing_assets(
    executor_db, tmp_path
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="已保存脚本", summary="已保存摘要", input_mode="script"
    )
    image = tmp_path / "legacy" / "images" / "seg_000.png"
    audio = tmp_path / "legacy" / "voiceovers" / "seg_000.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 0,
                "text": "已有分镜",
                "image_prompt": "已有提示词",
                "image_path": str(image),
                "image_status": "completed",
                "audio_path": str(audio),
                "audio_status": "completed",
            },
            {
                "segment_index": 1,
                "text": "待恢复分镜",
                "image_prompt": "待恢复提示词",
                "image_status": "pending",
                "audio_status": "pending",
            },
        ],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "legacy"), fail_assets=True)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert pipeline.script_rewriter.calls == 0
    assert pipeline.text_segmenter.calls == 0
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.image_generator.calls == [1]
    assert pipeline.voiceover_generator.calls == [1]
    assert pipeline.draft_builder.calls == 0
    assert executor_db.get_segments("task-1")[0]["image_path"] == str(image)


def test_missing_prompt_does_not_resubmit_valid_completed_image(
    executor_db, tmp_path
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    image = tmp_path / "legacy" / "images" / "seg_000.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 0,
                "text": "已有分镜",
                "image_prompt": "",
                "image_path": str(image),
                "image_status": "completed",
                "audio_status": "pending",
            }
        ],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "legacy"), fail_assets=True)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    row = executor_db.get_segments("task-1")[0]
    assert pipeline.image_prompt_agent.calls == 1
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == [0]
    assert row["image_status"] == "completed"
    assert row["image_path"] == str(image)


def test_prompt_failure_preserves_valid_completed_image_checkpoint(
    executor_db, tmp_path
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    image = tmp_path / "legacy" / "images" / "seg_000.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 0,
                "text": "已有分镜",
                "image_prompt": "",
                "image_path": str(image),
                "image_status": "completed",
                "audio_status": "pending",
            }
        ],
    )
    pipeline = FakePipeline(
        output_dir=str(tmp_path / "legacy"), prompt_failure_call=1
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    row = executor_db.get_segments("task-1")[0]
    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert row["image_status"] == "completed"
    assert row["image_path"] == str(image)


def test_provider_item_failures_interrupt_before_draft(executor_db):
    create_task(executor_db)
    pipeline = FakePipeline(output_dir="unused", fail_assets=True)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments("task-1")
    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert all(row["image_status"] == "failed" for row in rows)
    assert all(row["audio_status"] == "failed" for row in rows)
    assert all(row["image_error_code"] == "provider_error" for row in rows)
    assert all(row["audio_error_code"] == "provider_error" for row in rows)
    assert all(row["image_error"] == row["image_error_meta"]["safe_message"] for row in rows)
    assert all(row["audio_error"] == row["audio_error_meta"]["safe_message"] for row in rows)
    assert pipeline.draft_builder.calls == 0


def test_cancellation_at_real_stage_boundary_marks_task_interrupted(
    executor_db
):
    create_task(executor_db)
    cancellation = TaskCancellation()
    pipeline = FakePipeline(output_dir="unused", cancellation=cancellation)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=cancellation
    )

    task = executor_db.get_task("task-1")
    assert task["status"] == "interrupted"
    assert task["script_text"] == "第一段。第二段。"
    assert pipeline.text_segmenter.calls == 0
    assert pipeline.draft_builder.calls == 0


def test_cancellation_does_not_overwrite_deleting_status(executor_db):
    create_task(executor_db, status="deleting")
    cancellation = TaskCancellation()
    cancellation.cancel()

    TaskExecutor(pipeline_factory=FakePipeline).run_inline(
        "task-1", cancellation=cancellation
    )

    assert executor_db.get_task("task-1")["status"] == "deleting"


def test_new_task_uses_task_owned_output_directory(executor_db, tmp_path, monkeypatch):
    create_task(executor_db)
    created = []

    def pipeline_factory(**kwargs):
        created.append(kwargs)
        return FakePipeline(kwargs["output_dir"], prompt_failure_call=1)

    monkeypatch.chdir(tmp_path)
    TaskExecutor(pipeline_factory=pipeline_factory).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert Path(created[0]["output_dir"]) == Path("output/task-1/恢复测试")


def test_resume_uses_legacy_output_directory_from_persisted_path(
    executor_db, tmp_path
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    legacy_image = tmp_path / "old-project" / "images" / "seg_000.png"
    legacy_image.parent.mkdir(parents=True)
    legacy_image.write_bytes(b"png")
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 0,
                "text": "第一段",
                "image_prompt": "",
                "image_path": str(legacy_image),
                "image_status": "completed",
                "audio_status": "pending",
            }
        ],
    )
    created = []

    def pipeline_factory(**kwargs):
        created.append(kwargs)
        return FakePipeline(kwargs["output_dir"], prompt_failure_call=1)

    TaskExecutor(pipeline_factory=pipeline_factory).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert Path(created[0]["output_dir"]) == tmp_path / "old-project"


def test_resume_task_reports_lifecycle_outcomes(
    executor_db, monkeypatch
):
    create_task(executor_db, "recoverable", status="interrupted")
    executor_db.save_task_checkpoint(
        "recoverable", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "recoverable", [{"segment_index": 0, "text": "第一段"}]
    )
    create_task(executor_db, "completed", status="completed")
    create_task(executor_db, "empty", status="interrupted", theme="")
    create_task(executor_db, "theme-only", status="interrupted")
    registry = TaskRuntimeRegistry()
    threads = []

    class DeferredThread:
        def __init__(self, target, args):
            self.target = target
            self.args = args
            self.daemon = False
            threads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(task_executor_module, "task_runtime", registry)
    monkeypatch.setattr(task_executor_module, "Thread", DeferredThread)
    executor = TaskExecutor(pipeline_factory=FakePipeline)

    assert executor.resume_task("completed") == "already_completed"
    assert executor.resume_task("missing") == "not_recoverable"
    assert executor.resume_task("empty") == "not_recoverable"
    assert executor.resume_task("theme-only") == "started"
    assert executor_db.get_task("theme-only")["status"] == "processing"
    assert executor.resume_task("recoverable") == "started"
    assert executor_db.get_task("recoverable")["status"] == "processing"
    assert executor.resume_task("recoverable") == "already_running"
    assert len(threads) == 2
    assert all(thread.daemon for thread in threads)


def test_segments_only_legacy_resume_reconstructs_script_without_rewrite(
    executor_db
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint("task-1", summary="已有摘要", input_mode="script")
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 4,
                "text": "第一段",
                "image_prompt": "提示一",
                "image_status": "pending",
                "audio_status": "pending",
            },
            {
                "segment_index": 8,
                "text": "第二段",
                "image_prompt": "提示二",
                "image_status": "pending",
                "audio_status": "pending",
            },
        ],
    )
    pipeline = FakePipeline(output_dir="unused", fail_assets=True)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    task = executor_db.get_task("task-1")
    assert pipeline.script_rewriter.calls == 0
    assert pipeline.text_segmenter.calls == 0
    assert pipeline.article == "第一段\n第二段"
    assert pipeline.summary == "已有摘要"
    assert task["script_text"] == "第一段\n第二段"
    assert task["script_source"] == "reconstructed_segments"
    assert task["summary"] == "已有摘要"


def test_dot_dot_project_name_stays_inside_task_output(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db)
    with executor_db.get_connection() as connection:
        connection.execute(
            "UPDATE tasks SET name='..' WHERE task_id='task-1'"
        )
    created = []

    def pipeline_factory(**kwargs):
        created.append(kwargs)
        return FakePipeline(kwargs["output_dir"], prompt_failure_call=1)

    monkeypatch.chdir(tmp_path)
    TaskExecutor(pipeline_factory=pipeline_factory).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    task_root = (tmp_path / "output" / "task-1").resolve()
    output_dir = Path(created[0]["output_dir"]).resolve()
    assert output_dir == task_root / "task"
    assert output_dir.is_relative_to(task_root)


def test_script_checkpoint_false_stops_before_segmentation(
    executor_db, monkeypatch
):
    create_task(executor_db)
    pipeline = FakePipeline(output_dir="unused", fail_assets=True)
    monkeypatch.setattr(executor_db, "save_task_checkpoint", lambda *a, **k: False)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.script_rewriter.calls == 1
    assert pipeline.text_segmenter.calls == 0
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []


def test_initial_segments_checkpoint_false_stops_before_prompt_provider(
    executor_db, monkeypatch
):
    create_task(executor_db)
    pipeline = FakePipeline(output_dir="unused")
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("draft should not run"))
    monkeypatch.setattr(executor_db, "save_segments", lambda *a, **k: False)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.text_segmenter.calls == 1
    assert pipeline.image_prompt_agent.calls == 0
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 0


def test_prompt_checkpoint_false_stops_after_bounded_in_flight_prompts(
    executor_db, monkeypatch
):
    create_task(executor_db)
    pipeline = FakePipeline(output_dir="unused", fail_assets=True)
    original_update = executor_db.update_segment

    def fail_prompt_checkpoint(task_id, segment_index, updates):
        if "image_prompt" in updates:
            return False
        return original_update(task_id, segment_index, updates)

    monkeypatch.setattr(executor_db, "update_segment", fail_prompt_checkpoint)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.image_prompt_agent.calls == 2
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 0


def test_prompt_generation_uses_configured_bounded_concurrency(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "prompt-concurrency",
        "测试主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "prompt-concurrency"))
    pipeline.text_segmenter.split = lambda article: [f"第{i}段" for i in range(8)]
    pipeline.image_prompt_agent = ConcurrentPromptAgent(expected_concurrency=4)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"prompt_concurrency": 4, "tts_concurrency": 1, "image_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "prompt-concurrency", cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments("prompt-concurrency")
    assert pipeline.image_prompt_agent.max_active == 4
    assert sorted(pipeline.image_prompt_agent.calls) == [f"第{i}段" for i in range(8)]
    assert [row["image_prompt"] for row in rows] == [f"prompt:第{i}段" for i in range(8)]
    assert executor_db.get_task("prompt-concurrency")["status"] == "awaiting_confirmation"


def test_prompt_failure_keeps_other_concurrent_results_and_resume_only_retries_missing(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "prompt-recovery",
        "测试主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    first = FakePipeline(output_dir=str(tmp_path / "prompt-recovery"))
    first.text_segmenter.split = lambda article: [f"第{i}段" for i in range(4)]
    first.image_prompt_agent = ConcurrentPromptAgent(fail_text="第2段")
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"prompt_concurrency": 4, "tts_concurrency": 1, "image_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: first).run_inline(
        "prompt-recovery", cancellation=TaskCancellation()
    )

    interrupted_rows = executor_db.get_segments("prompt-recovery")
    assert [row["prompt_status"] for row in interrupted_rows] == [
        "completed", "completed", "failed", "completed"
    ]
    assert executor_db.get_task("prompt-recovery")["status"] == "interrupted"

    resumed = FakePipeline(output_dir=str(tmp_path / "prompt-recovery"))
    resumed.image_prompt_agent = ConcurrentPromptAgent()
    TaskExecutor(pipeline_factory=lambda **kwargs: resumed).run_inline(
        "prompt-recovery", cancellation=TaskCancellation()
    )

    resumed_rows = executor_db.get_segments("prompt-recovery")
    assert resumed.image_prompt_agent.calls == ["第2段"]
    assert [row["prompt_status"] for row in resumed_rows] == ["completed"] * 4
    assert executor_db.get_task("prompt-recovery")["status"] == "awaiting_confirmation"


def test_sparse_legacy_segment_indexes_are_used_for_db_rows_and_assets(
    executor_db
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 5,
                "text": "第五段",
                "image_prompt": "提示五",
                "image_status": "pending",
                "audio_status": "pending",
            },
            {
                "segment_index": 9,
                "text": "第九段",
                "image_prompt": "提示九",
                "image_status": "pending",
                "audio_status": "pending",
            },
        ],
    )
    pipeline = FakePipeline(output_dir="unused", fail_assets=True)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    rows = executor_db.get_segments("task-1")
    assets = executor_db.list_task_assets("task-1")
    assert [row["segment_index"] for row in rows] == [5, 9]
    assert all(row["image_status"] == "failed" for row in rows)
    assert all(row["audio_status"] == "failed" for row in rows)
    assert {asset["segment_index"] for asset in assets} == {5, 9}
    assert pipeline.image_generator.calls == [0, 1]
    assert pipeline.voiceover_generator.calls == [0, 1]


def test_stage_cancellation_drains_and_persists_all_submitted_assets(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": index,
                "text": f"第{index}段",
                "image_prompt": f"提示{index}",
                "image_status": "pending",
                "audio_status": "pending",
            }
            for index in range(2)
        ],
    )
    cancellation = TaskCancellation()
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    provider_calls_started = threading.Barrier(4)

    def cancel_after_all_provider_calls_started(index):
        provider_calls_started.wait(timeout=1)
        if index == 0:
            cancellation.cancel()

    pipeline.image_generator.on_generate = cancel_after_all_provider_calls_started
    pipeline.voiceover_generator.on_generate = cancel_after_all_provider_calls_started
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"tts_concurrency": 2, "image_concurrency": 2},
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=cancellation
    )

    rows = executor_db.get_segments("task-1")
    assets = executor_db.list_task_assets("task-1")
    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.draft_builder.calls == 0
    assert sorted(pipeline.image_generator.calls) == [0, 1]
    assert sorted(pipeline.voiceover_generator.calls) == [0, 1]
    assert all(row["image_status"] == "completed" for row in rows)
    assert all(row["audio_status"] == "completed" for row in rows)
    assert len(assets) == 4
    assert all(asset["status"] == "completed" for asset in assets)


def test_resume_processing_transition_clears_stale_task_error(
    executor_db, task_manager
):
    create_task(executor_db, status="interrupted")
    executor_db.update_task_status(
        "task-1", "interrupted", "image_prompt_generation", "旧任务错误"
    )

    task_manager.update_task_status("task-1", TaskStatus.PROCESSING)

    assert executor_db.get_task("task-1")["error"] is None


def test_explicit_none_clears_only_segment_error_columns(temp_db):
    create_task(temp_db)
    temp_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 0,
                "text": "第一段",
                "image_path": "keep.png",
                "image_error": "旧图片错误",
                "audio_error": "旧音频错误",
            }
        ],
    )

    assert temp_db.update_segment(
        "task-1",
        0,
        {"image_path": None, "image_error": None, "audio_error": None},
    )

    row = temp_db.get_segments("task-1")[0]
    assert row["image_path"] == "keep.png"
    assert row["image_error"] is None
    assert row["audio_error"] is None


def test_successful_retry_clears_segment_errors_and_preserves_failed_asset_history(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "task-1",
        [
            {
                "segment_index": 0,
                "text": "第一段",
                "image_prompt": "提示词",
                "image_status": "failed",
                "image_error": "旧图片错误",
                "audio_status": "failed",
                "audio_error": "旧音频错误",
            }
        ],
    )
    executor_db.save_task_asset(
        "task-1",
        "image",
        "generated",
        segment_index=0,
        status="failed",
        error_message="旧图片错误",
    )
    executor_db.save_task_asset(
        "task-1",
        "audio",
        "generated",
        segment_index=0,
        status="failed",
        error_message="旧音频错误",
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("stop after assets"))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    row = executor_db.get_segments("task-1")[0]
    assets = executor_db.list_task_assets("task-1")
    assert row["image_status"] == "completed"
    assert row["audio_status"] == "completed"
    assert row["image_error"] is None
    assert row["audio_error"] is None
    assert len(assets) == 4
    completed_assets = [asset for asset in assets if asset["status"] == "completed"]
    failed_assets = [asset for asset in assets if asset["status"] == "failed"]
    assert len(completed_assets) == 2
    assert len(failed_assets) == 2
    assert all(asset["error_message"] is None for asset in completed_assets)
    assert all(asset["path"] for asset in completed_assets)
    assert {asset["error_message"] for asset in failed_assets} == {"旧图片错误", "旧音频错误"}


def test_asset_segment_row_false_interrupts_before_draft(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "提示词",
            "image_status": "pending",
            "audio_status": "pending",
        }],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("draft reached"))
    original_update = executor_db.update_segment

    def fail_asset_row(task_id, segment_index, updates):
        if "image_path" in updates or "audio_path" in updates:
            return False
        return original_update(task_id, segment_index, updates)

    monkeypatch.setattr(executor_db, "update_segment", fail_asset_row)
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.draft_builder.calls == 0


def test_asset_record_falsey_interrupts_before_draft(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "提示词",
            "image_status": "pending",
            "audio_status": "pending",
        }],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("draft reached"))
    monkeypatch.setattr(executor_db, "save_task_asset", lambda *a, **k: {})
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.draft_builder.calls == 0


def test_final_segments_save_false_prevents_result_and_completion(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "提示词",
            "image_status": "pending",
            "audio_status": "pending",
        }],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    monkeypatch.setattr(executor_db, "save_segments", lambda *a, **k: False)
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(task_executor_module, "FFmpegExporter", FakeFFmpegExporter, raising=False)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    task = executor_db.get_task("task-1")
    assert pipeline.draft_builder.calls == 1
    assert task["status"] == "interrupted"
    assert task.get("result") is None


def test_default_task_completes_without_instantiating_ffmpeg(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db)
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))

    class ForbiddenFFmpegExporter:
        def __init__(self, **kwargs):
            raise AssertionError("默认任务不应初始化 FFmpeg")

    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(task_executor_module, "FFmpegExporter", ForbiddenFFmpegExporter, raising=False)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    task = executor_db.get_task("task-1")
    assert task["status"] == "completed"
    assert task["result"]["video_url"] is None
    assert "video_synthesis" not in {step["step_name"] for step in task["steps"]}
    assert list(tmp_path.rglob("*.mp4")) == []


def test_reused_valid_assets_clear_segment_errors_without_rewriting_asset_history(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    image = tmp_path / "legacy" / "images" / "seg.png"
    audio = tmp_path / "legacy" / "voiceovers" / "seg.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 7,
            "text": "已有分镜",
            "image_prompt": "已有提示词",
            "image_path": str(image),
            "image_status": "completed",
            "image_error": "旧图片错误",
            "audio_path": str(audio),
            "audio_status": "completed",
            "audio_error": "旧音频错误",
        }],
    )
    executor_db.save_task_asset(
        "task-1", "image", "generated", path=str(image), segment_index=7,
        status="completed", error_message="旧图片错误",
    )
    executor_db.save_task_asset(
        "task-1", "audio", "generated", path=str(audio), segment_index=7,
        status="completed", error_message="旧音频错误",
    )
    upsert_calls = []
    original_save_task_asset = executor_db.save_task_asset

    def record_upsert(*args, **kwargs):
        upsert_calls.append(kwargs.get("asset_type") or args[1])
        return original_save_task_asset(*args, **kwargs)

    monkeypatch.setattr(executor_db, "save_task_asset", record_upsert)
    pipeline = FakePipeline(output_dir=str(tmp_path / "legacy"))
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("stop after reuse"))

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    row = executor_db.get_segments("task-1")[0]
    assets = executor_db.list_task_assets("task-1")
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert row["image_error"] is None
    assert row["audio_error"] is None
    assert len(assets) == 2
    assert all(asset["status"] == "completed" for asset in assets)
    assert {asset["error_message"] for asset in assets} == {"旧图片错误", "旧音频错误"}
    assert sorted(upsert_calls) == ["audio", "image"]


def test_reused_valid_assets_insert_missing_task_asset_rows(
    executor_db, tmp_path
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    image = tmp_path / "legacy" / "images" / "seg.png"
    audio = tmp_path / "legacy" / "voiceovers" / "seg.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 7,
            "text": "已有分镜",
            "image_prompt": "已有提示词",
            "image_path": str(image),
            "image_status": "completed",
            "image_error": "旧图片错误",
            "audio_path": str(audio),
            "audio_status": "completed",
            "audio_error": "旧音频错误",
        }],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "legacy"))
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("stop after reuse"))

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assets = executor_db.list_task_assets("task-1")
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert len(assets) == 2
    assert {asset["asset_type"] for asset in assets} == {"image", "audio"}
    assert all(asset["status"] == "completed" for asset in assets)
    assert all(asset["error_message"] is None for asset in assets)


@pytest.mark.parametrize("upsert_failure", ["falsey", "raises"])
def test_reused_asset_upsert_failure_interrupts_before_draft(
    executor_db, tmp_path, monkeypatch, upsert_failure
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    image = tmp_path / "legacy" / "images" / "seg.png"
    audio = tmp_path / "legacy" / "voiceovers" / "seg.wav"
    image.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 7,
            "text": "已有分镜",
            "image_prompt": "已有提示词",
            "image_path": str(image),
            "image_status": "completed",
            "audio_path": str(audio),
            "audio_status": "completed",
        }],
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "legacy"))
    pipeline.draft_builder = FakeDraftBuilder(RuntimeError("draft reached"))

    def fail_upsert(*args, **kwargs):
        if upsert_failure == "raises":
            raise RuntimeError("asset upsert failed")
        return {}

    monkeypatch.setattr(executor_db, "save_task_asset", fail_upsert)

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 0


def test_task_cancelled_future_does_not_drop_other_successful_result(
    executor_db, tmp_path, monkeypatch
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    segments = []
    for index in range(2):
        audio = tmp_path / "legacy" / "voiceovers" / f"seg_{index}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"wav")
        segments.append({
            "segment_index": index,
            "text": f"第{index}段",
            "image_prompt": f"提示{index}",
            "image_status": "pending",
            "audio_path": str(audio),
            "audio_status": "completed",
        })
    executor_db.save_segments("task-1", segments)
    cancellation = TaskCancellation()
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    provider_calls_started = threading.Barrier(2)

    def generate_started_images(value, **kwargs):
        index = kwargs["index"]
        pipeline.image_generator.calls.append(index)
        provider_calls_started.wait(timeout=1)
        if index == 0:
            cancellation.cancel()
            raise TaskCancelled("cancelled item 0")
        path = tmp_path / "generated" / "images" / "seg_001.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return str(path)

    pipeline.image_generator.generate = generate_started_images
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"tts_concurrency": 1, "image_concurrency": 2},
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=cancellation
    )

    rows = executor_db.get_segments("task-1")
    image_assets = executor_db.list_task_assets("task-1", asset_type="image")
    assert pipeline.image_generator.calls == [0, 1]
    assert rows[0]["image_status"] == "pending"
    assert rows[1]["image_status"] == "completed"
    assert [asset["segment_index"] for asset in image_assets] == [1]
    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.draft_builder.calls == 0


@pytest.mark.parametrize(
    ("asset_type", "completed_path_field", "completed_status_field"),
    [
        ("image", "audio_path", "audio_status"),
        ("audio", "image_path", "image_status"),
    ],
)
def test_cancelled_worker_skips_paid_provider_calls_still_queued(
    executor_db,
    tmp_path,
    monkeypatch,
    asset_type,
    completed_path_field,
    completed_status_field,
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    segments = []
    completed_suffix = "wav" if asset_type == "image" else "png"
    for index in range(2):
        completed_path = tmp_path / "legacy" / f"seg_{index}.{completed_suffix}"
        completed_path.parent.mkdir(parents=True, exist_ok=True)
        completed_path.write_bytes(completed_suffix.encode())
        segments.append({
            "segment_index": index,
            "text": f"第{index}段",
            "image_prompt": f"提示{index}",
            "image_status": "pending" if asset_type == "image" else "completed",
            "audio_status": "pending" if asset_type == "audio" else "completed",
            completed_path_field: str(completed_path),
            completed_status_field: "completed",
        })
    executor_db.save_segments("task-1", segments)
    cancellation = TaskCancellation()
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    generator = (
        pipeline.image_generator
        if asset_type == "image"
        else pipeline.voiceover_generator
    )

    def cancel_after_first_provider_call(index):
        if index == 0:
            cancellation.cancel()

    generator.on_generate = cancel_after_first_provider_call
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"tts_concurrency": 1, "image_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "task-1", cancellation=cancellation
    )

    rows = executor_db.get_segments("task-1")
    status_field = f"{asset_type}_status"
    assert generator.calls == [0]
    assert rows[0][status_field] == "completed"
    assert rows[1][status_field] == "pending"
    assert executor_db.get_task("task-1")["status"] == "interrupted"
    assert pipeline.draft_builder.calls == 0


def test_executor_cancel_stops_new_dispatch_and_drains_started_assets(
    executor_db, tmp_path, monkeypatch
):
    task_id = "cancel-live-executor"
    create_asset_resume_task(executor_db, task_id, count=3)
    registry = TaskRuntimeRegistry()
    monkeypatch.setattr(task_executor_module, "task_runtime", registry)
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {
            "prompt_concurrency": 1,
            "image_concurrency": 1,
            "tts_concurrency": 1,
        },
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    started = threading.Event()
    release = threading.Event()
    started_count = 0
    started_lock = threading.Lock()

    def hold_started_asset(_index):
        nonlocal started_count
        with started_lock:
            started_count += 1
            if started_count == 2:
                started.set()
        assert release.wait(timeout=2)

    pipeline.image_generator.on_generate = hold_started_asset
    pipeline.voiceover_generator.on_generate = hold_started_asset
    executor = TaskExecutor(pipeline_factory=lambda **_kwargs: pipeline)

    assert executor.execute_task(
        task_id,
        "原始内容",
        "知识科普|电影质感",
        100,
    )
    assert started.wait(timeout=2)
    releaser = threading.Timer(0.05, release.set)
    releaser.start()
    try:
        assert executor.cancel_task(task_id, timeout=2)
    finally:
        release.set()
        releaser.join()

    rows = executor_db.get_segments(task_id)
    assert pipeline.image_generator.calls == [0]
    assert pipeline.voiceover_generator.calls == [0]
    assert rows[0]["image_status"] == "completed"
    assert rows[0]["audio_status"] == "completed"
    assert all(row["image_status"] == "pending" for row in rows[1:])
    assert all(row["audio_status"] == "pending" for row in rows[1:])
    assert executor_db.get_task(task_id)["status"] == "interrupted"
    assert not registry.is_running(task_id)
    assert pipeline.draft_builder.calls == 0


@pytest.mark.parametrize("asset_type", ["image", "audio"])
def test_upload_copy_failure_keeps_local_asset_reusable_on_resume(
    executor_db, tmp_path, monkeypatch, asset_type
):
    create_task(executor_db, status="interrupted")
    executor_db.save_task_checkpoint(
        "task-1", script_text="脚本", summary="摘要", input_mode="script"
    )
    completed_type = "audio" if asset_type == "image" else "image"
    completed_suffix = "wav" if completed_type == "audio" else "png"
    completed_path = tmp_path / "legacy" / f"completed.{completed_suffix}"
    completed_path.parent.mkdir(parents=True)
    completed_path.write_bytes(completed_suffix.encode())
    executor_db.save_segments(
        "task-1",
        [{
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "提示词",
            f"{asset_type}_status": "pending",
            f"{completed_type}_path": str(completed_path),
            f"{completed_type}_status": "completed",
        }],
    )
    first_pipeline = FakePipeline(output_dir=str(tmp_path / "generated"))
    first_pipeline.draft_builder = FakeDraftBuilder(RuntimeError("stop after assets"))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FailingUploader)

    TaskExecutor(pipeline_factory=lambda **kwargs: first_pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    path_field = f"{asset_type}_path"
    status_field = f"{asset_type}_status"
    error_field = f"{asset_type}_error"
    row = executor_db.get_segments("task-1")[0]
    asset = executor_db.list_task_assets("task-1", asset_type=asset_type)[0]
    assert Path(row[path_field]).is_file()
    assert row[status_field] == "completed"
    assert row[error_field] == "本地文件写入失败，请检查磁盘空间和目录权限。"
    assert row[f"{asset_type}_error_code"] == "disk"
    assert asset["status"] == "completed"
    assert asset["error_message"] == "本地文件写入失败，请检查磁盘空间和目录权限。"

    resumed_pipeline = FakePipeline(output_dir=str(tmp_path / "resumed"))
    resumed_pipeline.draft_builder = FakeDraftBuilder(RuntimeError("stop after assets"))
    TaskExecutor(pipeline_factory=lambda **kwargs: resumed_pipeline).run_inline(
        "task-1", cancellation=TaskCancellation()
    )

    resumed_generator = (
        resumed_pipeline.image_generator
        if asset_type == "image"
        else resumed_pipeline.voiceover_generator
    )
    resumed_row = executor_db.get_segments("task-1")[0]
    assert resumed_generator.calls == []
    assert resumed_row[status_field] == "completed"
    assert resumed_row[error_field] == "本地文件写入失败，请检查磁盘空间和目录权限。"


def test_review_first_verbatim_task_pauses_after_persisted_plan(
    executor_db, tmp_path
):
    executor_db.create_task(
        "review-task",
        "用户原文，不允许改写。",
        "知识科普|电影质感",
        0,
        name="预案任务",
        execution_mode="review_first",
        script_policy="verbatim",
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "review-task"))

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "review-task", cancellation=TaskCancellation()
    )

    task = executor_db.get_task("review-task")
    segments = executor_db.get_segments("review-task")
    assert task["script_text"] == "用户原文，不允许改写。"
    assert task["status"] == "awaiting_confirmation"
    assert task["workflow_phase"] == "awaiting_confirmation"
    assert pipeline.script_rewriter.calls == 0
    assert pipeline.image_prompt_agent.calls == 2
    assert pipeline.image_generator.calls == []
    assert pipeline.voiceover_generator.calls == []
    assert pipeline.draft_builder.calls == 0
    assert [segment["prompt_status"] for segment in segments] == ["completed", "completed"]


def test_workspace_plan_version_rejects_stale_segment_edit(temp_db):
    temp_db.create_task(
        "version-task",
        "原文",
        "知识科普|电影质感",
        0,
        execution_mode="review_first",
        script_policy="verbatim",
    )
    temp_db.save_segments(
        "version-task",
        [{"segment_index": 0, "text": "第一段", "image_prompt": "提示词"}],
    )

    version = temp_db.update_segment_plan(
        "version-task", 0, {"text": "新文案"}, expected_plan_version=0
    )
    stale = temp_db.update_segment_plan(
        "version-task", 0, {"text": "旧页面覆盖"}, expected_plan_version=0
    )

    assert version == 1
    assert stale == -1
    assert temp_db.get_segments("version-task")[0]["text"] == "新文案"


def test_review_first_asset_generation_honors_segment_voice_override(
    executor_db, tmp_path, monkeypatch
):
    executor_db.create_task(
        "voice-override-task",
        "第一段。第二段。",
        "知识科普|电影质感",
        0,
        voice_type="mimo:冰糖",
        tts_options={"speed_level": "normal", "style_prompt": "全片风格"},
        execution_mode="review_first",
        script_policy="verbatim",
    )
    executor_db.save_task_checkpoint(
        "voice-override-task",
        script_text="第一段。第二段。",
        summary="摘要",
        input_mode="script",
        workflow_phase="assets_requested",
        voice_confirmed=1,
    )
    executor_db.save_segments(
        "voice-override-task",
        [
            {
                "segment_index": 0,
                "text": "第一段",
                "image_prompt": "提示一",
                "image_status": "pending",
                "audio_status": "pending",
            },
            {
                "segment_index": 1,
                "text": "第二段",
                "image_prompt": "提示二",
                "image_status": "pending",
                "audio_status": "pending",
                "audio_voice_type": "doubao:zh_male_jieshuoxiaoming_moon_bigtts",
                "audio_tts_options_json": '{"speed_level":"fast","volume_ratio":1.2}',
            },
        ],
    )
    executor_db.update_task_workflow(
        "voice-override-task", "assets_requested", status="interrupted"
    )
    pipeline = FakePipeline(output_dir=str(tmp_path / "voice-override-task"))
    monkeypatch.setattr(task_executor_module, "LocalUploader", FakeUploader)
    monkeypatch.setattr(
        task_executor_module.Config,
        "generation_config",
        lambda: {"tts_concurrency": 1, "image_concurrency": 1},
    )

    TaskExecutor(pipeline_factory=lambda **kwargs: pipeline).run_inline(
        "voice-override-task", cancellation=TaskCancellation()
    )

    calls = sorted(
        pipeline.voiceover_generator.call_kwargs,
        key=lambda item: item["filename"],
    )
    rows = executor_db.get_segments("voice-override-task")
    assert [call["voice_type"] for call in calls] == [
        "mimo:冰糖",
        "doubao:zh_male_jieshuoxiaoming_moon_bigtts",
    ]
    assert calls[1]["speed_level"] == "fast"
    assert calls[1]["volume_ratio"] == 1.2
    assert rows[0]["audio_voice_type"] in {None, ""}
    assert rows[0]["audio_tts_options_json"] in {None, ""}
    assert rows[1]["audio_voice_type"].startswith("doubao:")
    assert json.loads(rows[1]["audio_tts_options_json"])["speed_level"] == "fast"
    assert rows[0]["audio_voice_type"] == ""
    assert rows[1]["audio_voice_type"] == "doubao:zh_male_jieshuoxiaoming_moon_bigtts"
    task = executor_db.get_task("voice-override-task")
    assert task["status"] == "awaiting_finalization"
    assert task["workflow_phase"] == "awaiting_finalization"
    assert task.get("result") is None
    assert pipeline.draft_builder.calls == 0

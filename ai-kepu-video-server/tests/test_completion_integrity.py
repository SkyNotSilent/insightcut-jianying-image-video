import json

import pytest

from src.api import task_manager as task_manager_module
from src.api.error_model import ErrorCode
from src.api.models import TaskStatus
from src.api.task_manager import CompletionIntegrityError, TaskManager
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


@pytest.fixture
def completion_context(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    database = SQLiteClient()
    monkeypatch.setattr(task_manager_module, "db_client", database)
    manager = TaskManager()
    return database, manager


def _create_completed_fixture(database, tmp_path, task_id="completion-task"):
    image = tmp_path / "segment.png"
    audio = tmp_path / "segment.wav"
    image.write_bytes(b"png")
    audio.write_bytes(b"wav")
    draft = tmp_path / "draft"
    draft.mkdir()
    content = {
        "tracks": [
            {
                "type": "video",
                "segments": [{"target_timerange": {"start": 0, "duration": 1_000_000}}],
            },
            {
                "type": "audio",
                "segments": [{"target_timerange": {"start": 0, "duration": 1_000_000}}],
            },
        ],
        "materials": {"videos": [], "audios": []},
    }
    (draft / "draft_content.json").write_text(json.dumps(content), encoding="utf-8")
    (draft / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    assert database.create_task(task_id, "主题", "知识科普|电影质感", 100)
    assert database.save_segments(task_id, [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    return image, audio, draft


def test_atomic_completion_clears_historical_error(completion_context, tmp_path):
    database, manager = completion_context
    _image, _audio, draft = _create_completed_fixture(database, tmp_path)
    assert database.update_task_status(
        "completion-task", "interrupted", "asset_repair", "旧错误",
        error_code=ErrorCode.UNKNOWN.value,
    )

    manager.complete_task(
        "completion-task", str(draft), 1, draft_url="/media/draft.zip"
    )

    task = database.get_task("completion-task")
    assert task["status"] == TaskStatus.COMPLETED.value
    assert task["workflow_phase"] == "ready"
    assert task["current_step"] == "completed"
    assert task["error"] is None
    assert task["error_code"] is None
    assert task["error_meta"] is None
    assert task["result"]["draft_path"] == str(draft)


def test_atomic_completion_rejects_missing_asset_and_keeps_checkpoint(
    completion_context, tmp_path
):
    database, manager = completion_context
    image, _audio, draft = _create_completed_fixture(database, tmp_path)
    image.unlink()

    with pytest.raises(CompletionIntegrityError) as captured:
        manager.complete_task("completion-task", str(draft), 1)

    assert captured.value.safe_error.code is ErrorCode.ASSET_MISSING
    task = database.get_task("completion-task")
    assert task["status"] == TaskStatus.INTERRUPTED.value
    assert task["workflow_phase"] == "generating_assets"
    assert task["current_step"] == "asset_repair"
    assert task["error_code"] == ErrorCode.ASSET_MISSING.value
    assert database.get_segments("completion-task")[0]["image_status"] == "completed"
    assert task.get("result") is None


def test_reconcile_completed_task_is_idempotent_and_preserves_assets(
    completion_context, tmp_path
):
    database, manager = completion_context
    image, audio, draft = _create_completed_fixture(database, tmp_path)
    assert database.complete_task_with_result(
        "completion-task", str(draft), 1, workflow_phase="ready"
    )
    image.unlink()

    row = database.get_task("completion-task")
    assert manager.reconcile_completed_task_data(row) is True
    assert manager.reconcile_completed_task_data(database.get_task("completion-task")) is False

    repaired = database.get_task("completion-task")
    segment = database.get_segments("completion-task")[0]
    assert repaired["status"] == TaskStatus.INTERRUPTED.value
    assert repaired["error_code"] == ErrorCode.ASSET_MISSING.value
    assert segment["image_path"] == str(image)
    assert segment["audio_path"] == str(audio)
    assert segment["audio_status"] == "completed"
    assert repaired["result"]["draft_path"] == str(draft)


def test_reconcile_accepts_flattened_completed_list_rows(completion_context, tmp_path):
    database, manager = completion_context
    _image, _audio, draft = _create_completed_fixture(database, tmp_path)
    assert database.complete_task_with_result(
        "completion-task", str(draft), 1, workflow_phase="ready"
    )

    rows = database.list_tasks(status=TaskStatus.COMPLETED.value)

    assert rows[0]["draft_path"] == str(draft)
    assert rows[0]["segments_count"] == 1
    assert "result" not in rows[0]
    assert manager.reconcile_completed_task_data(rows[0]) is False
    assert database.get_task("completion-task")["status"] == TaskStatus.COMPLETED.value

import inspect

from src.api.models import TaskStatus
from src.api.routes import _activity_item, get_task_activity
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


def test_activity_progress_counts_script_storyboard_and_prompts_before_assets():
    writing = _activity_item({
        "task_id": "writing",
        "name": "正在写文稿",
        "status": TaskStatus.PROCESSING.value,
        "workflow_phase": "planning",
        "current_step": "text_generation",
    })
    script_ready = _activity_item({
        "task_id": "script",
        "name": "文稿已完成",
        "status": TaskStatus.PROCESSING.value,
        "workflow_phase": "planning",
        "current_step": "segmentation",
        "script_text": "这是已经持久化的文稿。",
    })
    prompts_half_ready = _activity_item({
        "task_id": "prompts",
        "name": "正在生成提示词",
        "status": TaskStatus.PROCESSING.value,
        "workflow_phase": "planning",
        "current_step": "image_prompt_generation",
        "script_text": "这是已经持久化的文稿。",
        "segments_total": 4,
        "prompts_ready": 2,
    })
    awaiting_confirmation = _activity_item({
        "task_id": "confirm",
        "name": "等待确认",
        "status": TaskStatus.AWAITING_CONFIRMATION.value,
        "workflow_phase": "awaiting_confirmation",
        "script_text": "这是已经持久化的文稿。",
        "segments_total": 4,
        "prompts_ready": 4,
    })

    assert writing["progress"] == 5
    assert script_ready["progress"] == 30
    assert prompts_half_ready["progress"] == 45
    assert awaiting_confirmation["progress"] == 50
    assert awaiting_confirmation["activity_label"] == "等待确认"


def test_activity_progress_weights_images_and_audio_after_the_plan():
    item = _activity_item({
        "task_id": "assets",
        "name": "正在生成素材",
        "status": TaskStatus.PROCESSING.value,
        "workflow_phase": "generating_assets",
        "script_text": "这是已经持久化的文稿。",
        "segments_total": 4,
        "prompts_ready": 4,
        "images_ready": 2,
        "audio_ready": 1,
    })

    assert item["progress"] == 69
    assert item["progress_breakdown"] == {
        "script": 30,
        "storyboard": 10,
        "prompts": 10,
        "images": 13,
        "audio": 6,
    }


def test_assets_complete_reaches_one_hundred_but_only_validated_draft_is_export_ready():
    waiting = _activity_item({
        "task_id": "waiting",
        "name": "等待完成生产",
        "status": TaskStatus.AWAITING_FINALIZATION.value,
        "workflow_phase": "awaiting_finalization",
        "script_text": "已完成文稿",
        "segments_total": 2,
        "prompts_ready": 2,
        "images_ready": 2,
        "audio_ready": 2,
    })
    completed = _activity_item({
        "task_id": "completed",
        "name": "已经完成",
        "status": TaskStatus.COMPLETED.value,
        "workflow_phase": "ready",
        "script_text": "已完成文稿",
        "segments_total": 2,
        "prompts_ready": 2,
        "images_ready": 2,
        "audio_ready": 2,
    })

    assert waiting["progress"] == 100
    assert waiting["activity_label"] == "素材已齐 · 待构建草稿"
    assert waiting["export_ready"] is False
    assert completed["progress"] == 100
    assert completed["activity_label"] == "可导出"
    assert completed["export_ready"] is True
    assert completed["exported_at"] is None


def test_successful_export_is_persisted_for_activity_filtering(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "activity.db")
    database = SQLiteClient()
    assert database.create_task("export-me", "待导出项目", "知识科普", 100)
    assert database.update_task_status("export-me", "completed", "completed")
    assert [item["task_id"] for item in database.list_task_activity()] == ["export-me"]

    assert database.mark_task_exported("export-me", "mp4")

    task = database.get_task("export-me")
    assert task["exported_at"]
    assert task["last_export_target"] == "mp4"
    assert database.list_task_activity() == []


def test_activity_query_reports_persisted_prompt_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "prompts.db")
    database = SQLiteClient()
    assert database.create_task("planned", "已生成预案", "知识科普", 100)
    assert database.save_task_checkpoint("planned", script_text="完整文稿")
    with database.get_connection() as conn:
        conn.executemany(
            """INSERT INTO task_segments
               (task_id, segment_index, text, image_prompt, image_status, audio_status)
               VALUES (?, ?, ?, ?, 'pending', 'pending')""",
            [
                ("planned", 0, "分镜一", "提示词一"),
                ("planned", 1, "分镜二", ""),
            ],
        )

    row = database.list_task_activity()[0]

    assert row["segments_total"] == 2
    assert row["prompts_ready"] == 1


def test_all_incomplete_tasks_are_kept_even_when_they_exceed_the_recent_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "incomplete.db")
    database = SQLiteClient()
    for index in range(13):
        task_id = f"incomplete-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
    for index in range(6):
        task_id = f"ready-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
        assert database.update_task_status(task_id, "completed", "completed")

    rows = database.list_task_activity()

    assert len(rows) == 13
    assert all(row["status"] != "completed" for row in rows)


def test_recent_export_ready_tasks_only_fill_the_remaining_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "fill.db")
    database = SQLiteClient()
    for index in range(4):
        task_id = f"incomplete-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
    for index in range(12):
        task_id = f"ready-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
        assert database.update_task_status(task_id, "completed", "completed")
    assert database.create_task("ready-too-old", "ready-too-old", "知识科普", 100)
    assert database.update_task_status("ready-too-old", "completed", "completed")
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE tasks
               SET completed_at=datetime('now','localtime','-31 days'),
                   updated_at=datetime('now','localtime')
               WHERE task_id='ready-too-old'"""
        )

    rows = database.list_task_activity()

    assert len(rows) == 10
    assert sum(row["status"] != "completed" for row in rows) == 4
    assert sum(row["status"] == "completed" for row in rows) == 6
    assert all(row["status"] != "completed" for row in rows[:4])
    assert "ready-too-old" not in {row["task_id"] for row in rows}


def test_activity_does_not_pad_when_fewer_than_ten_recent_projects_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "sparse.db")
    database = SQLiteClient()
    for index in range(2):
        task_id = f"incomplete-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
    for index in range(3):
        task_id = f"ready-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
        assert database.update_task_status(task_id, "completed", "completed")

    rows = database.list_task_activity()

    assert len(rows) == 5
    assert sum(row["status"] != "completed" for row in rows) == 2
    assert sum(row["status"] == "completed" for row in rows) == 3


def test_completed_only_activity_uses_completion_time_and_thirty_day_window(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "completed.db")
    database = SQLiteClient()
    for index in range(12):
        task_id = f"ready-{index:02d}"
        assert database.create_task(task_id, task_id, "知识科普", 100)
        assert database.update_task_status(task_id, "completed", "completed")
        with database.get_connection() as conn:
            conn.execute(
                """UPDATE tasks
                   SET completed_at=datetime('now','localtime', ?),
                       updated_at=datetime('now','localtime', ?)
                   WHERE task_id=?""",
                (f"-{index} days", f"+{index} minutes", task_id),
            )
    assert database.create_task("ready-too-old", "ready-too-old", "知识科普", 100)
    assert database.update_task_status("ready-too-old", "completed", "completed")
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE tasks
               SET completed_at=datetime('now','localtime','-31 days'),
                   updated_at=datetime('now','localtime','+1 year')
               WHERE task_id='ready-too-old'"""
        )

    rows = database.list_task_activity()

    assert [row["task_id"] for row in rows] == [f"ready-{index:02d}" for index in range(10)]


def test_activity_endpoint_defaults_to_ten_projects():
    limit = inspect.signature(get_task_activity).parameters["limit"].default

    assert limit.default == 10

import json

from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


def test_task_snapshots_templates_activity_and_asset_pointers(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    db = SQLiteClient()

    template = db.create_production_template({
        "name": "横屏科普",
        "ratio": "16:9",
        "subtitle_options": {"size": "large", "position": "low", "outline": "strong"},
        "generation_options": {"image_concurrency": 6, "retry_count": 3},
        "is_default": True,
    })
    assert template["is_default"] is True
    assert template["subtitle_options"]["size"] == "large"

    task_id = "v2-platform"
    assert db.create_task(
        task_id,
        "项目文案",
        "知识科普|电影质感",
        120,
        execution_mode="review_first",
        source_draft_id="draft-1",
        template_id=template["template_id"],
        generation_options={"image_concurrency": 6, "retry_count": 3},
        subtitle_options={"size": "large", "position": "low", "outline": "strong"},
    )
    task = db.get_task(task_id)
    assert task["source_draft_id"] == "draft-1"
    assert task["template_id"] == template["template_id"]
    assert json.loads(task["generation_options_json"])["image_concurrency"] == 6

    db.save_segments(task_id, [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": "output/old.png",
        "audio_path": "output/audio.wav",
        "image_status": "completed",
        "audio_status": "completed",
    }])
    old_asset = db.save_task_asset(
        task_id, "image", "generated", path="output/old.png",
        segment_index=0, prompt="old prompt", snapshot_json='{"prompt":"old prompt"}',
    )
    repeated = db.save_task_asset(
        task_id, "image", "generated", path="output/old.png",
        segment_index=0, prompt="old prompt", snapshot_json='{"prompt":"old prompt"}',
    )
    assert repeated["asset_id"] == old_asset["asset_id"]
    legacy_backfill = db.save_task_asset(
        task_id, "image", "legacy", path="output/old.png",
        segment_index=0, prompt="old prompt",
    )
    assert legacy_backfill["asset_id"] == old_asset["asset_id"]
    assert len(db.list_task_assets(task_id, "image")) == 1
    db.backfill_selected_asset_ids(task_id)
    assert db.get_segments(task_id)[0]["selected_image_asset_id"] == old_asset["asset_id"]

    new_asset = db.save_task_asset(
        task_id, "image", "regenerated", path="output/new.png",
        segment_index=0, prompt="new prompt", operation_id="op-1",
        origin_asset_id=old_asset["asset_id"], snapshot_json='{"prompt":"new prompt"}',
    )
    db.update_segment(task_id, 0, {"image_path": "output/new.png"})
    db.backfill_selected_asset_ids(task_id)
    segment = db.get_segments(task_id)[0]
    assert segment["selected_image_asset_id"] == new_asset["asset_id"]
    assert db.get_task_asset(task_id, old_asset["asset_id"])["path"] == "output/old.png"

    activity = db.list_task_activity()
    item = next(row for row in activity if row["task_id"] == task_id)
    assert item["segments_total"] == 1
    assert item["images_ready"] == 1
    assert item["audio_ready"] == 1

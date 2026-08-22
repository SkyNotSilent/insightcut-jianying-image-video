import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from src.api import routes
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient
from src.utils import local_uploader as uploader_module


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


def _request():
    return Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/assets",
        "headers": [],
    })


def _task(task_id, *, result=None, name="素材历史项目"):
    return SimpleNamespace(
        task_id=task_id,
        name=name,
        theme=name,
        voice_type="mimo:冰糖",
        result=result,
    )


def _create_task(db, task_id):
    db.create_task(
        task_id,
        "项目文案",
        "知识科普|电影质感",
        120,
        execution_mode="review_first",
    )


async def _stream_bytes(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))
    return b"".join(chunks)


def test_select_image_accepts_same_and_cross_segment_versions_without_rewriting_history(
    tmp_path, temp_db, monkeypatch
):
    base_dir = tmp_path / "server"
    images_dir = base_dir / "output" / "project-a" / "images"
    images_dir.mkdir(parents=True)
    current = images_dir / "current.png"
    history = images_dir / "history.png"
    other = images_dir / "other-segment.png"
    current.write_bytes(b"current")
    history.write_bytes(b"history")
    other.write_bytes(b"other")
    task_id = "history-select"
    _create_task(temp_db, task_id)
    temp_db.save_segments(
        task_id,
        [
            {
                "segment_index": 2,
                "text": "第三个分镜",
                "image_prompt": "current prompt",
                "image_path": "output/project-a/images/current.png",
                "image_status": "completed",
                "audio_status": "pending",
            },
            {
                "segment_index": 5,
                "text": "第六个分镜",
                "image_prompt": "other prompt",
                "image_path": "output/project-a/images/other-segment.png",
                "image_status": "completed",
                "audio_status": "pending",
            },
        ],
    )
    current_asset = temp_db.save_task_asset(
        task_id,
        "image",
        "legacy",
        path="output/project-a/images/current.png",
        url="/media/project-a/images/current.png",
        segment_index=2,
        prompt="current prompt",
    )
    history_asset = temp_db.save_task_asset(
        task_id,
        "image",
        "regenerated",
        path="project-a/images/history.png",
        url="/media/project-a/images/history.png",
        segment_index=2,
        prompt="history prompt",
    )
    other_asset = temp_db.save_task_asset(
        task_id,
        "image",
        "regenerated",
        path="project-a/images/other-segment.png",
        url="/media/project-a/images/other-segment.png",
        segment_index=5,
        prompt="other prompt",
    )
    published_draft = base_dir / "output" / task_id / ".finalize" / "versions" / "v1"
    published_draft.mkdir(parents=True)
    published_marker = published_draft / "draft_content.json"
    published_marker.write_text("published", encoding="utf-8")
    temp_db.save_task_result(task_id, str(published_draft), 2)
    temp_db.update_task_status(task_id, "completed", "completed", None)
    before_asset_ids = {
        item["asset_id"] for item in temp_db.list_task_assets(task_id, "image")
    }
    before_mtimes = {
        path: path.stat().st_mtime_ns for path in (current, history, other)
    }

    task = _task(task_id)
    monkeypatch.setattr(routes.Config, "BASE_DIR", base_dir)
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(
        routes,
        "task_manager",
        SimpleNamespace(get_task=lambda _task_id: task),
    )
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: False)

    selected = asyncio.run(
        routes.select_segment_image(
            task_id,
            2,
            _request(),
            {"asset_id": history_asset["asset_id"]},
        )
    )

    assert selected["previous_image_path"] == current_asset["path"]
    assert selected["image_path"] == history_asset["path"]
    segment = next(
        item for item in temp_db.get_segments(task_id) if item["segment_index"] == 2
    )
    assert segment["image_path"] == "project-a/images/history.png"
    assert segment["image_prompt"] == "history prompt"
    assert segment["image_status"] == "completed"
    after_assets = temp_db.list_task_assets(task_id, "image")
    assert before_asset_ids.issubset({item["asset_id"] for item in after_assets})
    assert not any(item["source"] == "selected" for item in after_assets)
    assert segment["selected_image_asset_id"] == history_asset["asset_id"]
    assert temp_db.get_task(task_id)["status"] == "awaiting_finalization"
    assert temp_db.get_task(task_id)["result"] is None
    assert published_marker.read_text(encoding="utf-8") == "published"
    for path, mtime_ns in before_mtimes.items():
        assert path.stat().st_mtime_ns == mtime_ns

    cross_selected = asyncio.run(
        routes.select_segment_image(
            task_id,
            2,
            _request(),
            {"asset_id": other_asset["asset_id"]},
        )
    )
    assert cross_selected["image_path"] == other_asset["path"]
    assert cross_selected["asset_id"] != other_asset["asset_id"]
    assert cross_selected["origin_asset_id"] == other_asset["asset_id"]
    changed = next(
        item for item in temp_db.get_segments(task_id) if item["segment_index"] == 2
    )
    assert changed["image_path"] == other_asset["path"]
    assert changed["selected_image_asset_id"] == cross_selected["asset_id"]
    assert changed["image_prompt"] == "other prompt"
    selected_version = temp_db.get_task_asset(task_id, cross_selected["asset_id"])
    assert selected_version["source"] == "selected"
    assert selected_version["segment_index"] == 2
    assert selected_version["origin_asset_id"] == other_asset["asset_id"]
    for path, mtime_ns in before_mtimes.items():
        assert path.stat().st_mtime_ns == mtime_ns


def test_cross_segment_audio_requires_text_mismatch_confirmation(
    tmp_path, temp_db, monkeypatch
):
    base_dir = tmp_path / "server"
    audio_dir = base_dir / "output" / "project-a" / "voiceovers"
    audio_dir.mkdir(parents=True)
    audio_file = audio_dir / "source.wav"
    audio_file.write_bytes(b"audio")
    task_id = "audio-cross-select"
    _create_task(temp_db, task_id)
    temp_db.save_segments(task_id, [
        {
            "segment_index": 0,
            "text": "目标分镜文案",
            "image_status": "pending",
            "audio_status": "pending",
        },
        {
            "segment_index": 1,
            "text": "来源分镜文案",
            "image_status": "pending",
            "audio_status": "completed",
            "audio_path": "output/project-a/voiceovers/source.wav",
        },
    ])
    audio_asset = temp_db.save_task_asset(
        task_id,
        "audio",
        "generated",
        path="output/project-a/voiceovers/source.wav",
        segment_index=1,
        text="来源分镜文案",
        voice_type="mimo:冰糖",
    )
    task = _task(task_id)
    monkeypatch.setattr(routes.Config, "BASE_DIR", base_dir)
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(
        routes,
        "task_manager",
        SimpleNamespace(get_task=lambda _task_id: task),
    )
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes.select_segment_asset(
            task_id,
            0,
            _request(),
            {"asset_id": audio_asset["asset_id"], "asset_type": "audio"},
        ))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "audio_text_mismatch"

    selected = asyncio.run(routes.select_segment_asset(
        task_id,
        0,
        _request(),
        {
            "asset_id": audio_asset["asset_id"],
            "asset_type": "audio",
            "confirm_text_mismatch": True,
        },
    ))
    assert selected["audio_text_mismatch"] is True
    segment = temp_db.get_segments(task_id)[0]
    assert selected["asset_id"] != audio_asset["asset_id"]
    assert selected["origin_asset_id"] == audio_asset["asset_id"]
    assert segment["selected_audio_asset_id"] == selected["asset_id"]
    assert segment["audio_mismatch_confirmed"] == 1

def test_review_first_upload_without_draft_creates_history_and_downloadable_file(
    tmp_path, temp_db, monkeypatch
):
    base_dir = tmp_path / "server"
    base_dir.mkdir()
    task_id = "review-upload"
    _create_task(temp_db, task_id)
    temp_db.save_segments(
        task_id,
        [{
            "segment_index": 7,
            "text": "第八个分镜",
            "image_prompt": "prompt",
            "image_status": "pending",
            "audio_status": "pending",
        }],
    )
    task = _task(task_id, result=None, name="无草稿项目")

    class FakeUploader:
        def upload(self, _path, storage_path=None):
            return f"/media/{storage_path}"

    monkeypatch.setattr(routes.Config, "BASE_DIR", base_dir)
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(
        routes,
        "task_manager",
        SimpleNamespace(get_task=lambda _task_id: task),
    )
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: False)
    monkeypatch.setattr(uploader_module, "LocalUploader", FakeUploader)
    upload = UploadFile(
        filename="replacement.png",
        file=io.BytesIO(b"uploaded-image"),
        headers=Headers({"content-type": "image/png"}),
    )

    result = asyncio.run(routes.upload_image(task_id, 7, upload))

    uploaded_path = Path(result["image_path"])
    assert uploaded_path.is_file()
    assert uploaded_path.read_bytes() == b"uploaded-image"
    assert uploaded_path.is_relative_to(base_dir / "output" / task_id)
    assert temp_db.get_task(task_id)["result"] is None
    segment = temp_db.get_segments(task_id)[0]
    assert segment["image_path"] == str(uploaded_path)
    assert segment["image_status"] == "completed"
    upload_assets = temp_db.list_task_assets(task_id, "upload")
    assert len(upload_assets) == 1
    assert upload_assets[0]["source"] == "upload"
    assert upload_assets[0]["segment_index"] == 7
    assert upload_assets[0]["path"] == str(uploaded_path)
    assert segment["selected_image_asset_id"] == upload_assets[0]["asset_id"]
    assert result["asset_id"] == upload_assets[0]["asset_id"]
    assert temp_db.get_task(task_id)["status"] == "awaiting_finalization"

    response = asyncio.run(routes.download_task_assets(task_id, type="upload"))
    body = asyncio.run(_stream_bytes(response))
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert names[0].startswith("uploads/08_")
        assert archive.read(names[0]) == b"uploaded-image"


def test_asset_download_resolves_all_supported_relative_roots_and_preserves_records(
    tmp_path, temp_db, monkeypatch
):
    base_dir = tmp_path / "server"
    paths = {
        "bare_output": base_dir / "output" / "bare" / "images" / "a.png",
        "bare_media": base_dir / "data" / "media" / "bare" / "audio.wav",
        "rooted_output": base_dir / "output" / "rooted" / "images" / "b.png",
        "rooted_media": base_dir / "data" / "media" / "rooted" / "audio.wav",
        "base_relative": base_dir / "legacy" / "c.png",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"must-not-download")
    task_id = "relative-download"
    _create_task(temp_db, task_id)
    temp_db.save_segments(
        task_id,
        [{
            "segment_index": 0,
            "text": "旧分镜",
            "image_prompt": "prompt",
            "image_path": "bare/images/a.png",
            "image_status": "completed",
            "audio_path": "bare/audio.wav",
            "audio_status": "completed",
        }],
    )
    records = [
        temp_db.save_task_asset(
            task_id,
            "image",
            "legacy",
            path="bare/images/a.png",
            segment_index=0,
            status="completed",
        ),
        temp_db.save_task_asset(
            task_id,
            "audio",
            "legacy",
            path="bare/audio.wav",
            segment_index=0,
            status="completed",
        ),
        temp_db.save_task_asset(
            task_id,
            "image",
            "regenerated",
            path="output/rooted/images/b.png",
            segment_index=0,
            status="completed",
        ),
        temp_db.save_task_asset(
            task_id,
            "audio",
            "regenerated",
            path="data/media/rooted/audio.wav",
            segment_index=0,
            status="completed",
        ),
        temp_db.save_task_asset(
            task_id,
            "image",
            "selected",
            path="legacy/c.png",
            segment_index=0,
            status="completed",
        ),
    ]
    traversal = temp_db.save_task_asset(
        task_id,
        "image",
        "selected",
        path="../outside.png",
        segment_index=0,
        status="completed",
    )
    before_assets = {
        item["asset_id"]: (item["path"], item["status"], item["updated_at"])
        for item in temp_db.list_task_assets(task_id)
    }
    before_segment = temp_db.get_segments(task_id)[0]
    task = _task(task_id)
    monkeypatch.setattr(routes.Config, "BASE_DIR", base_dir)
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(
        routes,
        "task_manager",
        SimpleNamespace(get_task=lambda _task_id: task),
    )

    expected = [
        paths["bare_output"],
        paths["bare_media"],
        paths["rooted_output"],
        paths["rooted_media"],
        paths["base_relative"],
    ]
    for record, expected_path in zip(records, expected):
        response = asyncio.run(
            routes.download_task_asset_file(task_id, record["asset_id"])
        )
        assert Path(response.path) == expected_path.resolve()

    with pytest.raises(HTTPException) as traversal_exc:
        asyncio.run(
            routes.download_task_asset_file(task_id, traversal["asset_id"])
        )
    assert traversal_exc.value.status_code == 404

    bundle = asyncio.run(routes.download_task_assets(task_id, type="all"))
    body = asyncio.run(_stream_bytes(bundle))
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        archived_payloads = {archive.read(name) for name in archive.namelist()}
    assert archived_payloads == {key.encode() for key in paths}
    assert b"must-not-download" not in archived_payloads

    after_assets = {
        item["asset_id"]: (item["path"], item["status"], item["updated_at"])
        for item in temp_db.list_task_assets(task_id)
    }
    after_segment = temp_db.get_segments(task_id)[0]
    assert after_assets == before_assets
    assert after_segment["image_path"] == before_segment["image_path"]
    assert after_segment["audio_path"] == before_segment["audio_path"]
    assert after_segment["updated_at"] == before_segment["updated_at"]

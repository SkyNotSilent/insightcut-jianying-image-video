import asyncio
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api import routes
from src.export.asset_package import (
    NoMaterialAssetsError,
    build_material_package,
    current_material_package,
    material_package_state,
)


def _media_file(base_dir: Path, relative: str, content: bytes) -> Path:
    path = base_dir / "data" / "media" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in {'.png','.jpg','.webp'}:
        from PIL import Image
        Image.new('RGB',(8,8),(sum(content)%255,30,80)).save(path)
    else:
        import wave
        with wave.open(str(path),'wb') as output:
            output.setparams((1,2,24000,0,'NONE','not compressed'))
            output.writeframes(b'\0\0'*6000)
    return path


def _segment(index, image=None, audio=None, text=None, duration=2.5):
    return {
        "segment_index": index,
        "text": text or f"分镜 {index + 1}",
        "image_path": str(image) if image else None,
        "audio_path": str(audio) if audio else None,
        "image_status": "completed" if image else "pending",
        "audio_status": "completed" if audio else "pending",
        "audio_voice_type": "mimo:冰糖" if audio else None,
        "duration": duration,
    }


def test_material_package_uses_current_assets_and_numeric_segment_order(tmp_path):
    image_1 = _media_file(tmp_path, "task-1/current/cover.webp", b"image-one")
    image_2 = _media_file(tmp_path, "task-1/current/second.png", b"image-two")
    image_11 = _media_file(tmp_path, "task-1/current/eleventh.jpg", b"image-eleven")
    audio_1 = _media_file(tmp_path, "task-1/current/voice.m4a", b"audio-one")
    audio_2 = _media_file(tmp_path, "task-1/current/second.wav", b"audio-two")
    audio_11 = _media_file(tmp_path, "task-1/current/eleventh.mp3", b"audio-eleven")
    segments = [
        _segment(10, image_11, audio_11, "第十一段"),
        _segment(1, image_2, audio_2, "第二段"),
        _segment(0, image_1, audio_1, "第一段"),
    ]

    result = build_material_package("task-1", "项目:测试", segments, tmp_path)

    assert result["complete"] is True
    assert result["image_count"] == 3
    assert result["audio_count"] == 3
    with zipfile.ZipFile(result["zip_path"]) as archive:
        names = archive.namelist()
        root = "项目_测试_素材包"
        assert f"{root}/images/001.webp" in names
        assert f"{root}/images/002.png" in names
        assert f"{root}/images/003.jpg" in names
        assert f"{root}/audio/001.m4a" in names
        assert archive.read(f"{root}/images/001.webp") == image_1.read_bytes()
        manifest = json.loads(archive.read(f"{root}/metadata/manifest.json"))
        assert [item["segment_index"] for item in manifest["segments"]] == [0, 1, 10]
        assert [item["text"] for item in manifest["segments"]] == ["第一段", "第二段", "第十一段"]
        assert all(not name.startswith("/") and "../" not in name for name in names)


def test_partial_package_reports_missing_and_rejects_invalid_content(tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"do-not-package")
    audio = _media_file(tmp_path, "task-2/audio/segment.wav", b"audio")
    segments = [_segment(0, outside, audio)]

    state = material_package_state("task-2", "部分项目", segments, tmp_path)
    result = build_material_package("task-2", "部分项目", segments, tmp_path)

    assert state["available"] is True
    assert state["complete"] is False
    assert state["image_count"] == 0
    assert state["missing_image_orders"] == [1]
    with zipfile.ZipFile(result["zip_path"]) as archive:
        root = "部分项目_素材包"
        assert f"{root}/audio/001.wav" in archive.namelist()
        assert not any(name.startswith(f"{root}/images/001") for name in archive.namelist())
        manifest = json.loads(archive.read(f"{root}/metadata/manifest.json"))
        assert manifest["segments"][0]["image_missing_reason"] == "invalid_content"


def test_package_cache_invalidates_when_current_file_changes(tmp_path):
    image = _media_file(tmp_path, "task-3/image.png", b"first")
    segments = [_segment(0, image)]

    first = build_material_package("task-3", "缓存项目", segments, tmp_path)
    second = build_material_package("task-3", "缓存项目", segments, tmp_path)
    assert first["cached"] is False
    assert second["cached"] is True

    from PIL import Image
    Image.new("RGB",(9,9),"blue").save(image)
    state = material_package_state("task-3", "缓存项目", segments, tmp_path)
    assert state["snapshot_key"] != first["snapshot_key"]
    assert state["package_ready"] is False
    assert current_material_package(
        "task-3", "缓存项目", segments, tmp_path, first["snapshot_key"]
    ) is None

    updated = build_material_package("task-3", "缓存项目", segments, tmp_path)
    assert updated["cached"] is False
    with zipfile.ZipFile(updated["zip_path"]) as archive:
        assert archive.read("缓存项目_素材包/images/001.png") == image.read_bytes()


def test_package_requires_at_least_one_current_file(tmp_path):
    with pytest.raises(NoMaterialAssetsError, match="暂无可打包素材"):
        build_material_package("task-4", "空项目", [_segment(0)], tmp_path)


def test_export_state_exposes_partial_materials_without_draft_result(tmp_path, monkeypatch):
    image = _media_file(tmp_path, "task-5/image.png", b"image")
    segments = [_segment(0, image)]
    task = SimpleNamespace(
        task_id="task-5",
        name="失败项目",
        theme="失败项目",
        ratio="16:9",
        status="failed",
        result=None,
    )
    monkeypatch.setattr(routes.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(routes.task_manager, "get_task", lambda _task_id: task)
    monkeypatch.setattr(routes.mysql_client, "get_segments", lambda _task_id: segments)
    monkeypatch.setattr(routes.mysql_client, "get_task", lambda _task_id: {"ratio": "16:9"})

    state = asyncio.run(routes.get_export_state("task-5", None))

    assert state["outputs"]["mp4"]["available"] is False
    assert state["outputs"]["draft"]["available"] is False
    assert state["outputs"]["materials"]["available"] is True
    assert state["outputs"]["materials"]["complete"] is False
    assert state["outputs"]["materials"]["image_count"] == 1


def test_material_export_job_does_not_require_draft_result(tmp_path, monkeypatch):
    image = _media_file(tmp_path, "task-6/image.png", b"image")
    audio = _media_file(tmp_path, "task-6/audio.wav", b"audio")
    segments = [_segment(0, image, audio)]
    task = SimpleNamespace(
        task_id="task-6",
        name="中断项目",
        theme="中断项目",
        ratio="9:16",
        status="interrupted",
        result=None,
    )
    monkeypatch.setattr(routes.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(routes.task_manager, "get_task", lambda _task_id: task)
    monkeypatch.setattr(routes.mysql_client, "get_segments", lambda _task_id: segments)

    job = routes._create_export_job("task-6", "materials", {})
    routes._run_export_job(job["job_id"], "materials", True, {})
    completed = routes._export_job_snapshot(job["job_id"])

    assert completed["status"] == "completed"
    assert completed["result"]["target"] == "materials"
    assert completed["result"]["complete"] is True
    assert Path(completed["result"]["zip_path"]).is_file()
    assert completed["result"]["download_url"].startswith(
        "/ai/native/video/kepu/tasks/task-6/download-materials?"
    )

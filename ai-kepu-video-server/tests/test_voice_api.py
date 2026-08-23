import asyncio
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from src.api import routes
from src.api.models import RegenerateAudioRequest, TaskStatus, TTSOptions
from src.api.task_manager import TaskManager
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient
from src.draft.voice_preview import PRESET_VOICE_PREVIEW_TEXT


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


@pytest.fixture
def tts_config():
    return {
        "provider": "mimo",
        "enabled_providers": ["doubao", "mimo"],
        "preview_text": "这是试听。",
        "auth_method": "api_key",
        "api_url": "https://doubao.invalid/tts",
        "api_key": "doubao-key",
        "cluster": "volcano_tts",
        "default_voice": "zh_male_jieshuoxiaoming_moon_bigtts",
        "speed_level": "normal",
        "volume_ratio": 1.0,
        "mimo": {
            "base_url": "https://mimo.invalid/v1",
            "api_key": "mimo-key",
            "model": "mimo-v2.5-tts",
            "clone_model": "mimo-v2.5-tts-voiceclone",
            "default_voice": "冰糖",
            "format": "wav",
            "style_prompt": "自然清晰",
            "speed_level": "normal",
        },
    }


def test_config_readiness_reports_only_safe_missing_fields(monkeypatch):
    secret = "sk-readiness-secret-value"
    monkeypatch.setattr(routes.Config, "load_model_config", lambda: {
        "llm": {
            "provider": "custom",
            "base_url": "",
            "api_key": "",
            "model": "",
            "provider_options": {},
        },
        "image": {
            "api_url": "https://image.invalid/v1",
            "api_key": "",
            "model": "agnes-image-2.1-flash",
        },
        "tts": {
            "provider": "mimo",
            "enabled_providers": ["mimo"],
            "api_key": secret,
            "mimo": {
                "base_url": "https://mimo.invalid/v1",
                "api_key": secret,
                "model": "mimo-v2.5-tts",
                "default_voice": "冰糖",
            },
        },
    })

    readiness = routes._config_readiness("mimo:冰糖")

    assert readiness["status"] == "not_ready"
    by_key = {item["key"]: item for item in readiness["items"]}
    assert by_key["llm"]["status"] == "not_ready"
    assert set(by_key["llm"]["missing"]) == {"Base URL", "API Key", "Model"}
    assert by_key["image"]["missing"] == ["API Key"]
    assert by_key["tts"]["status"] == "ready"
    serialized = json.dumps(readiness, ensure_ascii=False)
    assert secret not in serialized
    assert "Authorization" not in serialized


def test_config_readiness_ignores_an_unselected_tts_provider(monkeypatch):
    monkeypatch.setattr(routes.Config, "load_model_config", lambda: {
        "llm": {
            "provider": "custom",
            "base_url": "https://llm.invalid/v1",
            "api_key": "llm-key",
            "model": "openai/test-model",
            "provider_options": {},
        },
        "image": {
            "api_url": "https://image.invalid/v1",
            "api_key": "image-key",
            "model": "agnes-image-2.1-flash",
        },
        "tts": {
            "provider": "mimo",
            "enabled_providers": ["mimo"],
            "auth_method": "access_token",
            "api_url": "",
            "appid": "",
            "token": "",
            "mimo": {
                "base_url": "https://mimo.invalid/v1",
                "api_key": "mimo-key",
                "model": "mimo-v2.5-tts",
                "default_voice": "冰糖",
            },
        },
    })

    readiness = routes._config_readiness("mimo:冰糖")

    assert readiness["status"] == "ready"
    assert readiness["can_continue"] is True
    assert all(item["status"] == "ready" for item in readiness["items"])


def wav_bytes(seconds=0.05):
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x01\x00" * int(seconds * 24000))
    return target.getvalue()


def test_tts_models_validate_five_speed_levels_and_provider_fields():
    options = TTSOptions(speed_level="fast", volume_ratio=1.8, style_prompt="有感情")
    assert options.speed_level == "fast"
    assert options.volume_ratio == 1.8
    with pytest.raises(Exception):
        TTSOptions(speed_level="impossible")
    with pytest.raises(Exception):
        TTSOptions(volume_ratio=3)


def test_catalog_endpoint_filters_providers_and_bulk_availability(
    temp_db, monkeypatch, tts_config
):
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(lambda cls: tts_config))

    mimo = asyncio.run(routes.get_voices(provider="mimo", include_disabled=True))
    assert len([voice for voice in mimo if voice["kind"] == "preset"]) == 9
    assert all(voice["provider"] == "mimo" for voice in mimo)

    result = asyncio.run(
        routes.update_voice_availability(
            {"voice_keys": ["mimo:茉莉", "doubao:zh_male_jieshuoxiaoming_moon_bigtts"]}
        )
    )
    assert result["enabled_voice_keys"] == [
        "doubao:zh_male_jieshuoxiaoming_moon_bigtts",
        "mimo:茉莉",
    ]
    enabled = asyncio.run(routes.get_voices(provider=None, include_disabled=False))
    assert {voice["id"] for voice in enabled} == set(result["enabled_voice_keys"])


def test_preset_preview_endpoint_uses_fixed_copy_and_options(tmp_path, temp_db, monkeypatch, tts_config):
    captured = {}

    class FakePreviewService:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def generate(self, voice_type, text, tts_options, config_override=None):
            captured.update({
                "voice_type": voice_type,
                "text": text,
                "tts_options": tts_options,
                "config_override": config_override,
            })
            return {"url": "/media/_voice_previews/one.wav", "path": str(tmp_path / "one.wav"), "cached": False}

    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes, "VoicePreviewService", FakePreviewService)
    monkeypatch.setattr(routes.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(lambda cls: tts_config))

    result = asyncio.run(
        routes.preview_voice(
            {
                "voice_type": "mimo:冰糖",
                "text": "未保存配置试听",
                "tts_options": {"speed_level": "slow"},
                "config_override": {"mimo": {"style_prompt": "轻松"}},
            }
        )
    )
    assert result["url"].endswith("one.wav")
    assert captured["text"] == PRESET_VOICE_PREVIEW_TEXT
    assert captured["tts_options"] == {"speed_level": "slow"}
    assert captured["config_override"] == {"mimo": {"style_prompt": "轻松"}}


def test_confirming_voice_does_not_rewrite_legacy_single_value_style(temp_db, monkeypatch):
    temp_db.create_task(
        "legacy-style-task",
        "原始文案",
        "cinematic",
        200,
        execution_mode="review_first",
        script_policy="verbatim",
    )
    temp_db.save_segments(
        "legacy-style-task",
        [{"segment_index": 0, "text": "第一段", "image_prompt": "已有提示词"}],
    )
    temp_db.update_task_workflow(
        "legacy-style-task", "awaiting_confirmation", status="awaiting_confirmation"
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "invalidate_task_cache", lambda _task_id: None)

    result = asyncio.run(
        routes.update_task_workspace_settings(
            "legacy-style-task",
            {
                "voice_type": "mimo:冰糖",
                "tts_options": {"speed_level": "normal"},
                "voice_confirmed": True,
                "expected_plan_version": 0,
            },
        )
    )

    task = temp_db.get_task("legacy-style-task")
    segment = temp_db.get_segments("legacy-style-task")[0]
    assert result["stage"] != "planning"
    assert task["style"] == "cinematic"
    assert segment["image_prompt"] == "已有提示词"
    assert segment["prompt_status"] == "completed"


def test_style_change_marks_the_plan_for_manual_resume_without_starting_models(
    tmp_path, temp_db, monkeypatch
):
    image = tmp_path / "ready.png"
    audio = tmp_path / "ready.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    temp_db.create_task(
        "style-change",
        "原文",
        "知识科普|电影质感",
        200,
        execution_mode="review_first",
    )
    temp_db.save_segments("style-change", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "电影画面",
        "prompt_status": "completed",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    temp_db.update_task_workflow(
        "style-change", "ready", status="completed", current_step="completed"
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "invalidate_task_cache", lambda _task_id: None)
    model_starts = []
    monkeypatch.setattr(
        routes.task_executor,
        "resume_task",
        lambda task_id: model_starts.append(task_id) or "started",
    )

    result = asyncio.run(routes.update_task_workspace_settings(
        "style-change",
        {"visual_style": "国风", "expected_plan_version": 0},
    ))

    task = temp_db.get_task("style-change")
    segment = temp_db.get_segments("style-change")[0]
    assert result["stage"] == "interrupted"
    assert model_starts == []
    assert task["workflow_phase"] == "planning"
    assert task["current_step"] == "image_prompt_generation"
    assert segment["image_prompt"] == ""
    assert segment["prompt_status"] == "pending"
    assert segment["image_status"] == "stale"


def test_workspace_reports_progressive_planning_step_and_prompt_counts(
    temp_db, monkeypatch
):
    temp_db.create_task(
        "planning-task",
        "原始主题",
        "知识科普|电影质感",
        200,
        execution_mode="review_first",
    )
    temp_db.update_task_status("planning-task", "processing", "text_generation")
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    text_stage = asyncio.run(routes.get_task_workspace("planning-task", request))
    assert text_stage["planning_step"] == "text_generation"
    assert text_stage["script_text"] == ""
    assert text_stage["progress"]["prompts_total"] == 0

    temp_db.save_task_checkpoint(
        "planning-task", script_text="完整文案", summary="摘要", input_mode="theme"
    )
    temp_db.save_segments(
        "planning-task",
        [
            {"segment_index": 0, "text": "第一段", "image_prompt": "prompt", "prompt_status": "completed"},
            {"segment_index": 1, "text": "第二段", "prompt_status": "processing"},
            {"segment_index": 2, "text": "第三段", "prompt_status": "failed", "prompt_error": "failed"},
        ],
    )
    temp_db.update_task_status(
        "planning-task", "processing", "image_prompt_generation"
    )

    prompt_stage = asyncio.run(routes.get_task_workspace("planning-task", request))
    assert prompt_stage["planning_step"] == "image_prompt_generation"
    assert prompt_stage["script_text"] == "完整文案"
    assert prompt_stage["progress"] == {
        "prompts_ready": 1,
        "prompts_total": 3,
        "prompts_processing": 1,
        "prompts_failed": 1,
        "images_ready": 0,
        "audio_ready": 0,
    }
    assert prompt_stage["voice_confirmed"] is False


@pytest.mark.parametrize(
    ("task_id", "workflow_phase", "current_step", "with_segments"),
    [
        ("asset-interrupted", "generating_assets", "image_generation", True),
        ("text-interrupted", "planning", "text_generation", False),
    ],
)
def test_workspace_interruption_overrides_stale_workflow_phase_and_can_resume(
    temp_db,
    monkeypatch,
    task_id,
    workflow_phase,
    current_step,
    with_segments,
):
    temp_db.create_task(
        task_id,
        "原始主题",
        "知识科普|电影质感",
        200,
        execution_mode="review_first",
    )
    if with_segments:
        temp_db.save_segments(
            task_id,
            [{"segment_index": 0, "text": "第一段", "image_prompt": "prompt"}],
        )
    temp_db.update_task_workflow(
        task_id,
        workflow_phase,
        status="interrupted",
        current_step=current_step,
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    workspace = asyncio.run(routes.get_task_workspace(task_id, request))

    assert workspace["stage"] == "interrupted"
    assert workspace["planning_step"] is None
    assert workspace["can_resume"] is True


def test_workspace_reports_recovery_and_delivery_capabilities_from_real_files(
    tmp_path, temp_db, monkeypatch
):
    image = tmp_path / "segment.png"
    audio = tmp_path / "segment.wav"
    draft = tmp_path / "draft"
    audio.write_bytes(b"audio")
    draft.mkdir()
    temp_db.create_task(
        "asset-health",
        "原始主题",
        "知识科普|电影质感",
        200,
        execution_mode="review_first",
    )
    temp_db.save_task_checkpoint(
        "asset-health",
        script_text="第一段",
        summary="摘要",
        input_mode="theme",
        workflow_phase="ready",
        voice_confirmed=1,
    )
    temp_db.save_segments(
        "asset-health",
        [{
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "prompt",
            "image_path": str(image),
            "image_url": "http://testserver/files/segment.png",
            "image_status": "completed",
            "audio_path": str(audio),
            "audio_url": "http://testserver/files/segment.wav",
            "audio_status": "completed",
        }],
    )
    temp_db.save_task_result("asset-health", str(draft), 1)
    temp_db.update_task_workflow(
        "asset-health", "ready", status="completed", current_step="completed"
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "invalidate_task_cache", lambda _task_id: None)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    missing_image = asyncio.run(routes.get_task_workspace("asset-health", request))

    assert missing_image["stage"] == "interrupted"
    assert missing_image["segments"][0]["image_status"] == "failed"
    assert missing_image["health"]["missing_images"] == 1
    assert missing_image["recovery"]["mode"] == "retry_assets"
    assert missing_image["capabilities"] == {
        "instant_preview": False,
        "full_video": False,
        "enter_export": True,
        "material_export": True,
        "retry_failed_assets": True,
        "update_stale_assets": False,
        "retry_selected_asset": True,
        "finalize": False,
    }
    assert temp_db.get_task("asset-health")["status"] == "interrupted"

    image.write_bytes(b"image")
    temp_db.update_task_workflow(
        "asset-health", "generating_assets", status="interrupted", current_step="draft_building"
    )
    draft.rmdir()

    assets_complete = asyncio.run(routes.get_task_workspace("asset-health", request))

    assert assets_complete["health"]["assets_complete"] is True
    assert assets_complete["recovery"]["mode"] == "finalize"
    assert assets_complete["capabilities"]["enter_export"] is True
    assert assets_complete["capabilities"]["full_video"] is False


def test_awaiting_confirmation_does_not_offer_asset_repair_for_pending_media(
    temp_db, monkeypatch
):
    temp_db.create_task(
        "awaiting-confirmation",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.save_segments("awaiting-confirmation", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "prompt_status": "completed",
        "image_status": "pending",
        "audio_status": "pending",
    }])
    temp_db.update_task_workflow(
        "awaiting-confirmation",
        "awaiting_confirmation",
        status="awaiting_confirmation",
        current_step="awaiting_confirmation",
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    workspace = asyncio.run(routes.get_task_workspace("awaiting-confirmation", request))

    assert workspace["stage"] == "awaiting_confirmation"
    assert workspace["recovery"]["allowed"] is False
    assert workspace["capabilities"]["retry_failed_assets"] is False
    assert workspace["capabilities"]["update_stale_assets"] is False


def test_workspace_separates_stale_assets_from_failed_recovery_targets(
    tmp_path, temp_db, monkeypatch
):
    image = tmp_path / "stale.png"
    audio = tmp_path / "ready.wav"
    draft = tmp_path / "draft"
    image.write_bytes(b"old-image")
    audio.write_bytes(b"audio")
    draft.mkdir()
    temp_db.create_task(
        "stale-only",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.save_segments("stale-only", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "stale",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    temp_db.save_task_result("stale-only", str(draft), 1)
    temp_db.update_task_workflow(
        "stale-only", "ready", status="completed", current_step="completed"
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "invalidate_task_cache", lambda _task_id: None)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    workspace = asyncio.run(routes.get_task_workspace("stale-only", request))

    assert workspace["health"]["stale_images"] == 1
    assert workspace["health"]["failed_images"] == 0
    assert workspace["recovery"]["mode"] == "update_stale_assets"
    assert workspace["recovery"]["targets"] == [{
        "segment_index": 0,
        "asset_type": "image",
        "status": "stale",
        "reason": "图片与当前预案不一致，需要更新",
    }]
    assert workspace["capabilities"]["retry_failed_assets"] is False
    assert workspace["capabilities"]["update_stale_assets"] is True
    assert workspace["capabilities"]["finalize"] is False

    with pytest.raises(HTTPException) as exc_info:
        routes._resolve_retry_targets(
            temp_db.get_segments("stale-only"), "failed", None
        )
    assert exc_info.value.status_code == 409


@pytest.mark.parametrize(
    ("case", "segments_kind", "expected_mode"),
    [
        ("no-segments", "none", "restart_planning"),
        ("missing-prompt", "prompt", "resume_planning"),
        ("missing-image", "asset", "retry_assets"),
        ("missing-draft", "draft", "finalize"),
    ],
)
def test_failed_full_tasks_expose_recovery_from_their_real_checkpoint(
    tmp_path, temp_db, monkeypatch, case, segments_kind, expected_mode
):
    task_id = f"full-{case}"
    temp_db.create_task(
        task_id,
        "旧任务主题",
        "知识科普|电影质感",
        100,
        execution_mode="full",
    )
    if segments_kind != "none":
        image = tmp_path / f"{case}.png"
        audio = tmp_path / f"{case}.wav"
        if segments_kind in {"prompt", "draft"}:
            image.write_bytes(b"image")
        if segments_kind in {"prompt", "asset", "draft"}:
            audio.write_bytes(b"audio")
        temp_db.save_segments(task_id, [{
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "" if segments_kind == "prompt" else "prompt",
            "image_path": str(image) if image.exists() else None,
            "image_status": "completed" if image.exists() else "failed",
            "audio_path": str(audio),
            "audio_status": "completed",
        }])
    temp_db.update_task_workflow(
        task_id,
        "generating_assets" if segments_kind in {"asset", "draft"} else "planning",
        status="failed",
        current_step="image_generation" if segments_kind in {"asset", "draft"} else "text_generation",
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    workspace = asyncio.run(routes.get_task_workspace(task_id, request))

    assert workspace["execution_mode"] == "full"
    assert workspace["recovery"]["mode"] == expected_mode
    assert temp_db.get_task(task_id)["execution_mode"] == "full"


def test_duplicate_finalize_request_reuses_the_active_operation(
    tmp_path, temp_db, monkeypatch
):
    image = tmp_path / "ready.png"
    audio = tmp_path / "ready.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    temp_db.create_task(
        "duplicate-finalize",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.save_segments("duplicate-finalize", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    task_row = temp_db.get_task("duplicate-finalize")
    segments = temp_db.get_segments("duplicate-finalize")
    snapshot_key = routes._plan_fingerprint(task_row, segments)
    active = temp_db.create_task_operation(
        "duplicate-finalize",
        "finalize",
        "active-finalize",
        snapshot_key,
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    temp_db.update_task_operation(active["operation_id"], state="running")
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    starts = []
    monkeypatch.setattr(
        routes.task_executor,
        "finalize_task",
        lambda *args: starts.append(args) or "started",
    )
    response = Response()

    result = asyncio.run(routes.finalize_task_workspace(
        "duplicate-finalize", response, {"snapshot_key": snapshot_key}
    ))

    assert response.status_code == 200
    assert result["operation_id"] == active["operation_id"]
    assert starts == []


def test_finalize_does_not_reuse_an_active_operation_from_an_old_snapshot(
    tmp_path, temp_db, monkeypatch
):
    image = tmp_path / "ready.png"
    audio = tmp_path / "ready.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    temp_db.create_task(
        "snapshot-finalize",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.save_segments("snapshot-finalize", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    task_row = temp_db.get_task("snapshot-finalize")
    segments = temp_db.get_segments("snapshot-finalize")
    snapshot_key = routes._plan_fingerprint(task_row, segments)
    active = temp_db.create_task_operation(
        "snapshot-finalize",
        "finalize",
        "old-snapshot-finalize",
        "older-snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )["operation"]
    temp_db.update_task_operation(active["operation_id"], state="running")
    monkeypatch.setattr(routes, "mysql_client", temp_db)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes.finalize_task_workspace(
            "snapshot-finalize", Response(), {"snapshot_key": snapshot_key}
        ))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "operation_running"
    assert exc_info.value.detail["operation_id"] == active["operation_id"]


def test_orphaned_finalize_operation_becomes_finalize_failed_recovery(
    tmp_path, temp_db, monkeypatch
):
    image = tmp_path / "ready.png"
    audio = tmp_path / "ready.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    temp_db.create_task(
        "orphan-finalize",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.save_segments("orphan-finalize", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_path": str(image),
        "image_status": "completed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    temp_db.create_task_operation(
        "orphan-finalize",
        "finalize",
        "orphan-finalize-key",
        "snapshot",
        [{"asset_type": "draft", "status": "running"}],
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: False)
    monkeypatch.setattr(routes.task_manager, "invalidate_task_cache", lambda _task_id: None)
    monkeypatch.setattr(routes.task_manager, "fail_stale_task_data", lambda _row: False)
    request = Request({
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/workspace",
        "headers": [],
    })

    workspace = asyncio.run(routes.get_task_workspace("orphan-finalize", request))

    task = temp_db.get_task("orphan-finalize")
    assert workspace["active_operation"] is None
    assert workspace["stage"] == "interrupted"
    assert workspace["recovery"]["mode"] == "finalize_failed"
    assert task["current_step"] == "finalize_failed"


def test_retry_assets_route_resolves_only_failed_targets(
    tmp_path, temp_db, monkeypatch
):
    temp_db.create_task(
        "exact-retry-route",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    image = tmp_path / "ready.png"
    audio0 = tmp_path / "ready-0.wav"
    audio1 = tmp_path / "ready-1.wav"
    image.write_bytes(b"image")
    audio0.write_bytes(b"audio")
    audio1.write_bytes(b"audio")
    temp_db.save_segments("exact-retry-route", [
        {
            "segment_index": 0,
            "text": "第一段",
            "image_prompt": "prompt-0",
            "image_path": str(image),
            "image_status": "completed",
            "audio_path": str(audio0),
            "audio_status": "completed",
        },
        {
            "segment_index": 1,
            "text": "第二段",
            "image_prompt": "prompt-1",
            "image_status": "failed",
            "image_error": "生成失败",
            "audio_path": str(audio1),
            "audio_status": "completed",
        },
    ])
    temp_db.update_task_workflow(
        "exact-retry-route",
        "generating_assets",
        status="interrupted",
        current_step="image_generation",
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    observed = {}

    def start_retry(task_id, operation_id, targets):
        observed.update({
            "task_id": task_id,
            "operation_id": operation_id,
            "targets": targets,
        })
        return "started"

    monkeypatch.setattr(routes.task_executor, "retry_assets", start_retry)
    task_row = temp_db.get_task("exact-retry-route")
    segments = temp_db.get_segments("exact-retry-route")
    response = Response()

    result = asyncio.run(routes.retry_task_assets(
        "exact-retry-route",
        response,
        {
            "snapshot_key": routes._plan_fingerprint(task_row, segments),
            "scope": "failed",
        },
    ))

    assert response.status_code == 202
    assert [(item["segment_index"], item["asset_type"]) for item in observed["targets"]] == [(1, "image")]
    assert result["total"] == 1
    assert result["targets"][0]["mode"] == "retry"


def test_resume_route_rejects_asset_repair_and_never_calls_full_executor(
    tmp_path, temp_db, monkeypatch
):
    temp_db.create_task(
        "resume-guard",
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    audio = tmp_path / "ready.wav"
    audio.write_bytes(b"audio")
    temp_db.save_segments("resume-guard", [{
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "image_status": "failed",
        "audio_path": str(audio),
        "audio_status": "completed",
    }])
    temp_db.update_task_workflow(
        "resume-guard",
        "generating_assets",
        status="interrupted",
        current_step="image_generation",
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(
        routes.task_manager,
        "get_task",
        lambda _task_id: SimpleNamespace(status=TaskStatus.INTERRUPTED),
    )
    called = []
    monkeypatch.setattr(routes.task_executor, "resume_task", lambda task_id: called.append(task_id))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes.resume_task("resume-guard", Response()))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "retry_assets"
    assert called == []


@pytest.mark.parametrize(
    ("stopped", "expected_status", "expected_outcome"),
    [
        (True, 200, "cancelled"),
        (False, 202, "cancel_requested"),
    ],
)
def test_cancel_route_requests_executor_stop_and_reports_drain_state(
    temp_db,
    monkeypatch,
    stopped,
    expected_status,
    expected_outcome,
):
    temp_db.create_task("cancel-route", "主题", "知识科普|电影质感", 100)
    temp_db.update_task_status("cancel-route", "processing", "image_generation")
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: True)
    calls = []

    def stop_task(task_id, timeout):
        calls.append((task_id, timeout))
        if stopped:
            temp_db.update_task_status(
                task_id,
                "interrupted",
                "image_generation",
                "本次操作已取消。",
                error_code="cancelled",
            )
        return stopped

    monkeypatch.setattr(routes.task_executor, "cancel_task", stop_task)
    response = Response()

    result = asyncio.run(routes.cancel_task("cancel-route", response))

    assert response.status_code == expected_status
    assert result["outcome"] == expected_outcome
    assert calls == [("cancel-route", 30)]
    if stopped:
        assert result["status"] == "interrupted"


def test_cancel_route_is_idempotent_when_no_worker_is_running(temp_db, monkeypatch):
    temp_db.create_task("cancel-stopped", "主题", "知识科普|电影质感", 100)
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: False)
    monkeypatch.setattr(
        routes.task_executor,
        "cancel_task",
        lambda *_args, **_kwargs: pytest.fail("已停止任务不应再请求取消"),
    )

    result = asyncio.run(routes.cancel_task("cancel-stopped", Response()))

    assert result["outcome"] == "already_stopped"
    assert result["status"] == "pending"


def test_resegment_moves_task_to_planning_before_resuming(temp_db, monkeypatch):
    temp_db.create_task(
        "resegment-state",
        "原始主题",
        "知识科普|电影质感",
        200,
        execution_mode="review_first",
    )
    temp_db.save_task_checkpoint(
        "resegment-state",
        script_text="第一句。第二句。",
        workflow_phase="awaiting_confirmation",
    )
    temp_db.save_segments(
        "resegment-state",
        [{"segment_index": 0, "text": "旧分镜", "image_prompt": "old"}],
    )
    temp_db.update_task_workflow(
        "resegment-state",
        "awaiting_confirmation",
        status="awaiting_confirmation",
        current_step="awaiting_confirmation",
    )
    observed = {}
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.task_manager, "invalidate_task_cache", lambda _task_id: None)

    def resume(task_id):
        observed.update(temp_db.get_task(task_id))
        return "started"

    monkeypatch.setattr(routes.task_executor, "resume_task", resume)

    result = asyncio.run(routes.resegment_task_workspace(
        "resegment-state",
        {"script_text": "第一句。第二句。", "expected_plan_version": 0},
    ))

    assert result["outcome"] == "started"
    assert observed["status"] == "interrupted"
    assert observed["workflow_phase"] == "planning"
    assert observed["current_step"] == "image_prompt_generation"


def test_new_task_accepts_known_preset_even_when_legacy_checkmark_is_off(
    temp_db, monkeypatch, tts_config
):
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(lambda cls: tts_config))

    voice_key = "doubao:zh_male_chenwendongge_moon_bigtts"
    assert temp_db.find_tts_voice("doubao", "zh_male_chenwendongge_moon_bigtts")["is_enabled"] is False
    assert routes._resolve_new_task_voice(voice_key) == voice_key


def test_preview_reports_provider_authorization_instead_of_generic_failure(
    tmp_path, temp_db, monkeypatch, tts_config
):
    class ForbiddenPreviewService:
        def __init__(self, **_kwargs):
            pass

        def generate(self, *_args, **_kwargs):
            error = RuntimeError("forbidden")
            error.response = SimpleNamespace(status_code=403)
            raise error

    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes, "VoicePreviewService", ForbiddenPreviewService)
    monkeypatch.setattr(routes.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(lambda cls: tts_config))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes.preview_voice({"voice_type": "doubao:missing-permission"}))
    assert caught.value.status_code == 409
    assert "未授权该音色" in caught.value.detail


def test_clone_multipart_lifecycle_requires_preview_before_enable(
    tmp_path, temp_db, monkeypatch, tts_config
):
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(lambda cls: tts_config))
    upload = UploadFile(filename="voice.wav", file=io.BytesIO(wav_bytes()))

    created = asyncio.run(routes.create_voice_clone("Creator", True, upload))
    clone_id = created["clone_id"]
    assert created["status"] == "draft"
    with pytest.raises(HTTPException, match="试听"):
        asyncio.run(routes.update_voice_clone(clone_id, {"is_enabled": True}))

    class FakePreviewService:
        def __init__(self, **kwargs):
            self.root = tmp_path / "data" / "media" / "_voice_previews"
            self.root.mkdir(parents=True, exist_ok=True)

        def generate(self, *args, **kwargs):
            path = self.root / "clone.wav"
            path.write_bytes(wav_bytes())
            return {"url": "/media/_voice_previews/clone.wav", "path": str(path), "cached": False}

    monkeypatch.setattr(routes, "VoicePreviewService", FakePreviewService)
    ready = asyncio.run(routes.preview_voice_clone(clone_id, {"text": "这是我的声音"}))
    assert ready["clone"]["status"] == "ready"

    enabled = asyncio.run(routes.update_voice_clone(clone_id, {"is_enabled": True}))
    assert enabled["is_enabled"] is True
    renamed = asyncio.run(routes.update_voice_clone(clone_id, {"name": "Creator 2"}))
    assert renamed["name"] == "Creator 2"

    deleted = asyncio.run(routes.delete_voice_clone(clone_id))
    assert deleted["outcome"] == "deleted"


def test_task_manager_persists_voice_and_tts_option_snapshot(
    temp_db, monkeypatch
):
    from src.api import task_manager as task_manager_module

    monkeypatch.setattr(task_manager_module, "db_client", temp_db)
    manager = TaskManager()
    task_id = manager.create_task(
        "文案",
        "知识科普",
        100,
        voice_type="mimo:冰糖",
        tts_options={"speed_level": "slow", "style_prompt": "平静"},
    )

    row = temp_db.get_task(task_id)
    assert row["voice_type"] == "mimo:冰糖"
    assert json.loads(row["tts_options_json"]) == {
        "speed_level": "slow",
        "style_prompt": "平静",
    }
    assert manager.get_task(task_id).to_response().tts_options.speed_level == "slow"


def test_segment_audio_snapshot_columns_round_trip(temp_db):
    temp_db.create_task(
        "task-1",
        "文案",
        "知识科普",
        100,
        voice_type="mimo:冰糖",
        tts_options={"speed_level": "normal"},
    )
    temp_db.save_segments("task-1", [{"segment_index": 0, "text": "第一段"}])
    assert temp_db.update_segment(
        "task-1",
        0,
        {
            "audio_voice_type": "doubao:zh_male_jieshuoxiaoming_moon_bigtts",
            "audio_tts_options_json": json.dumps({"speed_level": "fast"}),
        },
    )
    segment = temp_db.get_segments("task-1")[0]
    assert segment["audio_voice_type"].startswith("doubao:")
    assert json.loads(segment["audio_tts_options_json"])["speed_level"] == "fast"


def test_global_tts_changes_only_stale_fields_not_overridden_by_segment(
    temp_db, monkeypatch
):
    voice_type = "doubao:zh_female_wanwanxiaohe_moon_bigtts"
    temp_db.create_task(
        "segment-speed-override",
        "文案",
        "知识科普|电影质感",
        100,
        voice_type=voice_type,
        tts_options={"speed_level": "normal", "volume_ratio": 1.0},
        execution_mode="review_first",
    )
    temp_db.save_segments("segment-speed-override", [{
        "segment_index": 0,
        "text": "第一段",
        "audio_path": "existing.wav",
        "audio_status": "completed",
        "audio_tts_options_json": json.dumps({
            "speed_level": "slow",
            "_segment_override": True,
        }),
    }])
    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(
        routes.task_manager, "invalidate_task_cache", lambda _task_id: None
    )

    asyncio.run(routes.update_task_workspace_settings(
        "segment-speed-override",
        {
            "tts_options": {"speed_level": "normal", "volume_ratio": 1.2},
            "expected_plan_version": 0,
        },
    ))
    assert temp_db.get_segments("segment-speed-override")[0]["audio_status"] == "stale"

    temp_db.update_segment(
        "segment-speed-override", 0, {"audio_status": "completed"}
    )
    asyncio.run(routes.update_task_workspace_settings(
        "segment-speed-override",
        {
            "tts_options": {"speed_level": "fast", "volume_ratio": 1.2},
            "expected_plan_version": 1,
        },
    ))
    segment = temp_db.get_segments("segment-speed-override")[0]
    assert segment["audio_status"] == "completed"
    assert json.loads(segment["audio_tts_options_json"])["speed_level"] == "slow"


def test_regenerate_audio_request_keeps_query_compatibility_shape():
    body = RegenerateAudioRequest(
        voice_type="mimo:茉莉",
        tts_options=TTSOptions(speed_level="very_slow"),
    )
    assert body.voice_type == "mimo:茉莉"
    assert body.tts_options.speed_level == "very_slow"


def test_regenerate_audio_legacy_query_delegates_to_precise_operation(
    temp_db, monkeypatch, tts_config
):
    temp_db.create_task(
        "task-query",
        "文案",
        "知识科普",
        100,
        ratio="16:9",
        voice_type="mimo:冰糖",
        tts_options={"speed_level": "slow", "style_prompt": "平静"},
    )
    temp_db.save_segments(
        "task-query",
        [{
            "segment_index": 4,
            "text": "这是第一段",
            "audio_status": "failed",
            "audio_error": "previous failure",
        }],
    )
    task = SimpleNamespace(
        task_id="task-query",
        theme="文案",
        ratio="16:9",
        voice_type="mimo:冰糖",
        tts_options={"speed_level": "slow", "style_prompt": "平静"},
        result=None,
    )
    captured = {}

    def start_retry(task_id, operation_id, targets):
        captured.update({
            "task_id": task_id,
            "operation_id": operation_id,
            "targets": targets,
        })
        return "started"

    monkeypatch.setattr(routes, "mysql_client", temp_db)
    monkeypatch.setattr(routes, "task_manager", SimpleNamespace(get_task=lambda _task_id: task))
    monkeypatch.setattr(routes.Config, "tts_config", classmethod(lambda cls: tts_config))
    monkeypatch.setattr(routes.task_runtime, "is_running", lambda _task_id: False)
    monkeypatch.setattr(routes, "task_executor", SimpleNamespace(retry_assets=start_retry))
    response = Response()

    result = asyncio.run(
        routes.regenerate_audio(
            "task-query",
            4,
            response,
            payload=None,
            voice_type="doubao:zh_male_jieshuoxiaoming_moon_bigtts",
        )
    )

    assert response.status_code == 202
    assert response.headers["deprecation"] == "true"
    assert result["kind"] == "retry_assets"
    assert captured["task_id"] == "task-query"
    assert len(captured["targets"]) == 1
    target = captured["targets"][0]
    assert target["asset_type"] == "audio"
    assert target["voice_type"].startswith("doubao:")
    assert target["tts_options"]["speed_level"] == "slow"
    assert target["tts_options"]["volume_ratio"] == 1.0
    segment = temp_db.get_segments("task-query")[0]
    assert segment["audio_voice_type"] is None
    assert segment["audio_status"] == "failed"
    assert segment["audio_error"] == "previous failure"


def test_workspace_mutations_are_rejected_while_generation_is_running(
    temp_db, monkeypatch
):
    temp_db.create_task(
        "running-task",
        "文案",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    temp_db.update_task_workflow(
        "running-task", "planning", status="processing", current_step="text_generation"
    )
    monkeypatch.setattr(routes, "mysql_client", temp_db)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes.update_task_workspace_settings(
            "running-task",
            {"voice_type": "mimo:冰糖", "voice_confirmed": True},
        ))

    assert exc_info.value.status_code == 409
    assert "正在生成" in exc_info.value.detail

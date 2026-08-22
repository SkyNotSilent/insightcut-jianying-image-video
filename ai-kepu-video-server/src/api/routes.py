"""
FastAPI 路由
定义 API 端点
"""

import asyncio
import io
import json
import logging
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from threading import Thread, Lock
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Query, Body, UploadFile, File, Form, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from .models import (
    CreateTaskRequest,
    CreateTaskResponse,
    RegenerateAudioRequest,
    TaskResponse,
)
from .task_manager import task_manager, TaskStatus
from .task_executor import task_executor
from .task_runtime import task_runtime
from .error_model import (
    ErrorCode,
    SafeError,
    classify_exception,
    make_safe_error,
    normalize_error_metadata,
)
from src.config import Config
from src.database import mysql_client
from src.export.asset_package import (
    build_material_package,
    current_material_package,
    material_package_state,
)
from src.draft.voice_catalog import (
    build_voice_key,
    normalize_tts_options,
    parse_voice_key,
)
from src.draft.voice_clone import VoiceCloneStore
from src.draft.voice_preview import PRESET_VOICE_PREVIEW_TEXT, VoicePreviewService
from src.draft.voiceover import VoiceOverGenerator
from src.text.provider_catalog import (
    get_provider,
    list_llm_providers,
    list_provider_models,
)
from src.text.provider_models import ProviderModelSyncError, refresh_provider_models
from src.text.segmenter import TextSegmenter
from src.utils.path_fixer import (
    apply_content_info,
    apply_extract_path,
    apply_meta_info,
    generate_fix_bat,
    generate_fix_sh,
    normalize_extract_path,
    validate_extract_path,
)
from src.utils.rendering import canvas_for_ratio, normalize_ratio
from src.utils.subtitle_text import normalize_subtitle_text

router = APIRouter(prefix="/ai/native/video/kepu", tags=["tasks"])
logger = logging.getLogger(__name__)

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_IMAGE_COUNT = 20
EXPORT_JOBS = {}
EXPORT_JOBS_LOCK = Lock()
ALLOWED_DOCUMENT_EXTENSIONS = {".txt", ".md", ".markdown", ".docx", ".pdf"}
MAX_DOCUMENT_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_VOICE_REFERENCE_UPLOAD_BYTES = 20 * 1024 * 1024


class ExportJobCancelled(RuntimeError):
    """Internal control flow used to finish a cancelled export safely."""


def _voice_clone_store() -> VoiceCloneStore:
    return VoiceCloneStore(Config.BASE_DIR, mysql_client)


def _model_dict(model, exclude_none: bool = False) -> dict:
    if model is None:
        return {}
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=exclude_none)
    return model.dict(exclude_none=exclude_none)


def _public_error(
    error: Optional[str],
    error_code: Optional[str],
    error_meta: Optional[dict],
) -> tuple:
    """Return only normalized, credential-safe error fields to clients."""

    if not (error or error_code or error_meta):
        return None, None, None
    code = error_code or ErrorCode.UNKNOWN.value
    if isinstance(code, ErrorCode):
        code = code.value
    meta = normalize_error_metadata(code, error_meta)
    return meta["safe_message"], str(code), meta


def _snapshot_tts_options(voice_type: str, options: Optional[dict] = None) -> dict:
    config = Config.tts_config()
    selection = parse_voice_key(voice_type, default_provider=config.get("provider"))
    provider_config = config if selection.provider == "doubao" else config.get("mimo") or {}
    normalized = normalize_tts_options(options, provider_config, selection.provider)
    snapshot = {"speed_level": normalized["speed_level"]}
    if selection.provider == "doubao":
        snapshot["volume_ratio"] = normalized["volume_ratio"]
    else:
        snapshot["style_prompt"] = normalized["style_prompt"]
    return snapshot


def _resolve_new_task_voice(voice_type: Optional[str]) -> str:
    config = Config.tts_config()
    default_provider = (config.get("provider") or "doubao").lower()
    if voice_type:
        selection = parse_voice_key(voice_type, default_provider=default_provider)
    elif default_provider == "mimo":
        default_voice = (config.get("mimo") or {}).get("default_voice") or "冰糖"
        selection = parse_voice_key(
            default_voice if str(default_voice).startswith("mimo-clone:")
            else build_voice_key("mimo", default_voice)
        )
    else:
        selection = parse_voice_key(
            build_voice_key(
                "doubao",
                config.get("default_voice") or "zh_male_jieshuoxiaoming_moon_bigtts",
            )
        )

    enabled_providers = config.get("enabled_providers") or ["doubao", "mimo"]
    if selection.provider not in enabled_providers:
        raise HTTPException(status_code=400, detail=f"{selection.provider} TTS 当前未启用")
    if selection.kind == "clone":
        clone = _voice_clone_store().get(selection.voice_id)
        if not clone:
            raise HTTPException(status_code=400, detail="克隆音色不存在")
        if clone.get("status") != "ready" or not clone.get("is_enabled"):
            raise HTTPException(status_code=400, detail="克隆音色需先试听成功并启用")
        return selection.key

    record = mysql_client.find_tts_voice(selection.provider, selection.voice_id)
    if record:
        return selection.key
    if voice_type:
        raise HTTPException(status_code=400, detail="音色不存在")

    fallback = mysql_client.list_tts_voices(provider=selection.provider, include_disabled=True)
    if not fallback:
        fallback = mysql_client.list_tts_voices(include_disabled=True)
    if not fallback:
        raise HTTPException(status_code=400, detail="当前没有可用音色")
    return fallback[0]["id"]


def _parse_options_json(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_subtitle_options(value: Optional[dict]) -> dict:
    value = value or {}
    return {
        "size": value.get("size") if value.get("size") in {"small", "standard", "large"} else "standard",
        "position": value.get("position") if value.get("position") in {"low", "standard", "high"} else "standard",
        "outline": value.get("outline") if value.get("outline") in {"light", "standard", "strong"} else "standard",
    }


def _normalize_generation_options(value: Optional[dict]) -> dict:
    value = value or {}
    return {
        "prompt_concurrency": max(1, min(8, int(value.get("prompt_concurrency") or 4))),
        "image_concurrency": max(1, min(8, int(value.get("image_concurrency") or 8))),
        "retry_count": max(0, min(5, int(value.get("retry_count") if value.get("retry_count") is not None else 2))),
        "retry_interval_seconds": max(1, min(60, int(value.get("retry_interval_seconds") or 5))),
    }


def _task_subtitle_options(task_row: Optional[dict]) -> dict:
    return _normalize_subtitle_options(
        _parse_options_json((task_row or {}).get("subtitle_options_json"))
    )


def _safe_draft_name(name: str, task_id: str) -> str:
    """生成可用的本地草稿目录名。"""
    base = (name or "本地上传图片").strip() or "本地上传图片"
    invalid_chars = set('<>:"/\\|?*\n\r\t')
    safe = "".join("_" if ch in invalid_chars else ch for ch in base).strip("._ ")
    safe = safe[:20] or "本地上传图片"
    return f"{safe}_{task_id[:8]}"


def _validate_upload_image(file: UploadFile):
    content_type = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES and suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只支持 JPG、PNG、WEBP 格式的图片")


def _decode_plain_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _normalize_document_text(text: str) -> str:
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    normalized = "\n".join(lines)
    while "\n\n\n" in normalized:
        normalized = normalized.replace("\n\n\n", "\n\n")
    return normalized.strip()


def _extract_docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            document_xml = archive.read("word/document.xml")
    except KeyError:
        raise HTTPException(status_code=400, detail="DOCX 文件缺少正文内容")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="DOCX 文件格式无效")

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError:
        raise HTTPException(status_code=400, detail="DOCX 正文解析失败")

    paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        parts = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                parts.append(node.text)
            elif tag == "tab":
                parts.append("\t")
            elif tag in {"br", "cr"}:
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def _extract_pdf_text(content: bytes) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise HTTPException(status_code=501, detail="当前环境缺少 pdftotext，暂无法解析 PDF")

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name
        result = subprocess.run(
            [pdftotext, "-layout", "-enc", "UTF-8", temp_path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="PDF 解析超时，请尝试更小的文件")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="ignore").strip()
        raise HTTPException(status_code=400, detail=f"PDF 解析失败：{error or '文件格式无效'}")
    return result.stdout.decode("utf-8", errors="ignore")


def _extract_uploaded_document_text(filename: str, content: bytes) -> tuple[str, str]:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只支持 TXT、Markdown、DOCX、PDF 文档")
    if suffix in {".txt", ".md", ".markdown"}:
        return _decode_plain_text(content), "text"
    if suffix == ".docx":
        return _extract_docx_text(content), "docx"
    if suffix == ".pdf":
        return _extract_pdf_text(content), "pdf"
    raise HTTPException(status_code=400, detail="不支持的文档格式")


def _task_animation_seed(task_id: str) -> int:
    return int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12], 16)


def _segment_duration_seconds(segment: dict) -> float:
    try:
        duration = segment.get("duration")
        if duration:
            return float(duration)
    except (TypeError, ValueError):
        pass
    return 4.0


def _normalize_local_media_url(url: Optional[str], request: Request) -> Optional[str]:
    if not url or "/media/" not in url:
        return url
    media_path = url.split("/media/", 1)[1]
    return str(request.url_for("media", file_path=media_path))


def _local_media_url_from_path(path: Optional[str], request: Request) -> Optional[str]:
    resolved_file = _workspace_resolve_file(path)
    if resolved_file is None:
        return None
    try:
        resolved = resolved_file.resolve()
    except (OSError, RuntimeError):
        return None

    for root in (Config.BASE_DIR / "output", Config.BASE_DIR / "data" / "media"):
        try:
            root_resolved = root.resolve()
            rel = resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        return str(request.url_for("media", file_path=str(rel).replace(os.sep, "/")))
    return None


def _task_ratio(task) -> str:
    ratio = getattr(task, "ratio", "16:9")
    db_task = mysql_client.get_task(getattr(task, "task_id", ""))
    if db_task and db_task.get("ratio"):
        ratio = db_task.get("ratio")
    return normalize_ratio(ratio)


def _task_canvas(task) -> dict:
    return canvas_for_ratio(_task_ratio(task))


def _ratio_slug(ratio: str) -> str:
    return normalize_ratio(ratio).replace(":", "x")


def _file_signature(raw_path: Optional[str]) -> dict:
    if not raw_path:
        return {"path": "", "exists": False}
    path = Path(raw_path)
    try:
        stat = path.stat()
        return {
            "path": str(path),
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        return {"path": str(path), "exists": False}


def _media_fingerprint(task, segments: List[dict]) -> str:
    from src.export.ffmpeg_exporter import FFmpegExporter

    task_row = mysql_client.get_task(getattr(task, "task_id", "")) or {}
    payload = {
        "ratio": _task_ratio(task),
        "plan_version": int(task_row.get("plan_version") or 0),
        "style": task_row.get("style") or getattr(task, "style", "") or "",
        "voice_type": task_row.get("voice_type") or getattr(task, "voice_type", "") or "",
        "tts_options_json": task_row.get("tts_options_json") or "",
        "subtitle_options_json": task_row.get("subtitle_options_json") or "",
        "canvas": _task_canvas(task),
        "render_config": _render_config_for_task(FFmpegExporter, task, task_row),
        "settings": _file_signature(str(Config.BASE_DIR / "config" / "settings.json")),
        "animation_seed": _task_animation_seed(task.task_id),
        "segments": [
            {
                "text": seg.get("text") or "",
                "segment_index": seg.get("segment_index"),
                "image_prompt": seg.get("image_prompt") or "",
                "prompt_status": seg.get("prompt_status") or "",
                "image_status": seg.get("image_status") or "",
                "audio_status": seg.get("audio_status") or "",
                "image": _file_signature(seg.get("image_path")),
                "audio": _file_signature(seg.get("audio_path")),
                "audio_voice_type": seg.get("audio_voice_type"),
                "audio_tts_options_json": seg.get("audio_tts_options_json"),
                "selected_image_asset_id": seg.get("selected_image_asset_id"),
                "selected_audio_asset_id": seg.get("selected_audio_asset_id"),
                "duration": seg.get("duration"),
            }
            for seg in segments
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _render_config_for_task(exporter_class, task, task_row: dict) -> dict:
    """Read the task render snapshot while keeping simple test exporters compatible."""
    try:
        return exporter_class.get_render_config(
            canvas=_task_canvas(task),
            subtitle_options=_task_subtitle_options(task_row),
        )
    except TypeError:
        return exporter_class.get_render_config(canvas=_task_canvas(task))


def _plan_fingerprint(task_row: dict, segments: List[dict]) -> str:
    """Fingerprint only the editable production plan, not generated file metadata."""
    payload = {
        "task_id": task_row.get("task_id"),
        "plan_version": int(task_row.get("plan_version") or 0),
        "style": task_row.get("style") or "",
        "ratio": normalize_ratio(task_row.get("ratio") or "16:9"),
        "voice_type": task_row.get("voice_type") or "",
        "tts_options_json": task_row.get("tts_options_json") or "",
        "subtitle_options_json": task_row.get("subtitle_options_json") or "",
        "generation_options_json": task_row.get("generation_options_json") or "",
        "template_id": task_row.get("template_id") or "",
        "segments": [
            {
                "segment_index": segment.get("segment_index"),
                "text": segment.get("text") or "",
                "image_prompt": segment.get("image_prompt") or "",
                "audio_voice_type": segment.get("audio_voice_type") or "",
                "selected_image_asset_id": segment.get("selected_image_asset_id") or "",
                "selected_audio_asset_id": segment.get("selected_audio_asset_id") or "",
            }
            for segment in segments
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _task_draft_path(task) -> Path:
    raw_path = getattr(getattr(task, "result", None), "draft_path", None)
    resolved = _workspace_resolve_directory(raw_path)
    if resolved is not None:
        return resolved
    candidates = _workspace_storage_candidates(raw_path)
    return candidates[0] if candidates else Path(raw_path or "")


def _preview_manifest_path(task) -> Path:
    return _task_draft_path(task) / "previews" / "manifest_full.json"


def _read_preview_manifest(task) -> Optional[dict]:
    if not task.result or not task.result.draft_path:
        return None
    path = _preview_manifest_path(task)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        safe = classify_exception(e, provider="local_storage")
        logger.warning(
            "[%s] 读取最终预览 manifest 失败: %s",
            task.task_id,
            safe.safe_message,
        )
        return None


def _preview_state(task, segments: List[dict]) -> dict:
    ratio = _task_ratio(task)
    fingerprint = _media_fingerprint(task, segments)
    manifest = _read_preview_manifest(task)
    valid = False
    reason = "missing"
    if manifest:
        video_path = _workspace_resolve_file(manifest.get("video_path"))
        if manifest.get("fingerprint") != fingerprint:
            reason = "stale"
        elif normalize_ratio(manifest.get("ratio")) != ratio:
            reason = "ratio_mismatch"
        elif video_path is None:
            reason = "file_missing"
        else:
            valid = True
            reason = "valid"
    return {
        "exists": bool(manifest),
        "valid": valid,
        "reason": reason,
        "fingerprint": fingerprint,
        "manifest": manifest,
    }


def _write_preview_manifest(task, video_path: Path, preview_url: str, segments: List[dict]) -> dict:
    fingerprint = _media_fingerprint(task, segments)
    manifest = {
        "video_path": str(video_path),
        "preview_url": preview_url,
        "video_url": preview_url,
        "ratio": _task_ratio(task),
        "canvas": _task_canvas(task),
        "fingerprint": fingerprint,
        "snapshot_key": fingerprint,
        "created_at": datetime.now().isoformat(),
    }
    path = _preview_manifest_path(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _official_video_path(task) -> Path:
    draft_path = _task_draft_path(task)
    return draft_path / f"{draft_path.name}.mp4"


def _ensure_legacy_render_manifest(task, segments: List[dict]) -> None:
    """Adopt an old task's existing MP4 once so future edits can invalidate it."""
    if not task.result or not task.result.draft_path or _read_preview_manifest(task):
        return
    video_path = _official_video_path(task)
    if video_path.is_file() and task.result.video_url:
        _write_preview_manifest(task, video_path, task.result.video_url, segments)


def _draft_zip_path(task) -> Path:
    draft_path = _task_draft_path(task)
    return draft_path / f"{draft_path.name}.zip"


def _pack_draft_zip(task) -> Path:
    draft_path = Path(task.result.draft_path)
    zip_path = _draft_zip_path(task)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in draft_path.rglob("*"):
            if not file_path.is_file():
                continue
            rel_path = file_path.relative_to(draft_path)
            if rel_path.parts and rel_path.parts[0] in {"previews"}:
                continue
            if file_path.suffix.lower() in {".zip", ".mp4"}:
                continue
            zf.write(file_path, rel_path)
    return zip_path


def _server_target_os() -> str:
    if sys_platform := platform.system().lower():
        if "darwin" in sys_platform:
            return "mac"
        if "windows" in sys_platform:
            return "windows"
    return "mac" if os.name != "nt" else "windows"


def _pick_local_folder() -> Optional[str]:
    """在运行后端的本机弹出系统目录选择器。用户取消时返回 None。"""
    if _server_target_os() == "mac":
        script = 'POSIX path of (choose folder with prompt "选择剪映草稿根目录 com.lveditor.draft")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip().rstrip("/")

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="选择剪映草稿根目录")
        root.destroy()
        return folder or None
    except Exception as e:
        safe = classify_exception(e, provider="local_storage")
        logger.warning("系统目录选择器不可用: %s", safe.safe_message)
        return None


def _check_writable_dir(path: Path) -> tuple[bool, Optional[str]]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".kepu_write_test_{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, None
    except Exception as e:
        return False, str(e)


def _validate_local_draft_root(draft_root: str, target_os: Optional[str] = None) -> dict:
    target_os = "mac" if target_os == "mac" else "windows" if target_os == "windows" else _server_target_os()
    valid_path, normalized, issues = validate_extract_path(draft_root, target_os)
    warnings = []

    if target_os != _server_target_os():
        issues.append("直接写入只能选择运行后端这台电脑上的剪映草稿目录")

    root_path = Path(normalized) if normalized else None
    if root_path:
        if root_path.exists() and not root_path.is_dir():
            issues.append("选择的路径不是文件夹")
        else:
            if any((root_path / name).exists() for name in ("draft_info.json", "draft_content.json", "draft_meta_info.json")):
                issues.append("你选择的是单个草稿目录，请选择它的上一级剪映草稿根目录")
            writable, error = _check_writable_dir(root_path)
            if not writable:
                issues.append(f"目录不可写：{error}")

        if root_path.name not in {"com.lveditor.draft", "JianyingPro Drafts", "Drafts"}:
            warnings.append("这个目录名不像剪映草稿根目录，请确认选的是剪映的草稿列表目录")

    blocking = any(
        "必须" in item
        or "请填写" in item
        or "不可写" in item
        or "不是文件夹" in item
        or "只能选择" in item
        or "单个草稿目录" in item
        for item in issues
    )
    return {
        "valid": valid_path and not blocking,
        "path": normalized,
        "target_os": target_os,
        "issues": issues,
        "warnings": warnings,
    }


def _normalize_draft_files_for_location(draft_dir: Path, draft_root: str, draft_name: str, target_os: str) -> None:
    if target_os == "mac":
        _normalize_mac_draft_files(draft_dir, draft_root, draft_name)
        return

    now_us = int(time.time() * 1_000_000)
    content_path = draft_dir / "draft_content.json"
    meta_path = draft_dir / "draft_meta_info.json"

    if content_path.exists():
        content = json.loads(content_path.read_text(encoding="utf-8"))
        content = apply_extract_path(content, draft_root, draft_name, target_os=target_os, force=True)
        content = apply_content_info(content, draft_name, target_os=target_os, now_us=now_us)
        content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")

    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = apply_meta_info(meta, draft_root, draft_name, target_os=target_os, now_us=now_us)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _infer_ratio_from_content(content: dict) -> str:
    canvas = content.get("canvas_config") or {}
    ratio = canvas.get("ratio")
    if ratio in {"16:9", "9:16", "3:4"}:
        return ratio
    width = canvas.get("width") or 1920
    height = canvas.get("height") or 1080

    # 计算宽高比
    ratio_value = width / height if height > 0 else 1.0

    # 判断最接近的比例
    if abs(ratio_value - 0.75) < 0.1:  # 3:4 (0.75)
        return "3:4"
    elif abs(ratio_value - 1.0) < 0.1:  # 接近方形，归为3:4
        return "3:4"
    elif ratio_value > 1.5:  # 16:9 (1.778)
        return "16:9"
    else:  # 9:16 (0.5625)
        return "9:16"


def _resolve_material_source(draft_dir: Path, raw_path: str) -> Path:
    clean = str(raw_path or "").strip().strip("'\"").replace("\\", "/")
    if "##/" in clean:
        clean = clean.split("##/", 1)[1]
    if clean.startswith("##_draftpath_placeholder_") and "_##/" in clean:
        clean = clean.split("_##/", 1)[1]
    path = Path(clean)
    if path.is_absolute():
        return path
    return draft_dir / clean


def _mac_material_folder(path: Path, default_folder: str) -> str:
    if default_folder == "audio":
        return "audio"
    if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
        return "video"
    return "image"


def _media_suffix_from_magic(path: Path) -> Optional[str]:
    """Return the real image/video suffix when the file header is unambiguous."""
    try:
        header = path.read_bytes()[:32]
    except Exception:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _mac_target_filename(source: Path, raw_path: str, folder: str) -> str:
    filename = source.name or Path(str(raw_path or "")).name
    if folder == "image" and source.exists():
        real_suffix = _media_suffix_from_magic(source)
        if real_suffix:
            filename = f"{source.stem}{real_suffix}"
    return filename


def _copy_mac_material(draft_dir: Path, raw_path: str, default_folder: str) -> str:
    source = _resolve_material_source(draft_dir, raw_path)
    folder = _mac_material_folder(source, default_folder)
    filename = _mac_target_filename(source, raw_path, folder)
    target_dir = draft_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if source.exists() and source.resolve() != target.resolve():
        shutil.copy2(source, target)
    # Mac 剪映会在首次打开后迁移/加密 draft_info。这里写入本机绝对路径，
    # 避免占位符路径在迁移时无法映射到我们新建的草稿目录。
    return str(target)


def _material_ids(content: dict) -> set:
    ids = set()
    for values in content.get("materials", {}).values():
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and item.get("id"):
                    ids.add(item["id"])
    return ids


def _remove_dangling_extra_refs(content: dict) -> int:
    ids = _material_ids(content)
    removed = 0
    for track in content.get("tracks", []):
        for segment in track.get("segments", []):
            refs = segment.get("extra_material_refs")
            if not isinstance(refs, list):
                continue
            clean_refs = [ref for ref in refs if ref in ids]
            removed += len(refs) - len(clean_refs)
            segment["extra_material_refs"] = clean_refs
    return removed


def _normalize_mac_draft_files(draft_dir: Path, draft_root: str, draft_name: str) -> None:
    """把 pyJianYingDraft 的 Windows 风格草稿转成 Mac 剪映目录形态。"""
    now_us = int(time.time() * 1_000_000)
    content_path = draft_dir / "draft_content.json"
    info_path = draft_dir / "draft_info.json"
    meta_path = draft_dir / "draft_meta_info.json"
    if not content_path.exists() and info_path.exists():
        content_path = info_path
    if not content_path.exists():
        raise RuntimeError("缺少 draft_content.json，无法生成 Mac 剪映草稿")

    content = json.loads(content_path.read_text(encoding="utf-8"))
    first_image_path = None
    for video in content.get("materials", {}).get("videos", []):
        raw_path = video.get("path") or video.get("media_path") or ""
        mac_path = _copy_mac_material(draft_dir, raw_path, "image")
        video["path"] = mac_path
        video["media_path"] = ""
        video["material_name"] = Path(mac_path).name
        if Path(mac_path).parent.name == "image" and first_image_path is None:
            first_image_path = Path(mac_path)

    for audio in content.get("materials", {}).get("audios", []):
        raw_path = audio.get("path") or ""
        mac_path = _copy_mac_material(draft_dir, raw_path, "audio")
        audio["path"] = mac_path
        audio["name"] = audio.get("name") or Path(mac_path).name

    removed_refs = _remove_dangling_extra_refs(content)
    if removed_refs:
        logger.warning("Mac 剪映草稿清理了 %s 个悬空素材引用：%s", removed_refs, draft_dir)

    ratio = _infer_ratio_from_content(content)
    content = apply_content_info(content, draft_name, target_os="mac", now_us=now_us)
    content["platform"] = {
        "os": "web",
        "os_version": "",
        "app_version": "15.4.0",
        "app_source": "",
        "device_id": "",
        "hard_disk_id": "",
        "mac_address": "",
        "app_id": 348188,
    }
    content["last_modified_platform"] = dict(content["platform"])
    content["version"] = max(int(content.get("version") or 0), 400000)
    content["new_version"] = "127.0.0"
    canvas = content.setdefault("canvas_config", {})
    canvas["ratio"] = ratio
    canvas.setdefault("width", _task_canvas_from_ratio(ratio)["width"])
    canvas.setdefault("height", _task_canvas_from_ratio(ratio)["height"])
    content["path"] = ""

    info_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    if content_path.name == "draft_content.json":
        content_path.unlink(missing_ok=True)

    for folder in ("common_attachment", "cover", "effect"):
        (draft_dir / folder).mkdir(exist_ok=True)
    if first_image_path and first_image_path.exists():
        shutil.copy2(first_image_path, draft_dir / "draft_cover.jpg")

    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {}
    meta = apply_meta_info(meta, draft_root, draft_name, target_os="mac", now_us=now_us)
    meta["draft_cover"] = "draft_cover.jpg" if (draft_dir / "draft_cover.jpg").exists() else meta.get("draft_cover", "")
    meta["draft_materials"] = []
    meta["draft_timeline_materials_size_"] = sum(
        file_path.stat().st_size for folder in ("image", "audio", "video")
        for file_path in (draft_dir / folder).glob("*") if file_path.is_file()
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    for legacy in ("images", "voiceovers", "audios"):
        legacy_path = draft_dir / legacy
        if legacy_path.exists():
            shutil.rmtree(legacy_path)


def _task_canvas_from_ratio(ratio: str) -> dict:
    return canvas_for_ratio(normalize_ratio(ratio))


def _draft_preflight(draft_dir: Path, target_os: Optional[str] = None) -> dict:
    issues = []
    warnings = []
    is_mac = target_os == "mac"
    content_path = draft_dir / "draft_info.json" if is_mac else draft_dir / "draft_content.json"
    meta_path = draft_dir / "draft_meta_info.json"

    if not content_path.exists():
        issues.append("缺少 draft_info.json" if is_mac else "缺少 draft_content.json")
    if not meta_path.exists():
        issues.append("缺少 draft_meta_info.json")

    if content_path.exists():
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
            if not content.get("name"):
                warnings.append("draft_content.json 缺少草稿名称")
            if not content.get("tracks"):
                issues.append("draft_content.json 没有轨道数据")
            material_ids = _material_ids(content)
            materials = content.get("materials", {})
            for group in ("videos", "audios"):
                for item in materials.get(group, []):
                    path = str(item.get("path") or "").strip().strip("'\"")
                    if not path:
                        issues.append(f"{group} 存在空素材路径")
                        continue
                    clean_path = path.replace("\\", "/")
                    if is_mac and clean_path.startswith("##_draftpath_placeholder_") and "_##/" in clean_path:
                        clean_path = clean_path.split("_##/", 1)[1]
                    local_path = Path(clean_path) if Path(clean_path).is_absolute() else draft_dir / clean_path
                    if not local_path.exists():
                        issues.append(f"素材不存在：{path}")
                    if is_mac and group == "videos":
                        real_suffix = _media_suffix_from_magic(local_path)
                        if real_suffix and local_path.suffix.lower() != real_suffix:
                            issues.append(f"图片扩展名和真实格式不一致：{path} 应为 {real_suffix}")
            dangling_refs = []
            for track in content.get("tracks", []):
                for segment in track.get("segments", []):
                    refs = segment.get("extra_material_refs")
                    if isinstance(refs, list):
                        dangling_refs.extend(ref for ref in refs if ref not in material_ids)
            if dangling_refs:
                issues.append(f"存在 {len(dangling_refs)} 个悬空素材引用")
        except Exception as e:
            issues.append(f"{content_path.name} 解析失败：{e}")

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not meta.get("draft_name"):
                warnings.append("draft_meta_info.json 缺少草稿名称")
            if not meta.get("draft_fold_path"):
                warnings.append("draft_meta_info.json 缺少草稿目录路径")
        except Exception as e:
            issues.append(f"draft_meta_info.json 解析失败：{e}")

    return {
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
        "draft_path": str(draft_dir),
        "target_os": target_os or _server_target_os(),
    }


def _set_task_result_preserving(task, segments_count: int, draft_url: Optional[str] = None, video_url: Optional[str] = None):
    existing_draft_url = task.result.draft_url if task.result else None
    existing_video_url = task.result.video_url if task.result else None
    task_manager.set_task_result(
        task.task_id,
        task.result.draft_path,
        segments_count,
        draft_url=draft_url if draft_url is not None else existing_draft_url,
        video_url=video_url if video_url is not None else existing_video_url,
    )


def _asset_label(asset_type: str, source: str, segment_index: Optional[int], path: Optional[str] = None) -> str:
    source_map = {
        "generated": "AI 生成",
        "regenerated": "重新生成",
        "upload": "本地上传",
        "selected": "历史选择",
        "legacy": "历史素材",
        "subtitle": "字幕素材",
    }
    prefix = source_map.get(source, source or "素材")
    if segment_index is not None:
        return f"{prefix} · 分镜 {segment_index + 1}"
    if path:
        return Path(path).name
    return prefix


def _record_asset(
    task_id: str,
    asset_type: str,
    source: str,
    path: Optional[str] = None,
    url: Optional[str] = None,
    segment_index: Optional[int] = None,
    label: Optional[str] = None,
    prompt: Optional[str] = None,
    text: Optional[str] = None,
    voice_type: Optional[str] = None,
    metadata: Optional[dict] = None,
    operation_id: Optional[str] = None,
    origin_asset_id: Optional[str] = None,
) -> dict:
    snapshot = {
        "prompt": prompt,
        "text": text,
        "voice_type": voice_type,
        "metadata": metadata or {},
    }
    return mysql_client.save_task_asset(
        task_id=task_id,
        asset_type=asset_type,
        source=source,
        path=path,
        url=url,
        segment_index=segment_index,
        label=label or _asset_label(asset_type, source, segment_index, path),
        prompt=prompt,
        text=text,
        voice_type=voice_type,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        operation_id=operation_id,
        origin_asset_id=origin_asset_id,
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    )


def _format_srt_timestamp(seconds: float) -> str:
    millis = int(max(0, seconds) * 1000)
    hours = millis // 3_600_000
    millis %= 3_600_000
    minutes = millis // 60_000
    millis %= 60_000
    secs = millis // 1000
    millis %= 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _subtitle_srt_path(task) -> Path:
    return Path(task.result.draft_path) / "subtitles" / f"{Path(task.result.draft_path).name}.srt"


def _write_task_srt(task, segments: List[dict]) -> Path:
    srt_path = _subtitle_srt_path(task)
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    cursor = 0.0
    blocks = []
    for index, seg in enumerate(segments, start=1):
        duration = _segment_duration_seconds(seg)
        start = cursor
        end = cursor + duration
        text = normalize_subtitle_text(seg.get("text") or "")
        blocks.append(f"{index}\n{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n{text}\n")
        cursor = end
    srt_path.write_text("\n".join(blocks), encoding="utf-8")
    return srt_path


def _ensure_task_assets(task, segments: List[dict]):
    """兼容旧任务：从段落表和草稿目录尽量补齐资产记录。"""
    draft_path = (
        Path(task.result.draft_path)
        if task.result and task.result.draft_path
        else None
    )
    current_paths = set()

    for seg in segments:
        index = seg.get("segment_index")
        image_path = seg.get("image_path")
        if image_path:
            current_paths.add(str(image_path))
            _record_asset(
                task.task_id,
                "image",
                "legacy",
                path=str(image_path),
                url=seg.get("image_url"),
                segment_index=index,
                prompt=seg.get("image_prompt"),
                text=seg.get("text"),
            )
        audio_path = seg.get("audio_path")
        if audio_path:
            current_paths.add(str(audio_path))
            _record_asset(
                task.task_id,
                "audio",
                "legacy",
                path=str(audio_path),
                url=seg.get("audio_url"),
                segment_index=index,
                text=seg.get("text"),
                voice_type=getattr(task, "voice_type", None),
            )

    for dirname, asset_type in (("images", "image"), ("voiceovers", "audio")):
        if draft_path is None:
            continue
        folder = draft_path / dirname
        if not folder.exists():
            continue
        for file_path in folder.iterdir():
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if asset_type == "image" and suffix not in ALLOWED_IMAGE_EXTENSIONS:
                continue
            if asset_type == "audio" and suffix not in {".wav", ".mp3", ".m4a", ".aac"}:
                continue
            source = "upload" if "_upload" in file_path.name else ("regenerated" if "_regen" in file_path.name else "legacy")
            segment_index = None
            if file_path.name.startswith("seg_"):
                try:
                    segment_index = int(file_path.name.split("_", 2)[1])
                except (ValueError, IndexError):
                    segment_index = None
            _record_asset(
                task.task_id,
                asset_type,
                source,
                path=str(file_path),
                segment_index=segment_index,
                voice_type=getattr(task, "voice_type", None) if asset_type == "audio" else None,
            )

    if segments and task.result and task.result.draft_path:
        # Workspace reads must not create files.  Record an SRT only after an
        # explicit subtitle/export action has already materialized it.
        srt_path = _subtitle_srt_path(task)
        if srt_path.exists():
            _record_asset(
                task.task_id,
                "subtitle",
                "subtitle",
                path=str(srt_path),
                label="项目字幕 SRT",
                text="\n".join(normalize_subtitle_text(seg.get("text") or "") for seg in segments),
            )
    mysql_client.backfill_selected_asset_ids(task.task_id)


def _asset_to_response(asset: dict, request: Request) -> dict:
    asset_id = asset.get("asset_id")
    path = asset.get("path")
    has_file = _workspace_resolve_file(path) is not None
    file_url = str(request.url_for("download_task_asset_file", task_id=asset["task_id"], asset_id=asset_id)) if has_file else None
    return {
        "asset_id": asset_id,
        "task_id": asset.get("task_id"),
        "segment_index": asset.get("segment_index"),
        "asset_type": asset.get("asset_type"),
        "source": asset.get("source"),
        "path": path,
        "url": _normalize_local_media_url(asset.get("url"), request),
        "file_url": file_url,
        "download_url": file_url,
        "has_file": has_file,
        "label": asset.get("label") or _asset_label(asset.get("asset_type"), asset.get("source"), asset.get("segment_index"), path),
        "prompt": asset.get("prompt"),
        "text": asset.get("text"),
        "voice_type": asset.get("voice_type"),
        "operation_id": asset.get("operation_id"),
        "origin_asset_id": asset.get("origin_asset_id"),
        "snapshot": _parse_options_json(asset.get("snapshot_json")),
        "created_at": asset.get("created_at"),
    }


def _export_job_snapshot(job_id: str) -> dict:
    with EXPORT_JOBS_LOCK:
        return dict(EXPORT_JOBS.get(job_id, {}))


def _update_export_job(job_id: str, **updates):
    with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = datetime.now().isoformat()


def _safe_export_params(payload: Optional[dict]) -> dict:
    """Persist only operational export fields, never the caller's full body."""

    source = payload if isinstance(payload, dict) else {}
    return {
        key: source[key]
        for key in ("draft_root", "target_os", "use_preview", "auto_download", "overwrite")
        if key in source and isinstance(source[key], (str, bool))
    }


def _create_export_job(task_id: str, target: str, payload: Optional[dict] = None) -> dict:
    job_id = uuid.uuid4().hex
    now = datetime.now().isoformat()
    job = {
        "job_id": job_id,
        "task_id": task_id,
        "target": target,
        "status": "pending",
        "message": "等待开始",
        "result": None,
        "error": None,
        "error_code": None,
        "error_meta": None,
        "cancel_requested": False,
        "params": _safe_export_params(payload),
        "created_at": now,
        "updated_at": now,
    }
    with EXPORT_JOBS_LOCK:
        EXPORT_JOBS[job_id] = job
    return job


def _create_or_reuse_export_job(task_id: str, target: str, payload: Optional[dict] = None) -> tuple:
    """Create a job atomically, reusing an in-flight MP4 render for the same task."""
    with EXPORT_JOBS_LOCK:
        if target == "mp4":
            for existing in EXPORT_JOBS.values():
                if (
                    existing.get("task_id") == task_id
                    and existing.get("target") == "mp4"
                    and existing.get("status") in {"pending", "processing"}
                ):
                    return dict(existing), False

        job_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        job = {
            "job_id": job_id,
            "task_id": task_id,
            "target": target,
            "status": "pending",
            "message": "等待开始",
            "result": None,
            "error": None,
            "error_code": None,
            "error_meta": None,
            "cancel_requested": False,
            "params": _safe_export_params(payload),
            "created_at": now,
            "updated_at": now,
        }
        EXPORT_JOBS[job_id] = job
        return dict(job), True


def _active_export_jobs(task_id: str) -> List[dict]:
    with EXPORT_JOBS_LOCK:
        return [
            dict(job) for job in EXPORT_JOBS.values()
            if job.get("task_id") == task_id and job.get("status") in {"pending", "processing"}
        ]


def _latest_export_jobs(task_id: str) -> List[dict]:
    with EXPORT_JOBS_LOCK:
        matching = [dict(job) for job in EXPORT_JOBS.values() if job.get("task_id") == task_id]
    latest = {}
    for job in sorted(matching, key=lambda item: item.get("created_at") or ""):
        latest[job.get("target")] = job
    return list(latest.values())


def _export_cancel_requested(job_id: str) -> bool:
    with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def _raise_if_export_cancelled(job_id: str) -> None:
    if _export_cancel_requested(job_id):
        raise ExportJobCancelled("导出已取消")


def _run_export_job(job_id: str, target: str, use_preview: bool, payload: Optional[dict] = None):
    _update_export_job(job_id, status="processing", message="正在准备导出")
    job = _export_job_snapshot(job_id)
    task_id = job.get("task_id")
    try:
        _raise_if_export_cancelled(job_id)
        task = task_manager.get_task(task_id)
        if not task:
            raise RuntimeError("任务不存在")
        if target != "materials" and (not task.result or not task.result.draft_path):
            raise RuntimeError("草稿路径不存在")

        segments = mysql_client.get_segments(task_id)
        if not segments:
            raise RuntimeError("段落数据不存在")

        if target == "mp4":
            result = _export_mp4(
                task,
                segments,
                use_preview,
                should_cancel=lambda: _export_cancel_requested(job_id),
            )
        elif target == "draft":
            result = _export_draft(task, segments)
        elif target == "draft_local":
            result = _export_draft_local(task, segments, payload or {})
        elif target == "materials":
            result = build_material_package(
                task.task_id,
                getattr(task, "name", None) or getattr(task, "theme", None) or task.task_id,
                segments,
                Config.BASE_DIR,
            )
            result["download_url"] = (
                f"/ai/native/video/kepu/tasks/{task.task_id}/download-materials"
                f"?snapshot_key={quote(result['snapshot_key'])}"
            )
        else:
            raise RuntimeError("不支持的导出类型")

        _raise_if_export_cancelled(job_id)
        _update_export_job(job_id, status="completed", message="导出完成", result=result)
    except ExportJobCancelled:
        cancelled = make_safe_error(ErrorCode.CANCELLED, provider="export")
        _update_export_job(
            job_id,
            status="cancelled",
            message="已取消",
            error=None,
            error_code=cancelled.code.value,
            error_meta=cancelled.metadata(),
        )
    except Exception as e:
        safe = classify_exception(e, provider="export")
        logger.error("[%s] 导出任务失败: %s", task_id, safe.safe_message)
        _update_export_job(
            job_id,
            status="failed",
            message="导出失败",
            error=safe.safe_message,
            error_code=safe.code.value,
            error_meta=safe.metadata(),
        )


def _export_mp4(
    task,
    segments: List[dict],
    use_preview: bool,
    should_cancel=None,
) -> dict:
    from src.export.ffmpeg_exporter import FFmpegExporter, RenderCancelled
    from src.utils.local_uploader import LocalUploader

    output_path = _official_video_path(task)
    render_path: Optional[Path] = None
    snapshot_before = _media_fingerprint(task, segments)
    preview = _preview_state(task, segments)
    source = "rendered"

    def raise_if_cancelled() -> None:
        if should_cancel and should_cancel():
            raise ExportJobCancelled("导出已取消")

    raise_if_cancelled()

    if use_preview and preview["valid"]:
        manifest = preview["manifest"]
        preview_path = _workspace_resolve_file(manifest["video_path"])
        if preview_path is None:
            raise RuntimeError("完整视频缓存文件不存在")
        if preview_path.resolve() != output_path.resolve():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            render_path = output_path.with_name(
                f".{output_path.stem}.{uuid.uuid4().hex}.cached.mp4"
            )
            shutil.copy2(preview_path, render_path)
        source = "cached"
    else:
        segment_texts = [seg.get("text") or "" for seg in segments]
        resolved_images = [_workspace_resolve_file(seg.get("image_path")) for seg in segments]
        resolved_audio = [_workspace_resolve_file(seg.get("audio_path")) for seg in segments]
        if any(path is None for path in resolved_images):
            raise RuntimeError("分镜图片文件不存在，无法导出 MP4")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        render_path = output_path.with_name(
            f".{output_path.stem}.{uuid.uuid4().hex}.render.mp4"
        )
        try:
            FFmpegExporter(
                canvas=_task_canvas(task),
                subtitle_options=_task_subtitle_options(mysql_client.get_task(task.task_id)),
            ).export(
                segments=segment_texts,
                media_paths=[str(path) for path in resolved_images],
                voiceover_files=[str(path) if path else None for path in resolved_audio],
                output_path=str(render_path),
                animation_seed=_task_animation_seed(task.task_id),
                should_cancel=should_cancel,
            )
        except RenderCancelled as error:
            render_path.unlink(missing_ok=True)
            raise ExportJobCancelled("导出已取消") from error

    try:
        raise_if_cancelled()
        current_segments = mysql_client.get_segments(task.task_id)
        if _media_fingerprint(task, current_segments) != snapshot_before:
            raise RuntimeError("渲染期间素材已变化，本次结果已作废，请重新生成")
        # Rendering is staged beside the final file. The existing usable MP4 is
        # only replaced after the complete render and snapshot checks succeed.
        if render_path is not None:
            os.replace(render_path, output_path)
            render_path = None
    finally:
        if render_path is not None:
            render_path.unlink(missing_ok=True)

    video_url = LocalUploader().upload(
        str(output_path),
        f"{task.task_id}/exports/{output_path.stem}_{_ratio_slug(_task_ratio(task))}_{int(time.time())}.mp4",
    )
    manifest = _write_preview_manifest(task, output_path, video_url, segments)
    _set_task_result_preserving(task, len(segments), video_url=video_url)
    return {
        "target": "mp4",
        "source": source,
        "video_path": str(output_path),
        "video_url": video_url,
        "snapshot_key": manifest["snapshot_key"],
        "ratio": _task_ratio(task),
        "canvas": _task_canvas(task),
    }


def _build_editable_draft(task, segments: List[dict]) -> Path:
    from src.core.pipeline import VideoEditorPipeline

    segment_texts = [seg.get("text") or "" for seg in segments]
    media_paths = [seg.get("image_path") for seg in segments]
    voiceover_files = [seg.get("audio_path") for seg in segments]
    missing = [path for path in media_paths if not path or not Path(path).exists()]
    if missing:
        raise RuntimeError("分镜图片文件不存在，无法导出剪映草稿")

    draft_name = Path(task.result.draft_path).name
    pipeline = VideoEditorPipeline(theme=task.theme, output_dir=task.result.draft_path, canvas=_task_canvas(task))
    draft_path = pipeline.draft_builder.build(
        segments=segment_texts,
        media_paths=media_paths,
        draft_name=draft_name,
        voiceover_files=voiceover_files,
        animation_seed=_task_animation_seed(task.task_id),
        output_dir=task.result.draft_path,
    )
    return Path(draft_path)


def _export_draft(task, segments: List[dict]) -> dict:
    from src.utils.local_uploader import LocalUploader

    draft_path = _build_editable_draft(task, segments)
    zip_path = _pack_draft_zip(task)
    draft_url = LocalUploader().upload(
        str(zip_path),
        f"{task.task_id}/exports/{zip_path.stem}_{_ratio_slug(_task_ratio(task))}_{int(time.time())}.zip",
    )
    _set_task_result_preserving(task, len(segments), draft_url=draft_url)
    return {
        "target": "draft",
        "draft_path": draft_path,
        "zip_path": str(zip_path),
        "draft_url": draft_url,
        "ratio": _task_ratio(task),
        "canvas": _task_canvas(task),
    }


def _export_draft_local(task, segments: List[dict], payload: dict) -> dict:
    draft_root = (payload or {}).get("draft_root") or (payload or {}).get("extract_path")
    target_os = (payload or {}).get("target_os") or _server_target_os()
    overwrite = bool((payload or {}).get("overwrite", True))
    root_check = _validate_local_draft_root(draft_root, target_os)
    if not root_check["valid"]:
        raise RuntimeError("；".join(root_check["issues"] or ["剪映草稿目录不可用"]))

    source_draft = _build_editable_draft(task, segments)
    draft_name = source_draft.name
    draft_root_path = Path(root_check["path"])
    target_draft = draft_root_path / draft_name

    if target_draft.exists():
        if not overwrite:
            raise RuntimeError(f"剪映草稿已存在：{target_draft}")
        shutil.rmtree(target_draft)

    def _ignore_export_artifacts(_dir, names):
        return {
            name for name in names
            if name == "previews" or name.endswith(".zip") or name.endswith(".mp4")
        }

    shutil.copytree(source_draft, target_draft, ignore=_ignore_export_artifacts)
    _normalize_draft_files_for_location(target_draft, root_check["path"], draft_name, root_check["target_os"])
    preflight = _draft_preflight(target_draft, root_check["target_os"])
    if not preflight["valid"]:
        raise RuntimeError("；".join(preflight["issues"]))

    task_manager.update_extract_path(task.task_id, root_check["path"])

    return {
        "target": "draft_local",
        "draft_root": root_check["path"],
        "draft_path": str(target_draft),
        "draft_name": draft_name,
        "ratio": _task_ratio(task),
        "canvas": _task_canvas(task),
        "preflight": preflight,
        "warnings": root_check["warnings"] + preflight["warnings"],
    }


@router.get("/config")
async def get_config():
    """获取模型配置"""
    return Config.load_model_config()


def _readiness_item(
    key: str,
    label: str,
    status: str,
    *,
    provider: Optional[str] = None,
    missing: Optional[List[str]] = None,
    message: str = "",
) -> dict:
    return {
        "key": key,
        "label": label,
        "status": status,
        "provider": provider,
        "missing": list(missing or []),
        "message": message,
        "settings_anchor": f"settings-{key}",
    }


def _config_readiness(voice_type: Optional[str] = None) -> dict:
    """Return secret-free local readiness for the providers this task will use."""
    config = Config.load_model_config()
    llm = config.get("llm") or {}
    provider_id = str(llm.get("provider") or "custom").lower()
    provider_record = get_provider(provider_id) or get_provider("custom") or {}
    provider_options = llm.get("provider_options") or {}
    llm_missing = []
    credential_fields = provider_record.get("credential_fields") or []
    credential_ids = {field.get("id") for field in credential_fields}
    if "model" not in credential_ids and not str(llm.get("model") or "").strip():
        llm_missing.append("模型 ID")
    for field in credential_fields:
        if not field.get("required"):
            continue
        field_id = field.get("id")
        value = llm.get(field_id)
        if value is None or value == "":
            value = provider_options.get(field_id)
        if value is None or value == "":
            llm_missing.append(str(field.get("label") or field_id))
    if llm_missing:
        llm_status = "not_ready"
        llm_message = "请先补全当前生文服务商的本地配置"
    elif provider_record.get("config_status") == "advanced":
        llm_status = "unknown"
        llm_message = "当前服务商需要在生成时验证部署映射"
    else:
        llm_status = "ready"
        llm_message = "生文和提示词配置已齐全"

    image = config.get("image") or {}
    image_missing = [
        label for field, label in (
            ("api_url", "API URL"),
            ("api_key", "API Key"),
            ("model", "模型 ID"),
        )
        if not str(image.get(field) or "").strip()
    ]
    image_status = "not_ready" if image_missing else "ready"

    tts = config.get("tts") or {}
    default_provider = str(tts.get("provider") or "doubao").lower()
    if voice_type:
        selection = parse_voice_key(voice_type, default_provider=default_provider)
    elif default_provider == "mimo":
        default_voice = (tts.get("mimo") or {}).get("default_voice") or "冰糖"
        selection = parse_voice_key(
            default_voice if str(default_voice).startswith("mimo-clone:")
            else build_voice_key("mimo", default_voice)
        )
    else:
        selection = parse_voice_key(build_voice_key(
            "doubao",
            tts.get("default_voice") or "zh_male_jieshuoxiaoming_moon_bigtts",
        ))
    tts_missing = []
    enabled_providers = tts.get("enabled_providers") or ["doubao", "mimo"]
    if selection.provider not in enabled_providers:
        tts_missing.append("当前配音服务商未启用")
    if selection.provider == "mimo":
        mimo = tts.get("mimo") or {}
        for field, label in (
            ("base_url", "Base URL"),
            ("api_key", "API Key"),
            ("model", "模型 ID"),
        ):
            if not str(mimo.get(field) or "").strip():
                tts_missing.append(label)
        if selection.kind == "clone":
            clone = mysql_client.get_voice_clone(selection.voice_id)
            if not clone or clone.get("status") != "ready" or not clone.get("is_enabled"):
                tts_missing.append("克隆音色尚未试听成功并启用")
    else:
        auth_method = str(tts.get("auth_method") or "access_token").lower()
        if not str(tts.get("api_url") or "").strip():
            tts_missing.append("API URL")
        if auth_method == "api_key":
            if not str(tts.get("api_key") or "").strip():
                tts_missing.append("API Key")
        else:
            if not str(tts.get("appid") or "").strip():
                tts_missing.append("App ID")
            if not str(tts.get("token") or "").strip():
                tts_missing.append("Access Token")
    tts_status = "not_ready" if tts_missing else "ready"

    items = [
        _readiness_item(
            "llm",
            "文案与提示词",
            llm_status,
            provider=provider_id,
            missing=llm_missing,
            message=llm_message,
        ),
        _readiness_item(
            "image",
            "图片生成",
            image_status,
            provider="agnes",
            missing=image_missing,
            message=(
                "生图配置已齐全"
                if image_status == "ready"
                else "请先补全 Agnes 生图配置"
            ),
        ),
        _readiness_item(
            "tts",
            "配音",
            tts_status,
            provider=selection.provider,
            missing=tts_missing,
            message=(
                "当前音色所需的配音配置已齐全"
                if tts_status == "ready"
                else "请先补全当前音色所属服务商的配置"
            ),
        ),
    ]
    statuses = {item["status"] for item in items}
    overall = (
        "not_ready" if "not_ready" in statuses
        else "unknown" if "unknown" in statuses
        else "ready"
    )
    return {
        "status": overall,
        "can_continue": overall != "not_ready",
        "items": items,
    }


@router.get("/config/readiness")
async def get_config_readiness(voice_type: Optional[str] = Query(None)):
    return _config_readiness(voice_type)


@router.put("/config")
async def update_config(config: dict = Body(...)):
    """更新模型配置"""
    return Config.save_model_config(config)


@router.post("/config/test-tts")
async def test_tts_config(request: Request, payload: dict = Body(...)):
    """用当前表单配置合成一句短音频，确认 TTS 配置能真实跑通。"""
    model_config = Config.load_model_config()
    incoming_tts = payload.get("tts")
    if isinstance(incoming_tts, dict):
        model_config["tts"].update({
            key: value
            for key, value in incoming_tts.items()
            if value is not None
        })
    Config._normalize_model_config(model_config)

    test_text = str(payload.get("text") or "InsightCut 配音配置测试成功。")[:80]
    if (model_config["tts"].get("provider") or "doubao").lower() == "mimo":
        voice_type = payload.get("voice_type") or (model_config["tts"].get("mimo") or {}).get("default_voice")
    else:
        voice_type = payload.get("voice_type") or model_config["tts"].get("default_voice")
    output_dir = Config.BASE_DIR / "data" / "media" / "_config_tests"
    filename = f"tts_{uuid.uuid4().hex[:10]}"

    try:
        generator = VoiceOverGenerator(output_dir=str(output_dir), tts_config=model_config["tts"])
        audio_path = Path(generator.generate(test_text, filename=filename, voice_type=voice_type))
    except Exception as exc:
        safe = classify_exception(exc, provider="tts")
        logger.error("TTS 配置测试失败: %s", safe.safe_message)
        raise HTTPException(
            status_code=502,
            detail=safe.safe_message,
            headers={"X-Error-Code": safe.code.value},
        ) from None

    media_path = f"_config_tests/{audio_path.name}"
    return {
        "ok": True,
        "provider": model_config["tts"].get("provider"),
        "auth_method": model_config["tts"].get("auth_method"),
        "voice_type": voice_type,
        "url": str(request.url_for("media", file_path=media_path)) if request else f"/media/{media_path}",
    }


@router.post("/config/models")
async def fetch_config_models(config: dict = Body(...)):
    """根据当前填写的 Base URL 和 API Key 拉取可选模型列表。"""
    protocol = (config.get("protocol") or "openai").lower()
    result = None
    error_status = None
    error_detail = None
    try:
        result = refresh_provider_models("custom", config)
    except ProviderModelSyncError as exc:
        error_status = exc.status_code
        error_detail = exc.public_message
    config = None
    if error_status is not None:
        result = None
        raise HTTPException(
            status_code=error_status, detail=error_detail
        ) from None

    prefix = f"{protocol}/"
    models = []
    for model in result["models"]:
        model_id = str(model.get("id") or "")
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
        models.append({"id": model_id, "label": model.get("label") or model_id})
    return {"models_url": result["models_url"], "models": models}


@router.get("/config/llm-providers")
async def get_llm_providers():
    return {"providers": list_llm_providers()}


@router.get("/config/llm-providers/{provider_id}/models")
async def get_llm_provider_models(provider_id: str):
    if not get_provider(provider_id):
        raise HTTPException(status_code=404, detail="生文服务商不存在")
    return {
        "provider": provider_id,
        "models": list_provider_models(provider_id),
    }


@router.post("/config/llm-providers/{provider_id}/models/refresh")
async def refresh_llm_provider_models(
    provider_id: str, payload: dict = Body(...)
):
    result = None
    error_status = None
    error_detail = None
    try:
        result = refresh_provider_models(provider_id, payload)
    except ProviderModelSyncError as exc:
        error_status = exc.status_code
        error_detail = exc.public_message
    payload = None
    if error_status is not None:
        result = None
        raise HTTPException(
            status_code=error_status, detail=error_detail
        ) from None
    return result


@router.get("/render-config")
async def get_render_config():
    """获取前端实时预览和 FFmpeg 导出共用的渲染参数。"""
    from src.export.ffmpeg_exporter import FFmpegExporter

    return FFmpegExporter.get_render_config()


@router.get("/voices")
async def get_voices(
    provider: Optional[str] = Query(None),
    include_disabled: bool = Query(False),
):
    """返回双 provider 预置音色与 MiMo 本地克隆音色。"""
    normalized_provider = (provider or "").strip().lower() or None
    if normalized_provider not in {None, "doubao", "mimo"}:
        raise HTTPException(status_code=400, detail="provider 只支持 doubao 或 mimo")
    presets = mysql_client.list_tts_voices(
        provider=normalized_provider,
        include_disabled=include_disabled,
    )
    result = [
        {
            "id": voice["id"],
            "voice_id": voice["voice_id"],
            "name": voice["name"],
            "gender": voice.get("gender") or "unknown",
            "language": voice.get("language") or "zh",
            "description": voice.get("description") or "",
            "provider": voice["provider"],
            "kind": "preset",
            "source": voice.get("source") or "builtin",
            "is_enabled": bool(voice.get("is_enabled")),
            "status": "ready",
            "preview_url": voice.get("preview_url"),
            "capabilities": voice.get("capabilities") or {},
        }
        for voice in presets
    ]
    if normalized_provider in {None, "mimo"}:
        for clone in _voice_clone_store().list(include_hidden=include_disabled):
            if not include_disabled and not (
                clone.get("status") == "ready" and clone.get("is_enabled")
            ):
                continue
            result.append({
                "id": clone["voice_type"],
                "voice_id": clone["clone_id"],
                "name": clone["name"],
                "gender": "custom",
                "language": "auto",
                "description": "MiMo 本地参考音频克隆音色",
                "provider": "mimo",
                "kind": "clone",
                "source": "local-clone",
                "is_enabled": bool(clone.get("is_enabled")),
                "status": clone.get("status"),
                "preview_url": clone.get("preview_url"),
                "capabilities": {"style_prompt": True, "speed_level": True},
            })
    return result


@router.put("/voices/availability")
async def update_voice_availability(payload: dict = Body(...)):
    voice_keys = payload.get("voice_keys")
    if not isinstance(voice_keys, list) or not all(isinstance(key, str) for key in voice_keys):
        raise HTTPException(status_code=400, detail="voice_keys 必须是音色 ID 数组")
    try:
        mysql_client.set_voice_availability(voice_keys)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    enabled = mysql_client.list_tts_voices()
    return {"enabled_voice_keys": [voice["id"] for voice in enabled]}


@router.post("/voices/preview")
async def preview_voice(payload: dict = Body(...)):
    voice_type = str(payload.get("voice_type") or "").strip()
    if not voice_type:
        raise HTTPException(status_code=400, detail="请选择试听音色")
    selection = parse_voice_key(
        voice_type,
        default_provider=Config.tts_config().get("provider"),
    )
    text = (
        PRESET_VOICE_PREVIEW_TEXT
        if selection.kind == "preset"
        else str(payload.get("text") or Config.tts_config().get("preview_text") or "这是音色试听。")[:120]
    )
    store = _voice_clone_store()
    try:
        service = VoicePreviewService(
            base_dir=Config.BASE_DIR,
            tts_config=Config.tts_config(),
            clone_store=store,
        )
        return service.generate(
            voice_type,
            text,
            payload.get("tts_options") or {},
            config_override=payload.get("config_override"),
        )
    except ValueError as exc:
        safe = classify_exception(exc, provider="tts")
        raise HTTPException(
            status_code=400,
            detail=safe.safe_message,
            headers={"X-Error-Code": safe.code.value},
        ) from None
    except Exception as exc:
        safe = classify_exception(exc, provider="tts")
        logger.error("TTS 音色试听失败: %s", safe.safe_message)
        if getattr(getattr(exc, "response", None), "status_code", None) == 403:
            raise HTTPException(
                status_code=409,
                detail="当前 TTS 账号未授权该音色，请先在服务商控制台开通",
            ) from None
        raise HTTPException(
            status_code=502,
            detail=safe.safe_message,
            headers={"X-Error-Code": safe.code.value},
        ) from None


@router.get("/voice-clones")
async def list_voice_clones(include_hidden: bool = Query(False)):
    return _voice_clone_store().list(include_hidden=include_hidden)


@router.post("/voice-clones")
async def create_voice_clone(
    name: str = Form(...),
    consent_confirmed: bool = Form(...),
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "reference.wav").suffix.lower() or ".wav"
    if suffix not in {".mp3", ".wav", ".webm", ".ogg"}:
        raise HTTPException(status_code=400, detail="只支持 MP3、WAV 或浏览器录音")
    content = await file.read(MAX_VOICE_REFERENCE_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="参考音频为空")
    if len(content) > MAX_VOICE_REFERENCE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="参考音频不能超过 20 MB")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as staged:
            staged.write(content)
            temporary = Path(staged.name)
        return _voice_clone_store().create(name, temporary, consent_confirmed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


@router.patch("/voice-clones/{clone_id}")
async def update_voice_clone(clone_id: str, payload: dict = Body(...)):
    try:
        return _voice_clone_store().update(clone_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/voice-clones/{clone_id}/reference")
async def replace_voice_clone_reference(
    clone_id: str,
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "reference.wav").suffix.lower() or ".wav"
    content = await file.read(MAX_VOICE_REFERENCE_UPLOAD_BYTES + 1)
    if not content or len(content) > MAX_VOICE_REFERENCE_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="参考音频为空或过大")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as staged:
            staged.write(content)
            temporary = Path(staged.name)
        return _voice_clone_store().replace_reference(clone_id, temporary)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


@router.post("/voice-clones/{clone_id}/preview")
async def preview_voice_clone(clone_id: str, payload: dict = Body(default={})):
    store = _voice_clone_store()
    clone = store.get(clone_id)
    if not clone:
        raise HTTPException(status_code=404, detail="克隆音色不存在")
    text = str(payload.get("text") or Config.tts_config().get("preview_text") or "这是我的声音试听。")[:120]
    try:
        preview = VoicePreviewService(
            base_dir=Config.BASE_DIR,
            tts_config=Config.tts_config(),
            clone_store=store,
        ).generate(
            clone["voice_type"],
            text,
            payload.get("tts_options") or {},
            config_override=payload.get("config_override"),
        )
        ready = store.mark_ready(clone_id, Path(preview["path"]))
        return {"clone": ready, "preview": preview}
    except Exception as exc:
        safe = classify_exception(exc, provider="mimo")
        store.mark_failed(clone_id, safe.safe_message)
        logger.error("MiMo 克隆音色试听失败: %s", safe.safe_message)
        raise HTTPException(
            status_code=502,
            detail=safe.safe_message,
            headers={"X-Error-Code": safe.code.value},
        ) from None


@router.delete("/voice-clones/{clone_id}")
async def delete_voice_clone(clone_id: str):
    try:
        return _voice_clone_store().delete_or_hide(clone_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/documents/extract-text")
async def extract_document_text(file: UploadFile = File(...)):
    """提取上传文档中的纯文本，用于文稿页导入。"""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只支持 TXT、Markdown、DOCX、PDF 文档")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文档内容为空")
    if len(content) > MAX_DOCUMENT_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文档不能超过 20MB")

    text, document_type = _extract_uploaded_document_text(filename, content)
    normalized = _normalize_document_text(text)
    if not normalized:
        raise HTTPException(status_code=400, detail="未能从文档中提取到可用文字")

    return {
        "filename": filename,
        "type": document_type,
        "text": normalized,
        "char_count": len(normalized.replace("\n", "")),
    }


def _activity_step(row: dict) -> int:
    status = row.get("status")
    phase = str(row.get("workflow_phase") or "")
    current_step = str(row.get("current_step") or "")
    if status == TaskStatus.COMPLETED.value or phase == "ready":
        return 6
    if status == TaskStatus.AWAITING_FINALIZATION.value or phase in {"awaiting_finalization", "finalizing"}:
        return 6
    if phase in {"generating_assets", "repairing_assets", "assets_requested"} or current_step in {"image_generation", "voiceover_generation"}:
        return 5
    if status == TaskStatus.AWAITING_CONFIRMATION.value:
        return 3 if not row.get("voice_confirmed") else 4
    if current_step in {"image_prompt_generation", "segmentation"} or phase == "planning":
        return 2
    return 1


def _activity_item(row: dict) -> dict:
    total = int(row.get("segments_total") or 0)
    images = int(row.get("images_ready") or 0)
    audio = int(row.get("audio_ready") or 0)
    asset_total = total * 2
    completed = images + audio
    if asset_total:
        progress = round(completed / asset_total * 100)
    elif row.get("status") == TaskStatus.COMPLETED.value:
        progress = 100
    elif row.get("status") == TaskStatus.PENDING.value:
        progress = 4
    else:
        progress = 12 if _activity_step(row) <= 2 else 50
    operation_total = 0
    try:
        operation_total = len(json.loads(row.get("targets_json") or "[]"))
    except (TypeError, ValueError):
        operation_total = 0
    step = _activity_step(row)
    route = f"/export/{row['task_id']}" if step == 6 and row.get("status") == TaskStatus.COMPLETED.value else f"/workspace/{row['task_id']}"
    return {
        "task_id": row.get("task_id"),
        "name": row.get("name") or row.get("theme") or "未命名项目",
        "status": row.get("status"),
        "stage": row.get("workflow_phase") or row.get("current_step") or row.get("status"),
        "step": step,
        "progress": max(0, min(100, progress)),
        "segments_total": total,
        "images_ready": images,
        "audio_ready": audio,
        "operation": {
            "operation_id": row.get("operation_id"),
            "kind": row.get("operation_kind"),
            "state": row.get("operation_state"),
            "total": operation_total,
            "completed": int(row.get("completed_count") or 0),
            "failed": int(row.get("failed_count") or 0),
        } if row.get("operation_id") else None,
        "can_cancel": bool(row.get("status") in {TaskStatus.PENDING.value, TaskStatus.PROCESSING.value}),
        "target_route": route,
        "updated_at": row.get("updated_at"),
    }


@router.get("/activity/tasks")
async def get_task_activity(limit: int = Query(20, ge=1, le=50)):
    """Single aggregate used by the global task capsule; never fans out per task."""
    rows = mysql_client.list_task_activity(limit=limit)
    running, attention, recent = [], [], []
    attention_statuses = {
        TaskStatus.AWAITING_CONFIRMATION.value,
        TaskStatus.AWAITING_FINALIZATION.value,
        TaskStatus.INTERRUPTED.value,
        TaskStatus.FAILED.value,
    }
    for row in rows:
        item = _activity_item(row)
        if row.get("status") in attention_statuses:
            attention.append(item)
        elif row.get("status") in {TaskStatus.PENDING.value, TaskStatus.PROCESSING.value} or row.get("operation_id"):
            running.append(item)
        else:
            recent.append(item)
    return {
        "running": running,
        "attention": attention,
        "recent": recent,
        "counts": {"running": len(running), "attention": len(attention)},
        "polled_at": datetime.now().isoformat(),
    }


@router.get("/templates")
async def list_production_templates():
    return {"items": mysql_client.list_production_templates()}


@router.post("/templates", status_code=201)
async def create_production_template(payload: dict = Body(...)):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名称不能为空")
    values = dict(payload)
    values["name"] = name[:80]
    values["ratio"] = normalize_ratio(values.get("ratio") or "16:9")
    values["subtitle_options"] = _normalize_subtitle_options(values.get("subtitle_options"))
    values["generation_options"] = _normalize_generation_options(values.get("generation_options"))
    created = mysql_client.create_production_template(values)
    if not created:
        raise HTTPException(status_code=500, detail="创建模板失败")
    return created


@router.patch("/templates/{template_id}")
async def update_production_template(template_id: str, payload: dict = Body(...)):
    values = dict(payload)
    if "name" in values and not str(values.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="模板名称不能为空")
    if "name" in values:
        values["name"] = str(values["name"]).strip()[:80]
    if "ratio" in values:
        values["ratio"] = normalize_ratio(values["ratio"])
    if "subtitle_options" in values:
        values["subtitle_options"] = _normalize_subtitle_options(values["subtitle_options"])
    if "generation_options" in values:
        values["generation_options"] = _normalize_generation_options(values["generation_options"])
    updated = mysql_client.update_production_template(template_id, values)
    if not updated:
        raise HTTPException(status_code=404, detail="模板不存在")
    return updated


@router.delete("/templates/{template_id}", status_code=204)
async def delete_production_template(template_id: str):
    if not mysql_client.delete_production_template(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return Response(status_code=204)


@router.post("/tasks", response_model=CreateTaskResponse)
async def create_task(request: CreateTaskRequest):
    """
    创建视频生成任务

    - **theme**: 视频主题或剧本文案（主题模式 1-100 字；文稿模式 1-5000 字）
    - **style**: 文章风格或“文章风格|画面风格”（默认：温暖感人）
    - **length**: 主题模式下的目标脚本字数（50-2000，默认：300）
    - **voice_type**: TTS 音色 ID（可选）
    """
    input_mode = "theme" if request.input_mode == "theme" else "script"
    theme_text = request.theme.strip()
    if input_mode == "theme" and len(theme_text) > 100:
        raise HTTPException(status_code=400, detail="主题模式最多输入 100 字")

    voice_type = _resolve_new_task_voice(request.voice_type)
    tts_options = _snapshot_tts_options(
        voice_type,
        _model_dict(request.tts_options, exclude_none=True),
    )

    # 创建任务
    task_id = task_manager.create_task(
        theme=theme_text,
        name=request.name,
        style=request.style,
        length=request.length,
        voice_type=voice_type,
        ratio=normalize_ratio(request.ratio),
        tts_options=tts_options,
        execution_mode=request.execution_mode,
        script_policy=request.script_policy,
        source_draft_id=request.source_draft_id,
        template_id=request.template_id,
        generation_options=_normalize_generation_options(
            _model_dict(request.generation_options, exclude_none=True)
        ),
        subtitle_options=_normalize_subtitle_options(
            _model_dict(request.subtitle_options, exclude_none=True)
        ),
    )

    # 启动异步执行
    task_executor.execute_task(
        task_id=task_id,
        theme=theme_text,
        style=request.style,
        length=request.length,
        voice_type=voice_type,
        ratio=normalize_ratio(request.ratio),
        input_mode=input_mode,
    )

    return CreateTaskResponse(
        task_id=task_id,
        status="pending"
    )


@router.post("/tasks/create-from-images", response_model=CreateTaskResponse)
async def create_task_from_images(
    images: List[UploadFile] = File(...),
    style: str = Form("温暖感人"),
    ratio: str = Form("16:9"),
    voice_type: Optional[str] = Form(None),
    tts_options: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
):
    """
    使用本地上传图片创建可编辑任务

    - **images**: 本地图片列表（1-20 张，JPG/PNG/WEBP）
    - **style**: 文章风格或“文章风格|画面风格”
    - **voice_type**: TTS 音色 ID（可选）
    - **name**: 项目名称（可选）
    """
    if not images:
        raise HTTPException(status_code=400, detail="请至少上传 1 张图片")
    if len(images) > MAX_UPLOAD_IMAGE_COUNT:
        raise HTTPException(status_code=400, detail=f"最多上传 {MAX_UPLOAD_IMAGE_COUNT} 张图片")
    if name and len(name) > 100:
        raise HTTPException(status_code=400, detail="项目名称最多 100 字")

    for file in images:
        _validate_upload_image(file)

    canonical_voice = _resolve_new_task_voice(voice_type)
    raw_options = _parse_options_json(tts_options)
    option_snapshot = _snapshot_tts_options(canonical_voice, raw_options)

    theme = (name or "").strip() or "本地上传图片"
    task_id = task_manager.create_task(
        theme=theme,
        name=name or theme,
        style=style or "温暖感人",
        length=300,
        voice_type=canonical_voice,
        ratio=normalize_ratio(ratio),
        tts_options=option_snapshot,
    )

    try:
        from src.utils.local_uploader import LocalUploader

        draft_name = _safe_draft_name(name or theme, task_id)
        draft_path = Path("output") / draft_name
        images_dir = draft_path / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        local_uploader = LocalUploader()
        segments_data = []

        for index, file in enumerate(images):
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in ALLOWED_IMAGE_EXTENSIONS:
                suffix = ".jpg"

            local_filename = f"seg_{index:03d}_upload{suffix}"
            local_path = images_dir / local_filename

            with open(local_path, "wb") as f:
                shutil.copyfileobj(file.file, f)

            storage_path = f"{task_id}/images/{local_filename}"
            image_url = local_uploader.upload(str(local_path), storage_path)

            segments_data.append({
                "segment_index": index,
                "text": "",
                "image_prompt": "",
                "image_path": str(local_path),
                "image_url": image_url,
                "audio_path": None,
                "audio_url": None,
                "duration": None,
            })
            _record_asset(
                task_id,
                "image",
                "upload",
                path=str(local_path),
                url=image_url,
                segment_index=index,
                label=f"本地上传 · 分镜 {index + 1}",
            )

        if not mysql_client.save_segments(task_id, segments_data):
            raise RuntimeError("保存段落数据失败")

        task_manager.set_task_result(task_id, str(draft_path), len(segments_data))
        task_manager.update_task_status(task_id, TaskStatus.COMPLETED)

        return CreateTaskResponse(
            task_id=task_id,
            status=TaskStatus.COMPLETED
        )
    except HTTPException:
        task_manager.set_task_error(task_id, "上传图片创建任务失败")
        raise
    except Exception as e:
        safe = classify_exception(e, provider="local_storage")
        logger.error("[%s] 从本地图片创建任务失败: %s", task_id, safe.safe_message)
        task_manager.set_task_error(
            task_id,
            safe.safe_message,
            error_code=safe.code.value,
            error_meta=safe.metadata(),
        )
        raise HTTPException(status_code=500, detail=safe.safe_message)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """
    查询任务状态

    - **task_id**: 任务ID
    """
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return task.to_response()


def _workspace_stage(task_row: dict) -> str:
    status = task_row.get("status")
    # A stopped task must expose its recovery state even when the last persisted
    # workflow phase still says planning/generating_assets. Otherwise the
    # workspace keeps showing a non-running progress state and hides resume.
    if status == TaskStatus.AWAITING_FINALIZATION.value:
        return "awaiting_finalization"
    if status in {TaskStatus.FAILED.value, TaskStatus.INTERRUPTED.value}:
        return status
    phase = str(task_row.get("workflow_phase") or "").strip()
    if phase and phase != "pending":
        if phase == "assets_requested":
            return "generating_assets"
        return phase
    current_step = task_row.get("current_step")
    if status == TaskStatus.COMPLETED.value:
        return "ready"
    if status == TaskStatus.AWAITING_CONFIRMATION.value:
        return "awaiting_confirmation"
    if current_step in {"voiceover_generation", "image_generation", "draft_building"}:
        return "generating_assets"
    return "planning"


def _workspace_storage_candidates(path: Optional[str]) -> List[Path]:
    """Resolve persisted local paths without depending on the process cwd."""
    if not path:
        return []
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            return [candidate.resolve()]

        base_dir = Config.BASE_DIR.resolve()
        parts = candidate.parts
        already_rooted = bool(
            (parts and parts[0] == "output")
            or (len(parts) >= 2 and parts[0] == "data" and parts[1] == "media")
        )
        raw_candidates = (
            [base_dir / candidate]
            if already_rooted
            else [
                base_dir / "output" / candidate,
                base_dir / "data" / "media" / candidate,
                base_dir / candidate,
            ]
        )
        resolved = []
        seen = set()
        for raw_candidate in raw_candidates:
            normalized = raw_candidate.resolve()
            try:
                normalized.relative_to(base_dir)
            except ValueError:
                continue
            key = str(normalized)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(normalized)
        return resolved
    except (OSError, RuntimeError):
        return []


def _workspace_resolve_file(path: Optional[str]) -> Optional[Path]:
    for candidate in _workspace_storage_candidates(path):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _workspace_resolve_directory(path: Optional[str]) -> Optional[Path]:
    for candidate in _workspace_storage_candidates(path):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _workspace_file_ready(path: Optional[str]) -> bool:
    return _workspace_resolve_file(path) is not None


def _workspace_draft_ready(task_row: dict) -> bool:
    result = task_row.get("result") or {}
    draft_path = result.get("draft_path")
    if not draft_path:
        return False
    return _workspace_resolve_directory(draft_path) is not None


def _workspace_output_dir(task, segments: List[dict]) -> Path:
    # Prefer the live project media root. A completed task's result points into
    # an immutable finalize version and must not become the upload workspace.
    for segment in segments:
        for field in ("image_path", "audio_path"):
            raw_path = segment.get(field)
            if not raw_path:
                continue
            path = _workspace_resolve_file(raw_path)
            if path and path.parent.name in {"images", "voiceovers", "audio"}:
                return path.parent.parent
    result = getattr(task, "result", None)
    if result and result.draft_path:
        resolved_draft = _workspace_resolve_directory(result.draft_path)
        if resolved_draft:
            return resolved_draft
        candidates = _workspace_storage_candidates(result.draft_path)
        if candidates:
            return candidates[0]
    name = getattr(task, "name", None) or getattr(task, "theme", None) or task.task_id
    return Config.BASE_DIR / "output" / task.task_id / _safe_draft_name(name, task.task_id)


def _ensure_workspace_mutable(task_row: dict) -> None:
    if task_row.get("status") == TaskStatus.PROCESSING.value:
        raise HTTPException(status_code=409, detail="任务正在生成，请等待当前阶段完成后再修改")


def _invalidate_review_first_draft(task_id: str, task_row: dict) -> None:
    """Require a fresh draft after selecting or uploading different media."""
    if task_row.get("execution_mode") != "review_first":
        return
    if not mysql_client.invalidate_task_result_for_finalization(task_id):
        raise HTTPException(status_code=500, detail="素材已保存，但生产草稿状态更新失败")
    invalidate = getattr(task_manager, "invalidate_task_cache", None)
    if callable(invalidate):
        invalidate(task_id)


def _workspace_health(task_row: dict, segments: List[dict]) -> dict:
    segment_count = len(segments)
    prompts_ready = sum(
        1 for segment in segments if str(segment.get("image_prompt") or "").strip()
    )
    images_ready = sum(
        1
        for segment in segments
        if segment.get("image_status") == "completed"
        and _workspace_file_ready(segment.get("image_path"))
    )
    audio_ready = sum(
        1
        for segment in segments
        if segment.get("audio_status") == "completed"
        and _workspace_file_ready(segment.get("audio_path"))
    )

    def count_asset_state(asset_type: str, expected_state: str) -> int:
        return sum(
            1
            for segment in segments
            if _workspace_asset_state(segment, asset_type) == expected_state
        )

    return {
        "segments": segment_count,
        "prompts_ready": prompts_ready,
        "missing_prompts": max(0, segment_count - prompts_ready),
        "images_ready": images_ready,
        "missing_images": max(0, segment_count - images_ready),
        "stale_images": count_asset_state("image", "stale"),
        "failed_images": count_asset_state("image", "failed"),
        "pending_images": count_asset_state("image", "missing"),
        "audio_ready": audio_ready,
        "missing_audio": max(0, segment_count - audio_ready),
        "stale_audio": count_asset_state("audio", "stale"),
        "failed_audio": count_asset_state("audio", "failed"),
        "pending_audio": count_asset_state("audio", "missing"),
        "plan_complete": bool(segment_count and prompts_ready == segment_count),
        "assets_complete": bool(
            segment_count
            and images_ready == segment_count
            and audio_ready == segment_count
        ),
        "draft_ready": _workspace_draft_ready(task_row),
    }


def _workspace_asset_state(segment: dict, asset_type: str) -> str:
    """Classify persisted media without conflating stale with failed/missing."""
    status = str(segment.get(f"{asset_type}_status") or "pending")
    file_ready = _workspace_file_ready(segment.get(f"{asset_type}_path"))
    if status == "completed" and file_ready:
        return "ready"
    if status == "stale" and file_ready:
        return "stale"
    if status == "failed" or (status in {"completed", "stale"} and not file_ready):
        return "failed"
    return "missing"


def _workspace_recovery_targets(segments: List[dict]) -> List[dict]:
    targets = []
    for segment in segments:
        segment_index = int(segment.get("segment_index") or 0)
        for asset_type, label in (("image", "图片"), ("audio", "配音")):
            status = segment.get(f"{asset_type}_status") or "pending"
            state = _workspace_asset_state(segment, asset_type)
            if state in {"ready", "stale"}:
                continue
            if segment.get(f"{asset_type}_error"):
                reason, error_code, error_meta = _public_error(
                    segment.get(f"{asset_type}_error"),
                    segment.get(f"{asset_type}_error_code"),
                    segment.get(f"{asset_type}_error_meta"),
                )
            else:
                missing = SafeError(
                    code=ErrorCode.DISK,
                    retryable=False,
                    safe_message=f"{label}文件缺失",
                    provider="local_storage",
                )
                reason = missing.safe_message
                error_code = missing.code.value
                error_meta = missing.metadata()
            targets.append({
                "segment_index": segment_index,
                "asset_type": asset_type,
                "status": status,
                "reason": reason,
                "error_code": error_code,
                "error_meta": error_meta,
            })
    return targets


def _workspace_stale_targets(segments: List[dict]) -> List[dict]:
    targets = []
    for segment in segments:
        segment_index = int(segment.get("segment_index") or 0)
        for asset_type, label in (("image", "图片"), ("audio", "配音")):
            if _workspace_asset_state(segment, asset_type) != "stale":
                continue
            targets.append({
                "segment_index": segment_index,
                "asset_type": asset_type,
                "status": "stale",
                "reason": f"{label}与当前预案不一致，需要更新",
            })
    return targets


def _reconcile_workspace_state(task_row: dict, segments: List[dict]) -> dict:
    """Repair impossible persisted states before exposing workspace actions."""
    if task_row.get("execution_mode") != "review_first":
        return task_row

    task_id = task_row["task_id"]
    status = task_row.get("status")
    phase = str(task_row.get("workflow_phase") or "")
    health = _workspace_health(task_row, segments)
    next_phase = None
    next_status = None
    next_step = None
    error = None

    if status == TaskStatus.COMPLETED.value and not (
        health["plan_complete"] and health["assets_complete"] and health["draft_ready"]
    ):
        next_status = TaskStatus.INTERRUPTED.value
        if not health["plan_complete"]:
            next_phase = "planning"
            next_step = "image_prompt_generation" if segments else "text_generation"
            error = "检测到预案内容不完整，已保留现有内容，可继续生成"
        else:
            next_phase = "awaiting_finalization" if health["assets_complete"] else "generating_assets"
            next_status = (
                TaskStatus.AWAITING_FINALIZATION.value
                if health["assets_complete"]
                else TaskStatus.INTERRUPTED.value
            )
            next_step = "awaiting_finalization" if health["assets_complete"] else "image_generation"
            error = (
                "检测到生产草稿缺失，可使用现有素材恢复"
                if health["assets_complete"]
                else "检测到素材与当前预案不一致，可精确更新受影响部分"
                if health["stale_images"] or health["stale_audio"]
                else "检测到本地素材缺失，可重新生成缺失部分"
            )
    elif (
        status in {TaskStatus.FAILED.value, TaskStatus.INTERRUPTED.value}
        and health["plan_complete"]
        and health["assets_complete"]
        and not health["draft_ready"]
        and task_row.get("current_step") != "finalize_failed"
    ):
        next_status = TaskStatus.AWAITING_FINALIZATION.value
        next_phase = "awaiting_finalization"
        next_step = "awaiting_finalization"
        error = None
    elif status == TaskStatus.AWAITING_FINALIZATION.value and not health["assets_complete"]:
        next_status = TaskStatus.INTERRUPTED.value
        next_phase = "generating_assets"
        next_step = "asset_repair"
        error = "检测到素材不完整，请先修复缺失素材"
    elif status == TaskStatus.AWAITING_CONFIRMATION.value and not health["plan_complete"]:
        next_status = TaskStatus.INTERRUPTED.value
        next_phase = "planning"
        next_step = "image_prompt_generation" if segments else "text_generation"
        error = "预案尚未完整生成，已切换为可恢复状态"
    elif (
        status in {TaskStatus.FAILED.value, TaskStatus.INTERRUPTED.value}
        and phase == "awaiting_confirmation"
        and health["plan_complete"]
    ):
        next_status = TaskStatus.AWAITING_CONFIRMATION.value
        next_phase = "awaiting_confirmation"
        next_step = "awaiting_confirmation"
        error = None

    if not next_status:
        return task_row

    mysql_client.update_task_status(task_id, next_status, next_step, error)
    mysql_client.update_task_workflow(
        task_id,
        next_phase,
        status=next_status,
        current_step=next_step,
    )
    invalidate = getattr(task_manager, "invalidate_task_cache", None)
    if callable(invalidate):
        invalidate(task_id)
    return mysql_client.get_task(task_id) or task_row


def _workspace_recovery(
    task_row: dict,
    stage: str,
    health: dict,
    targets: Optional[List[dict]] = None,
    stale_targets: Optional[List[dict]] = None,
) -> dict:
    targets = targets or []
    stale_targets = stale_targets or []
    can_resume = bool(
        stage in {TaskStatus.FAILED.value, TaskStatus.INTERRUPTED.value}
        and (task_row.get("theme") or task_row.get("script_text") or health["segments"])
    )
    if can_resume and not health["segments"]:
        return {
            "allowed": True,
            "mode": "restart_planning",
            "label": "重新开始生成",
            "description": "生成在文案阶段中断，可从原始内容重新开始",
            "targets": [],
        }
    if can_resume and health["missing_prompts"]:
        count = health["missing_prompts"]
        return {
            "allowed": True,
            "mode": "resume_planning",
            "label": f"继续生成 {count} 段提示词",
            "description": f"已有内容已保存，仍有 {count} 段提示词待生成",
            "targets": [],
        }
    asset_recovery_stage = stage in {
        TaskStatus.FAILED.value,
        TaskStatus.INTERRUPTED.value,
    }
    if targets and asset_recovery_stage:
        missing_assets = len(targets)
        return {
            "allowed": True,
            "mode": "retry_assets",
            "label": f"重试 {missing_assets} 个缺失或失败素材",
            "description": (
                f"图片 {health['images_ready']}/{health['segments']} · "
                f"配音 {health['audio_ready']}/{health['segments']} · "
                f"{missing_assets} 项待重试"
            ),
            "targets": targets,
        }
    if stale_targets and (asset_recovery_stage or stage == "ready"):
        count = len(stale_targets)
        return {
            "allowed": True,
            "mode": "update_stale_assets",
            "label": f"更新 {count} 个受影响素材",
            "description": "旧素材仍可查看，但需要更新后才能完成生产或生成完整视频",
            "targets": stale_targets,
        }
    if (
        health["assets_complete"]
        and not health["draft_ready"]
        and (asset_recovery_stage or stage == "awaiting_finalization")
    ):
        finalize_failed = task_row.get("current_step") == "finalize_failed"
        return {
            "allowed": True,
            "mode": "finalize_failed" if finalize_failed else "finalize",
            "label": "重新完成生产" if finalize_failed else "完成生产并进入预览",
            "description": (
                "素材均已保留；请处理草稿构建问题后重新完成生产"
                if finalize_failed
                else "图片与配音均已齐全，等待构建生产草稿"
            ),
            "targets": [],
        }
    return {"allowed": False, "mode": None, "label": None, "description": None, "targets": []}


def _workspace_operation_payload(operation: Optional[dict]) -> Optional[dict]:
    if not operation:
        return None
    targets = operation.get("targets") or []
    error, error_code, error_meta = _public_error(
        operation.get("error"),
        operation.get("error_code"),
        operation.get("error_meta"),
    )
    return {
        "operation_id": operation.get("operation_id"),
        "kind": operation.get("kind"),
        "status": operation.get("state"),
        "total": len(targets),
        "completed": int(operation.get("completed_count") or 0),
        "failed": int(operation.get("failed_count") or 0),
        "targets": targets,
        "error": error,
        "error_code": error_code,
        "error_meta": error_meta,
    }


def _workspace_segment_payload(segment: dict, request: Request) -> dict:
    image_file_ready = _workspace_file_ready(segment.get("image_path"))
    audio_file_ready = _workspace_file_ready(segment.get("audio_path"))
    image_status = segment.get("image_status") or (
        "completed" if image_file_ready else "pending"
    )
    audio_status = segment.get("audio_status") or (
        "completed" if audio_file_ready else "pending"
    )
    image_error = segment.get("image_error")
    audio_error = segment.get("audio_error")
    prompt_error, prompt_error_code, prompt_error_meta = _public_error(
        segment.get("prompt_error"),
        segment.get("prompt_error_code"),
        segment.get("prompt_error_meta"),
    )
    image_error, image_error_code, image_error_meta = _public_error(
        image_error,
        segment.get("image_error_code"),
        segment.get("image_error_meta"),
    )
    audio_error, audio_error_code, audio_error_meta = _public_error(
        audio_error,
        segment.get("audio_error_code"),
        segment.get("audio_error_meta"),
    )
    if image_status == "completed" and not image_file_ready:
        image_status = "failed"
        image_error = image_error or "本地图片文件缺失，请重新生成"
        missing = SafeError(
            code=ErrorCode.DISK,
            retryable=False,
            safe_message=image_error,
            provider="local_storage",
        )
        image_error_code = missing.code.value
        image_error_meta = missing.metadata()
    if audio_status == "completed" and not audio_file_ready:
        audio_status = "failed"
        audio_error = audio_error or "本地音频文件缺失，请重新生成"
        missing = SafeError(
            code=ErrorCode.DISK,
            retryable=False,
            safe_message=audio_error,
            provider="local_storage",
        )
        audio_error_code = missing.code.value
        audio_error_meta = missing.metadata()
    image_storage_warning = None
    audio_storage_warning = None
    if image_status in {"completed", "stale"} and image_file_ready and image_error:
        image_storage_warning = image_error_meta
        image_error = image_error_code = image_error_meta = None
    if audio_status in {"completed", "stale"} and audio_file_ready and audio_error:
        audio_storage_warning = audio_error_meta
        audio_error = audio_error_code = audio_error_meta = None
    payload = {
        "id": segment.get("id"),
        "task_id": segment.get("task_id"),
        "segment_index": segment.get("segment_index"),
        "text": segment.get("text") or "",
        "image_prompt": segment.get("image_prompt") or "",
        "prompt_status": segment.get("prompt_status") or (
            "completed" if segment.get("image_prompt") else "pending"
        ),
        "prompt_error": prompt_error,
        "prompt_error_code": prompt_error_code,
        "prompt_error_meta": prompt_error_meta,
        "prompt_manual": bool(segment.get("prompt_manual")),
        "prompt_needs_review": bool(segment.get("prompt_needs_review")),
        "image_url": (
            _normalize_local_media_url(segment.get("image_url"), request)
            or _local_media_url_from_path(segment.get("image_path"), request)
        ) if image_file_ready else None,
        "audio_url": (
            _normalize_local_media_url(segment.get("audio_url"), request)
            or _local_media_url_from_path(segment.get("audio_path"), request)
        ) if audio_file_ready else None,
        "image_status": image_status,
        "image_error": image_error,
        "image_error_code": image_error_code,
        "image_error_meta": image_error_meta,
        "image_storage_warning": image_storage_warning,
        "audio_status": audio_status,
        "audio_error": audio_error,
        "audio_error_code": audio_error_code,
        "audio_error_meta": audio_error_meta,
        "audio_storage_warning": audio_storage_warning,
        "audio_voice_type": segment.get("audio_voice_type"),
        "audio_tts_options_json": segment.get("audio_tts_options_json"),
        "selected_image_asset_id": segment.get("selected_image_asset_id"),
        "selected_audio_asset_id": segment.get("selected_audio_asset_id"),
        "audio_mismatch_confirmed": bool(segment.get("audio_mismatch_confirmed")),
        "duration": segment.get("duration"),
        "created_at": segment.get("created_at"),
        "updated_at": segment.get("updated_at"),
    }
    return payload


@router.get("/tasks/{task_id}/workspace")
async def get_task_workspace(task_id: str, request: Request):
    """Return the complete persisted state needed by the production workspace."""
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    active_operation = mysql_client.get_active_task_operation(task_id)
    if active_operation and not task_runtime.is_running(task_id):
        orphan_kind = active_operation.get("kind")
        mysql_client.interrupt_orphaned_task_operation(task_id)
        orphan_phase = "finalizing" if orphan_kind == "finalize" else "generating_assets"
        orphan_step = "finalize_failed" if orphan_kind == "finalize" else "asset_repair"
        mysql_client.update_task_workflow(
            task_id,
            orphan_phase,
            status=TaskStatus.INTERRUPTED.value,
            current_step=orphan_step,
        )
        task_manager.invalidate_task_cache(task_id)
        active_operation = {}
        task_row = mysql_client.get_task(task_id) or task_row
    task_manager.fail_stale_task_data(task_row)
    task_row = mysql_client.get_task(task_id) or task_row
    segments = mysql_client.get_segments(task_id)
    task_object = task_manager.get_task(task_id)
    if task_object:
        _ensure_task_assets(task_object, segments)
        segments = mysql_client.get_segments(task_id)
    task_row = _reconcile_workspace_state(task_row, segments)
    health = _workspace_health(task_row, segments)
    recovery_targets = _workspace_recovery_targets(segments)
    stale_targets = _workspace_stale_targets(segments)
    parsed_options = {}
    try:
        parsed_options = json.loads(task_row.get("tts_options_json") or "{}")
    except (TypeError, ValueError):
        parsed_options = {}
    subtitle_options = _normalize_subtitle_options(
        _parse_options_json(task_row.get("subtitle_options_json"))
    )
    generation_options = _normalize_generation_options(
        _parse_options_json(task_row.get("generation_options_json"))
    )

    real_duration = sum(
        float(segment.get("duration") or 0)
        for segment in segments
        if segment.get("duration")
    )
    estimated_duration = real_duration or sum(
        max(3.0, len(str(segment.get("text") or "")) / 4.2)
        for segment in segments
    )
    segment_count = len(segments)
    stage = _workspace_stage(task_row)
    recovery = _workspace_recovery(
        task_row, stage, health, recovery_targets, stale_targets
    )
    current_step = str(task_row.get("current_step") or "")
    if stage == "planning":
        if current_step == "image_prompt_generation":
            planning_step = "image_prompt_generation"
        elif task_row.get("script_text") and not segments:
            planning_step = "segmentation"
        else:
            planning_step = "text_generation"
    else:
        planning_step = None
    generation_min = max(30, segment_count * 3)
    generation_max = max(60, segment_count * 8)
    parts = str(task_row.get("style") or "").split("|", 2)
    result = task_row.get("result") or {}
    workspace_segments = [
        _workspace_segment_payload(segment, request) for segment in segments
    ]
    storage_warnings = [
        {
            "segment_index": segment["segment_index"],
            "asset_type": asset_type,
            "warning": segment.get(f"{asset_type}_storage_warning"),
        }
        for segment in workspace_segments
        for asset_type in ("image", "audio")
        if segment.get(f"{asset_type}_storage_warning")
    ]
    task_error, task_error_code, task_error_meta = _public_error(
        task_row.get("error"),
        task_row.get("error_code"),
        task_row.get("error_meta"),
    )
    return {
        "task_id": task_id,
        "name": task_row.get("name") or task_row.get("theme") or "未命名项目",
        "status": task_row.get("status"),
        "stage": stage,
        "planning_step": planning_step,
        "execution_mode": task_row.get("execution_mode") or "full",
        "input_mode": task_row.get("input_mode") or "script",
        "script_text": task_row.get("script_text") or "",
        "summary": task_row.get("summary") or "",
        "text_style": parts[0] if parts else "知识科普",
        "visual_style": parts[1] if len(parts) > 1 else "电影质感",
        "ratio": normalize_ratio(task_row.get("ratio") or "16:9"),
        "voice_type": task_row.get("voice_type") or "",
        "tts_options": parsed_options,
        "subtitle_options": subtitle_options,
        "generation_options": generation_options,
        "template_id": task_row.get("template_id"),
        "source_draft_id": task_row.get("source_draft_id"),
        "voice_confirmed": bool(
            task_row.get("voice_confirmed") and task_row.get("voice_type")
        ),
        "plan_version": int(task_row.get("plan_version") or 0),
        "snapshot_key": _plan_fingerprint(task_row, segments),
        "segments_count": segment_count,
        "estimated_duration": round(estimated_duration, 1),
        "duration_is_estimate": not bool(real_duration),
        "generation_estimate": {
            "min_seconds": generation_min,
            "max_seconds": generation_max,
        },
        "segments": workspace_segments,
        "storage_warnings": storage_warnings,
        "progress": {
            "prompts_ready": sum(1 for segment in segments if segment.get("image_prompt")),
            "prompts_total": segment_count,
            "prompts_processing": sum(
                1 for segment in segments if segment.get("prompt_status") == "processing"
            ),
            "prompts_failed": sum(
                1 for segment in segments if segment.get("prompt_status") == "failed"
            ),
            "images_ready": health["images_ready"],
            "audio_ready": health["audio_ready"],
        },
        "health": health,
        "recovery": recovery,
        "active_operation": _workspace_operation_payload(active_operation),
        "capabilities": {
            "instant_preview": any(
                segment.get("image_status") in {"completed", "stale"}
                and _workspace_file_ready(segment.get("image_path"))
                and segment.get("audio_status") in {"completed", "stale"}
                and _workspace_file_ready(segment.get("audio_path"))
                for segment in segments
            ),
            "full_video": bool(health["assets_complete"] and health["draft_ready"]),
            "enter_export": bool(health["images_ready"] or health["audio_ready"]),
            "material_export": bool(health["images_ready"] or health["audio_ready"]),
            "retry_failed_assets": bool(
                recovery.get("mode") == "retry_assets" and not active_operation
            ),
            "update_stale_assets": bool(
                recovery.get("mode") == "update_stale_assets" and not active_operation
            ),
            "retry_selected_asset": not bool(active_operation),
            "finalize": bool(
                recovery.get("mode") in {"finalize", "finalize_failed"}
                and not active_operation
            ),
        },
        "draft_url": _normalize_local_media_url(result.get("draft_url"), request),
        "error": task_error,
        "error_code": task_error_code,
        "error_meta": task_error_meta,
        "can_resume": recovery["allowed"],
    }


@router.patch("/tasks/{task_id}/settings")
async def update_task_workspace_settings(task_id: str, payload: dict = Body(...)):
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    _ensure_workspace_mutable(task_row)
    expected_version = payload.get("expected_plan_version")
    old_voice = task_row.get("voice_type") or ""
    old_style = task_row.get("style") or ""
    old_ratio = normalize_ratio(task_row.get("ratio") or "16:9")
    parts = old_style.split("|", 2)
    text_style = payload.get("text_style") or (parts[0] if parts else "知识科普")
    visual_style = payload.get("visual_style") or (parts[1] if len(parts) > 1 else "电影质感")
    style_suffix = parts[2] if len(parts) > 2 else None
    style_fields_changed = "text_style" in payload or "visual_style" in payload
    style = (
        "|".join(part for part in (text_style, visual_style, style_suffix) if part)
        if style_fields_changed
        else old_style
    )
    ratio = normalize_ratio(payload.get("ratio") or old_ratio)
    voice_type = payload.get("voice_type", old_voice)
    tts_options = payload.get("tts_options")
    if tts_options is None:
        tts_options_json = task_row.get("tts_options_json")
    else:
        tts_options_json = json.dumps(
            _snapshot_tts_options(voice_type, tts_options), ensure_ascii=False
        )
    old_subtitle_options = _normalize_subtitle_options(
        _parse_options_json(task_row.get("subtitle_options_json"))
    )
    old_generation_options = _normalize_generation_options(
        _parse_options_json(task_row.get("generation_options_json"))
    )
    subtitle_options = _normalize_subtitle_options(
        payload.get("subtitle_options") or old_subtitle_options
    )
    generation_options = _normalize_generation_options(
        payload.get("generation_options") or old_generation_options
    )
    voice_confirmed = (
        bool(payload.get("voice_confirmed"))
        if "voice_confirmed" in payload
        else bool(task_row.get("voice_confirmed"))
    )
    updates = {
        "style": style,
        "ratio": ratio,
        "voice_type": voice_type,
        "tts_options_json": tts_options_json,
        "voice_confirmed": 1 if voice_confirmed and voice_type else 0,
        "template_id": payload.get("template_id", task_row.get("template_id")),
        "subtitle_options_json": json.dumps(subtitle_options, ensure_ascii=False),
        "generation_options_json": json.dumps(generation_options, ensure_ascii=False),
    }
    next_version = mysql_client.update_task_plan_fields(
        task_id, updates, expected_plan_version=expected_version
    )
    if next_version == -1:
        raise HTTPException(status_code=409, detail="预案已在其他页面更新，请刷新后重试")
    if next_version is None:
        raise HTTPException(status_code=500, detail="保存工作台设置失败")

    style_changed = style != old_style or ratio != old_ratio
    voice_changed = voice_type != old_voice or tts_options_json != task_row.get("tts_options_json")
    subtitle_changed = subtitle_options != old_subtitle_options
    segments = mysql_client.get_segments(task_id)
    for segment in segments:
        segment_updates = {}
        if style_changed:
            segment_updates["image_status"] = "stale" if segment.get("image_path") else "pending"
            if segment.get("prompt_manual"):
                segment_updates["prompt_needs_review"] = 1
            else:
                segment_updates.update({
                    "image_prompt": "",
                    "prompt_status": "pending",
                    "prompt_error": None,
                    "prompt_needs_review": 0,
                })
        if voice_changed and (
            not segment.get("audio_voice_type") or segment.get("audio_voice_type") == old_voice
        ):
            segment_updates["audio_status"] = "stale" if segment.get("audio_path") else "pending"
            if segment.get("audio_voice_type") == old_voice:
                segment_updates["audio_voice_type"] = ""
        if segment_updates:
            mysql_client.update_segment(task_id, segment["segment_index"], segment_updates)

    task_manager.invalidate_task_cache(task_id)
    if style_changed:
        mysql_client.update_task_workflow(
            task_id,
            "planning",
            status=TaskStatus.INTERRUPTED.value,
            current_step="image_prompt_generation",
        )
        task_manager.invalidate_task_cache(task_id)
    refreshed = mysql_client.get_task(task_id) or task_row
    refreshed_segments = mysql_client.get_segments(task_id)
    return {
        "message": "设置已保存",
        "plan_version": next_version,
        "snapshot_key": _plan_fingerprint(refreshed, refreshed_segments),
        "stage": _workspace_stage(refreshed),
        "subtitle_changed": subtitle_changed,
        "generation_options": generation_options,
        "subtitle_options": subtitle_options,
    }


@router.post("/tasks/{task_id}/generate-assets")
async def generate_task_workspace_assets(task_id: str, response: Response, payload: dict = Body(...)):
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    if not segments or any(not str(segment.get("image_prompt") or "").strip() for segment in segments):
        raise HTTPException(status_code=409, detail="分镜提示词尚未全部生成")
    if not bool(task_row.get("voice_confirmed")):
        raise HTTPException(status_code=409, detail="请先确认全片配音音色")
    snapshot_key = payload.get("snapshot_key") or ""
    if snapshot_key != _plan_fingerprint(task_row, segments):
        raise HTTPException(status_code=409, detail="预案已发生变化，请确认最新内容后再生成")
    outcome = task_executor.continue_task(task_id)
    if outcome in {"started", "already_running"}:
        response.status_code = 202 if outcome == "started" else 200
        return {"task_id": task_id, "outcome": outcome, "stage": "generating_assets"}
    raise HTTPException(status_code=409, detail="当前任务不能从预案阶段继续生成")


def _operation_idempotency_key(
    kind: str,
    task_row: dict,
    snapshot_key: str,
    targets: List[dict],
) -> str:
    payload = {
        "kind": kind,
        "task_id": task_row.get("task_id"),
        "snapshot_key": snapshot_key,
        "task_updated_at": str(task_row.get("updated_at") or ""),
        "targets": [
            {
                "segment_index": item.get("segment_index"),
                "asset_type": item.get("asset_type"),
                "mode": item.get("mode"),
                "version": item.get("version"),
                "voice_type": item.get("voice_type"),
                "tts_options": item.get("tts_options"),
                "plan_version": item.get("plan_version"),
            }
            for item in targets
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_retry_targets(
    segments: List[dict],
    scope: str,
    requested_targets: Optional[List[dict]],
) -> List[dict]:
    by_index = {int(item.get("segment_index") or 0): item for item in segments}
    if scope == "failed":
        requested_targets = _workspace_recovery_targets(segments)
    elif scope != "selected":
        raise HTTPException(status_code=400, detail="scope 仅支持 failed 或 selected")
    if not requested_targets:
        raise HTTPException(status_code=409, detail="当前没有需要重试的素材")

    resolved = []
    seen = set()
    for requested in requested_targets:
        try:
            segment_index = int(requested.get("segment_index"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="分镜索引无效")
        asset_type = str(requested.get("asset_type") or "")
        if asset_type not in {"image", "audio"}:
            raise HTTPException(status_code=400, detail="asset_type 仅支持 image 或 audio")
        key = (segment_index, asset_type)
        if key in seen:
            continue
        seen.add(key)
        segment = by_index.get(segment_index)
        if not segment:
            raise HTTPException(status_code=404, detail=f"分镜 {segment_index + 1} 不存在")
        if asset_type == "image" and not str(segment.get("image_prompt") or "").strip():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "needs_prompt",
                    "message": f"分镜 {segment_index + 1} 缺少图片提示词，请先恢复预案",
                },
            )
        has_existing_file = _workspace_file_ready(segment.get(f"{asset_type}_path"))
        ready = segment.get(f"{asset_type}_status") == "completed" and has_existing_file
        if scope == "failed" and ready:
            continue
        resolved_target = {
            "segment_index": segment_index,
            "asset_type": asset_type,
            "mode": "replace" if has_existing_file else "retry",
            "status": "pending",
            "error": None,
            "version": str(segment.get("updated_at") or ""),
        }
        if asset_type == "audio":
            if requested.get("voice_type"):
                resolved_target["voice_type"] = str(requested["voice_type"])
            if isinstance(requested.get("tts_options"), dict):
                resolved_target["tts_options"] = dict(requested["tts_options"])
        resolved.append(resolved_target)
    if not resolved:
        raise HTTPException(status_code=409, detail="当前没有需要重试的素材")
    return sorted(resolved, key=lambda item: (item["segment_index"], item["asset_type"]))


@router.post("/tasks/{task_id}/segments/{segment_index}/regenerate-prompt")
async def regenerate_segment_prompt(
    task_id: str,
    segment_index: int,
    response: Response,
    payload: dict = Body(...),
):
    """Regenerate one image prompt without invoking image or TTS generation."""
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    segment = next(
        (
            item
            for item in segments
            if int(item.get("segment_index") or 0) == int(segment_index)
        ),
        None,
    )
    if not segment:
        raise HTTPException(status_code=404, detail="分镜不存在")
    snapshot_key = str((payload or {}).get("snapshot_key") or "")
    if snapshot_key != _plan_fingerprint(task_row, segments):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": "预案已发生变化，请刷新后再重新生成提示词",
            },
        )
    target = {
        "segment_index": int(segment_index),
        "asset_type": "prompt",
        "mode": "regenerate",
        "status": "pending",
        "error": None,
        "version": str(segment.get("updated_at") or ""),
        "plan_version": int(task_row.get("plan_version") or 0),
        "origin_status": task_row.get("status"),
        "origin_phase": task_row.get("workflow_phase"),
    }
    active = mysql_client.get_active_task_operation(task_id)
    if active:
        active_targets = active.get("targets") or []
        active_target = active_targets[0] if len(active_targets) == 1 else {}
        if (
            active.get("kind") == "regenerate_prompt"
            and active.get("snapshot_key") == snapshot_key
            and active_target.get("asset_type") == "prompt"
            and int(active_target.get("segment_index", -1)) == int(segment_index)
        ):
            response.status_code = 200
            return _workspace_operation_payload(active)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_running",
                "message": "当前已有其他项目操作正在执行",
                "operation_id": active.get("operation_id"),
            },
        )
    if task_runtime.is_running(task_id):
        raise HTTPException(status_code=409, detail="当前已有项目操作正在执行")
    idempotency_key = _operation_idempotency_key(
        "regenerate_prompt", task_row, snapshot_key, [target]
    )
    created = mysql_client.create_task_operation(
        task_id,
        "regenerate_prompt",
        idempotency_key,
        snapshot_key,
        [target],
    )
    if created.get("outcome") == "conflict":
        raise HTTPException(status_code=409, detail="当前已有项目操作正在执行")
    if created.get("outcome") == "error":
        raise HTTPException(status_code=500, detail="创建提示词操作失败")
    operation = created.get("operation") or {}
    if created.get("outcome") == "duplicate":
        response.status_code = 200
        return _workspace_operation_payload(operation)
    outcome = task_executor.regenerate_prompt(
        task_id, operation["operation_id"], target
    )
    if outcome != "started":
        mysql_client.update_task_operation(
            operation["operation_id"], state="failed", error="提示词操作启动失败"
        )
        raise HTTPException(status_code=409, detail="当前项目无法重新生成提示词")
    response.status_code = 202
    return _workspace_operation_payload(
        mysql_client.get_task_operation(operation["operation_id"])
    )


@router.post("/tasks/{task_id}/retry-assets")
async def retry_task_assets(task_id: str, response: Response, payload: dict = Body(...)):
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    snapshot_key = str(payload.get("snapshot_key") or "")
    if snapshot_key != _plan_fingerprint(task_row, segments):
        raise HTTPException(status_code=409, detail="预案已发生变化，请刷新后再重试")
    requested_scope = str(payload.get("scope") or "failed")
    targets = _resolve_retry_targets(
        segments,
        requested_scope,
        payload.get("targets"),
    )
    active = mysql_client.get_active_task_operation(task_id)
    if active:
        requested_keys = {
            (int(item["segment_index"]), item["asset_type"]) for item in targets
        }
        active_keys = {
            (int(item["segment_index"]), item["asset_type"])
            for item in active.get("targets") or []
            if item.get("asset_type") in {"image", "audio"}
        }
        if (
            active.get("kind") == "retry_assets"
            and active.get("snapshot_key") == snapshot_key
            and requested_keys == active_keys
        ):
            response.status_code = 200
            return _workspace_operation_payload(active)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_running",
                "message": "当前已有素材操作正在执行",
                "operation_id": active.get("operation_id") if active else None,
            },
        )
    if task_runtime.is_running(task_id):
        raise HTTPException(status_code=409, detail="当前已有任务操作正在执行")
    idempotency_key = _operation_idempotency_key(
        "retry_assets", task_row, snapshot_key, targets
    )
    created = mysql_client.create_task_operation(
        task_id,
        "retry_assets",
        idempotency_key,
        snapshot_key,
        targets,
    )
    if created.get("outcome") == "conflict":
        raise HTTPException(status_code=409, detail="当前已有素材操作正在执行")
    if created.get("outcome") == "error":
        raise HTTPException(status_code=500, detail="创建素材重试操作失败")
    operation = created.get("operation") or {}
    if created.get("outcome") == "duplicate":
        response.status_code = 200
        return _workspace_operation_payload(operation)
    outcome = task_executor.retry_assets(task_id, operation["operation_id"], targets)
    if outcome != "started":
        mysql_client.update_task_operation(
            operation["operation_id"], state="failed", error="素材重试启动失败"
        )
        raise HTTPException(status_code=409, detail="当前任务无法启动素材重试")
    response.status_code = 202
    return _workspace_operation_payload(
        mysql_client.get_task_operation(operation["operation_id"])
    )


@router.post("/tasks/{task_id}/finalize")
async def finalize_task_workspace(task_id: str, response: Response, payload: dict = Body(...)):
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    snapshot_key = str(payload.get("snapshot_key") or "")
    force_rebuild = bool(payload.get("force"))
    if snapshot_key != _plan_fingerprint(task_row, segments):
        raise HTTPException(status_code=409, detail="预案已发生变化，请刷新后再完成生产")
    health = _workspace_health(task_row, segments)
    if not health["assets_complete"]:
        raise HTTPException(status_code=409, detail="素材尚未齐全，请先修复失败素材")
    if health["draft_ready"] and not force_rebuild:
        response.status_code = 200
        return {"task_id": task_id, "status": "ready", "outcome": "already_ready"}
    active = mysql_client.get_active_task_operation(task_id)
    if active:
        if (
            active.get("kind") == "finalize"
            and active.get("snapshot_key") == snapshot_key
        ):
            response.status_code = 200
            return _workspace_operation_payload(active)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_running",
                "message": "当前已有其他版本的任务操作正在执行",
                "operation_id": active.get("operation_id"),
            },
        )
    if task_runtime.is_running(task_id):
        raise HTTPException(status_code=409, detail="当前已有任务操作正在执行")
    target = [{
        "asset_type": "draft",
        "mode": "rebuild" if force_rebuild else "finalize",
        "status": "pending",
        "version": str(task_row.get("updated_at") or ""),
    }]
    key = _operation_idempotency_key("finalize", task_row, snapshot_key, target)
    created = mysql_client.create_task_operation(
        task_id, "finalize", key, snapshot_key, target
    )
    if created.get("outcome") == "conflict":
        raise HTTPException(status_code=409, detail="当前已有任务操作正在执行")
    if created.get("outcome") == "error":
        raise HTTPException(status_code=500, detail="创建草稿构建操作失败")
    operation = created.get("operation") or {}
    if created.get("outcome") == "duplicate":
        response.status_code = 200
        return _workspace_operation_payload(operation)
    outcome = task_executor.finalize_task(task_id, operation["operation_id"])
    if outcome != "started":
        mysql_client.update_task_operation(
            operation["operation_id"], state="failed", error="草稿构建启动失败"
        )
        raise HTTPException(status_code=409, detail="当前任务无法构建草稿")
    response.status_code = 202
    return _workspace_operation_payload(
        mysql_client.get_task_operation(operation["operation_id"])
    )


@router.post("/tasks/{task_id}/resegment")
async def resegment_task_workspace(task_id: str, payload: dict = Body(...)):
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    _ensure_workspace_mutable(task_row)
    script_text = str(payload.get("script_text") or "").strip()
    if not script_text:
        raise HTTPException(status_code=400, detail="完整文案不能为空")
    segments = TextSegmenter().split(script_text)
    if not segments:
        raise HTTPException(status_code=400, detail="未能从文案中拆出有效分镜")
    rows = [
        {
            "segment_index": index,
            "text": text,
            "image_prompt": "",
            "image_status": "pending",
            "audio_status": "pending",
            "prompt_status": "pending",
        }
        for index, text in enumerate(segments)
    ]
    next_version = mysql_client.replace_plan_segments(
        task_id,
        script_text,
        rows,
        expected_plan_version=payload.get("expected_plan_version"),
    )
    if next_version == -1:
        raise HTTPException(status_code=409, detail="预案已在其他页面更新，请刷新后重试")
    if next_version is None:
        raise HTTPException(status_code=500, detail="重新拆分分镜失败")
    if not mysql_client.update_task_workflow(
        task_id,
        "planning",
        status=TaskStatus.INTERRUPTED.value,
        current_step="image_prompt_generation",
    ):
        raise HTTPException(status_code=500, detail="重新拆分状态保存失败")
    task_manager.invalidate_task_cache(task_id)
    outcome = task_executor.resume_task(task_id)
    return {"message": "已开始重新拆分提示词", "plan_version": next_version, "outcome": outcome}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, response: Response):
    """恢复存在可用检查点的中断任务。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task_row = mysql_client.get_task(task_id) or {}
    segments = mysql_client.get_segments(task_id)
    health = _workspace_health(task_row, segments)
    recovery = _workspace_recovery(
        task_row,
        _workspace_stage(task_row),
        health,
        _workspace_recovery_targets(segments),
        _workspace_stale_targets(segments),
    )
    if recovery.get("mode") in {
        "retry_assets",
        "update_stale_assets",
        "finalize",
        "finalize_failed",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "code": recovery["mode"],
                "message": recovery["description"],
                "recovery": recovery,
            },
        )

    outcome = task_executor.resume_task(task_id)
    if outcome == "started":
        response.status_code = 202
        status = TaskStatus.PROCESSING.value
    elif outcome == "already_running":
        response.status_code = 200
        status = TaskStatus.PROCESSING.value
    elif outcome == "already_completed":
        response.status_code = 200
        status = TaskStatus.COMPLETED.value
    else:
        response.status_code = 409
        status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
    return {"task_id": task_id, "status": status, "outcome": outcome}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, response: Response):
    """Stop future provider dispatch while letting in-flight work checkpoint."""

    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task_row.get("status") == TaskStatus.DELETING.value:
        raise HTTPException(status_code=409, detail="任务正在删除")

    was_running = task_runtime.is_running(task_id)
    if not was_running:
        return {
            "task_id": task_id,
            "status": task_row.get("status"),
            "outcome": "already_stopped",
            "message": "当前没有正在执行的生成操作",
        }

    stopped = await asyncio.to_thread(task_executor.cancel_task, task_id, 30)
    refreshed = mysql_client.get_task(task_id) or task_row
    if not stopped:
        response.status_code = 202
        return {
            "task_id": task_id,
            "status": refreshed.get("status"),
            "outcome": "cancel_requested",
            "message": "已停止新请求，正在等待已开始的生成完成并保存",
        }
    return {
        "task_id": task_id,
        "status": refreshed.get("status"),
        "outcome": "cancelled",
        "message": "生成已停止，已完成的内容已保存",
    }


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    response: Response,
    delete_files: bool = Query(False, description="同时删除本地任务文件"),
):
    """删除任务记录，并可安全删除本地任务文件。"""
    outcome = task_manager.request_delete(task_id, delete_files=delete_files)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="任务不存在")
    response.status_code = 202 if outcome == "deleting" else 200
    result = {
        "task_id": task_id,
        "status": outcome,
        "outcome": outcome,
        "message": "任务已删除" if outcome == "deleted" else "任务正在停止并删除",
    }
    report = task_manager.get_deletion_report(task_id)
    if report is not None:
        result["deletion_report"] = asdict(report)
    return result


@router.get("/tasks/{task_id}/download")
async def download_task(
    task_id: str,
    extract_path: Optional[str] = Query(None, description="用户解压路径，如 D:\\JianyingPro Drafts；留空则按浏览器下载原草稿包"),
    target_os: str = Query("windows", description="目标系统：windows/mac"),
):
    """
    通过浏览器下载任务生成的草稿包

    - **task_id**: 任务ID
    - **extract_path**: 用户解压目标路径（可选），填写时用于将草稿内素材路径转为绝对路径

    返回浏览器可下载的草稿包，包内包含以草稿名命名的根文件夹。
    """
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail=f"任务未完成，当前状态: {task.status}")

    if not task.result or not task.result.draft_path:
        raise HTTPException(status_code=404, detail="草稿路径不存在")

    draft_path = Path(task.result.draft_path)
    zip_path = draft_path / f"{draft_path.name}.zip"

    if not zip_path.exists():
        try:
            zip_path = _pack_draft_zip(task)
        except Exception as e:
            safe = classify_exception(e, provider="local_storage")
            logger.error("准备浏览器下载草稿失败: %s", safe.safe_message)
            raise HTTPException(status_code=500, detail=safe.safe_message)

    draft_name = draft_path.name
    target_os = "mac" if target_os == "mac" else "windows"
    normalized_extract_path = ""
    if extract_path:
        valid_path, normalized_extract_path, path_issues = validate_extract_path(extract_path, target_os)
        if not valid_path:
            raise HTTPException(status_code=400, detail="；".join(path_issues))

    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(zip_path, "r") as src_zip, \
             zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst_zip:

            for item in src_zip.infolist():
                data = src_zip.read(item.filename)

                if normalized_extract_path and item.filename == "draft_content.json":
                    draft_json = json.loads(data.decode("utf-8"))
                    draft_json = apply_extract_path(draft_json, normalized_extract_path, draft_name, target_os=target_os)
                    draft_json = apply_content_info(draft_json, draft_name, target_os=target_os)
                    data = json.dumps(draft_json, ensure_ascii=False, indent=2).encode("utf-8")
                elif normalized_extract_path and item.filename == "draft_meta_info.json":
                    meta_json = json.loads(data.decode("utf-8"))
                    meta_json = apply_meta_info(meta_json, normalized_extract_path, draft_name, target_os=target_os)
                    data = json.dumps(meta_json, ensure_ascii=False, indent=2).encode("utf-8")

                dst_zip.writestr(f"{draft_name}/{item.filename}", data)

            if normalized_extract_path:
                if target_os == "mac":
                    dst_zip.writestr(f"{draft_name}/fix_paths.command", generate_fix_sh())
                else:
                    dst_zip.writestr(f"{draft_name}/fix_paths.bat", generate_fix_bat())

        buf.seek(0)
    except Exception as e:
        safe = classify_exception(e, provider="local_storage")
        logger.error("生成下载 ZIP 失败: %s", safe.safe_message)
        raise HTTPException(status_code=500, detail=safe.safe_message)

    if normalized_extract_path:
        task_manager.update_extract_path(task_id, normalized_extract_path)

    filename = f"{task.theme[:20]}.zip"
    encoded_filename = quote(filename)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        },
    )


@router.get("/tasks/{task_id}/download-mp4")
async def download_video(task_id: str):
    """
    下载任务生成的视频文件

    - **task_id**: 任务ID

    返回 MP4 视频文件供下载
    """
    task = task_manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail=f"任务未完成，当前状态: {task.status}")

    if not task.result or not task.result.draft_path:
        raise HTTPException(status_code=404, detail="草稿路径不存在")

    segments = mysql_client.get_segments(task_id)
    _ensure_legacy_render_manifest(task, segments)
    preview = _preview_state(task, segments)
    if not preview["valid"]:
        raise HTTPException(status_code=409, detail="当前 MP4 未生成或已过期，请重新生成")

    video_path = Path(preview["manifest"]["video_path"])

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="视频文件不存在")

    # 返回文件下载
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"{task.theme[:20]}.mp4"
    )


@router.get("/tasks", response_model=List[dict])
async def list_tasks(
    request: Request,
    status: Optional[str] = Query(None, description="任务状态筛选：pending/processing/completed/failed"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量")
):
    """
    获取任务列表

    - **status**: 任务状态筛选（可选）
    - **limit**: 每页数量（默认 20，最大 100）
    - **offset**: 偏移量（默认 0）
    """
    tasks = task_manager.list_tasks(status=status, limit=limit, offset=offset)
    for task in tasks:
        task["cover_image_url"] = (
            _normalize_local_media_url(task.get("cover_image_url"), request)
            or _local_media_url_from_path(task.get("cover_image_path"), request)
        )
        task.pop("cover_image_path", None)
    return tasks


@router.get("/tasks/{task_id}/segments")
async def get_segments(task_id: str, request: Request):
    """
    获取任务的段落列表

    - **task_id**: 任务ID
    """
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    segments = mysql_client.get_segments(task_id)

    # Reuse the workspace serializer so legacy callers receive the same safe
    # structured errors and storage-warning semantics.
    return [_workspace_segment_payload(seg, request) for seg in segments]


@router.get("/tasks/{task_id}/render-config")
async def get_task_render_config(task_id: str):
    """获取指定任务的渲染参数、分镜动画参数和预览时长。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    segments = mysql_client.get_segments(task_id)
    from src.export.ffmpeg_exporter import FFmpegExporter, build_animation_params

    config = FFmpegExporter.get_render_config(
        canvas=_task_canvas(task),
        subtitle_options=_task_subtitle_options(mysql_client.get_task(task_id)),
    )
    config["ratio"] = _task_ratio(task)
    config["animation_seed"] = _task_animation_seed(task_id)
    config["animations"] = build_animation_params(len(segments), config["animation_seed"])
    config["segment_durations"] = [_segment_duration_seconds(seg) for seg in segments]
    return config


@router.post("/tasks/{task_id}/preview-render")
async def render_task_preview(
    task_id: str,
    segment_index: Optional[int] = Query(None, ge=0, description="只渲染指定分镜；不传则渲染全片")
):
    """使用最终 FFmpeg 链路生成精准预览 MP4。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task.result or not task.result.draft_path:
        raise HTTPException(status_code=404, detail="草稿路径不存在")

    segments = mysql_client.get_segments(task_id)
    if not segments:
        raise HTTPException(status_code=404, detail="段落数据不存在")
    if segment_index is not None and segment_index >= len(segments):
        raise HTTPException(status_code=404, detail="段落不存在")

    from src.export.ffmpeg_exporter import FFmpegExporter, build_animation_params
    from src.utils.local_uploader import LocalUploader

    seed = _task_animation_seed(task_id)
    all_animation_params = build_animation_params(len(segments), seed)
    if segment_index is None:
        selected = segments
        animation_params = all_animation_params
        ratio_slug = _task_ratio(task).replace(":", "x")
        filename = f"preview_full_{ratio_slug}_{int(time.time())}.mp4"
        mode = "full"
    else:
        selected = [segments[segment_index]]
        animation_params = [all_animation_params[segment_index]]
        ratio_slug = _task_ratio(task).replace(":", "x")
        filename = f"preview_seg_{segment_index:03d}_{ratio_slug}_{int(time.time())}.mp4"
        mode = "segment"

    segment_texts = [seg.get("text") or "" for seg in selected]
    media_paths = [seg.get("image_path") for seg in selected]
    voiceover_files = [seg.get("audio_path") for seg in selected]

    missing = [path for path in media_paths if not path or not Path(path).exists()]
    if missing:
        raise HTTPException(status_code=404, detail="分镜图片文件不存在，无法生成精准预览")

    preview_dir = Path(task.result.draft_path) / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    video_path = preview_dir / filename

    exporter = FFmpegExporter(
        canvas=_task_canvas(task),
        subtitle_options=_task_subtitle_options(mysql_client.get_task(task_id)),
    )
    exporter.export(
        segments=segment_texts,
        media_paths=media_paths,
        voiceover_files=voiceover_files,
        output_path=str(video_path),
        animation_seed=seed,
        animation_params=animation_params,
    )

    preview_url = LocalUploader().upload(str(video_path), f"{task_id}/previews/{filename}")
    manifest = None
    if segment_index is None:
        manifest = _write_preview_manifest(task, video_path, preview_url, segments)
    return {
        "message": "精准预览生成成功",
        "mode": mode,
        "preview_url": preview_url,
        "video_path": str(video_path),
        "manifest": manifest,
    }


@router.get("/tasks/{task_id}/export-state")
async def get_export_state(task_id: str, request: Request):
    """获取导出页状态：预览、MP4、当前分镜素材包与剪映草稿。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    segments = mysql_client.get_segments(task_id)
    ratio = _task_ratio(task)
    canvas = _task_canvas(task)
    _ensure_legacy_render_manifest(task, segments)
    preview = _preview_state(task, segments)
    has_draft_path = bool(task.result and task.result.draft_path)
    draft_zip = _draft_zip_path(task) if has_draft_path else None
    video_path = _official_video_path(task) if has_draft_path else None
    materials = material_package_state(
        task_id,
        getattr(task, "name", None) or getattr(task, "theme", None) or task_id,
        segments,
        Config.BASE_DIR,
    )
    materials["download_url"] = (
        f"{request.url_for('download_material_package', task_id=task_id)}"
        f"?snapshot_key={quote(materials['snapshot_key'])}"
        if materials["package_ready"] else None
    )

    jobs = _latest_export_jobs(task_id)
    latest_mp4_job = next((job for job in jobs if job.get("target") == "mp4"), None)
    if latest_mp4_job and latest_mp4_job.get("status") in {"pending", "processing"}:
        render_status = latest_mp4_job["status"]
    elif preview["valid"]:
        render_status = "ready"
    elif preview["reason"] in {"stale", "ratio_mismatch", "file_missing"}:
        render_status = "stale"
    elif latest_mp4_job and latest_mp4_job.get("status") == "failed":
        render_status = "failed"
    elif latest_mp4_job and latest_mp4_job.get("status") == "cancelled":
        render_status = "cancelled"
    else:
        render_status = "missing"

    manifest = dict(preview["manifest"] or {})
    if manifest.get("preview_url"):
        manifest["preview_url"] = _normalize_local_media_url(manifest["preview_url"], request)
        manifest["video_url"] = manifest["preview_url"]

    return {
        "task_id": task_id,
        "status": task.status,
        "ratio": ratio,
        "canvas": canvas,
        "preview": {
            "exists": preview["exists"],
            "valid": preview["valid"],
            "reason": preview["reason"],
            "status": render_status,
            "snapshot_key": preview["fingerprint"],
            "manifest": manifest or None,
        },
        "render": {
            "status": render_status,
            "snapshot_key": preview["fingerprint"],
            "video_url": manifest.get("preview_url"),
            "created_at": manifest.get("created_at"),
            "error": latest_mp4_job.get("error") if latest_mp4_job else None,
            "error_code": latest_mp4_job.get("error_code") if latest_mp4_job else None,
            "error_meta": latest_mp4_job.get("error_meta") if latest_mp4_job else None,
        },
        "outputs": {
            "mp4": {
                "available": preview["valid"],
                "stale": preview["exists"] and not preview["valid"],
                "path": manifest.get("video_path"),
                "url": manifest.get("preview_url"),
            },
            "draft": {
                "available": bool(draft_zip and draft_zip.exists()) or bool(task.result and task.result.draft_url),
                "path": str(draft_zip) if draft_zip else None,
                "url": _normalize_local_media_url(task.result.draft_url, request) if task.result else None,
            },
            "materials": materials,
        },
        "jobs": jobs,
    }


@router.post("/tasks/{task_id}/draft-folder/select")
async def select_local_draft_folder(task_id: str):
    """在本机弹出目录选择器，返回真实剪映草稿根目录。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    folder = _pick_local_folder()
    if not folder:
        raise HTTPException(status_code=400, detail="未选择文件夹")
    return _validate_local_draft_root(folder, _server_target_os())


@router.post("/tasks/{task_id}/draft-folder/validate")
async def validate_local_draft_folder(task_id: str, payload: dict = Body(...)):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _validate_local_draft_root(
        (payload or {}).get("draft_root") or (payload or {}).get("path") or "",
        (payload or {}).get("target_os") or _server_target_os(),
    )


@router.post("/tasks/{task_id}/exports")
async def create_export(task_id: str, payload: dict = Body(...)):
    """创建异步导出任务。支持 MP4、剪映草稿、本地写入和分镜素材包。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    target = (payload or {}).get("target")
    use_preview = bool((payload or {}).get("use_preview", True))
    if target not in {"mp4", "draft", "draft_local", "materials"}:
        raise HTTPException(status_code=400, detail="target 必须是 mp4、draft、draft_local 或 materials")
    if target != "materials" and (not task.result or not task.result.draft_path):
        raise HTTPException(status_code=404, detail="草稿路径不存在")

    if target == "materials":
        segments = mysql_client.get_segments(task_id)
        state = material_package_state(
            task_id,
            getattr(task, "name", None) or getattr(task, "theme", None) or task_id,
            segments,
            Config.BASE_DIR,
        )
        if not state["available"]:
            raise HTTPException(status_code=409, detail="暂无可打包素材")

    if target == "draft_local":
        root_check = _validate_local_draft_root(
            (payload or {}).get("draft_root") or (payload or {}).get("extract_path") or "",
            (payload or {}).get("target_os") or _server_target_os(),
        )
        if not root_check["valid"]:
            raise HTTPException(status_code=400, detail="；".join(root_check["issues"] or ["剪映草稿目录不可用"]))
        payload = {**(payload or {}), "draft_root": root_check["path"], "target_os": root_check["target_os"]}

    job, created = _create_or_reuse_export_job(task_id, target, payload)
    if created:
        thread = Thread(target=_run_export_job, args=(job["job_id"], target, use_preview, payload), daemon=True)
        thread.start()
    return job


@router.get("/tasks/{task_id}/exports/{job_id}")
async def get_export_job(task_id: str, job_id: str):
    job = _export_job_snapshot(job_id)
    if not job or job.get("task_id") != task_id:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    return job


@router.post("/tasks/{task_id}/exports/{job_id}/cancel")
async def cancel_export_job(task_id: str, job_id: str):
    """Request cooperative cancellation without discarding prior valid output."""
    with EXPORT_JOBS_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job or job.get("task_id") != task_id:
            raise HTTPException(status_code=404, detail="导出任务不存在")
        if job.get("status") not in {"pending", "processing"}:
            return dict(job)
        job["cancel_requested"] = True
        job["message"] = "正在取消；已开始的片段会安全收尾"
        job["updated_at"] = datetime.now().isoformat()
        return dict(job)


@router.get("/tasks/{task_id}/download-materials", name="download_material_package")
async def download_material_package(
    task_id: str,
    snapshot_key: str = Query(..., min_length=16, description="素材包任务快照"),
):
    """下载与当前分镜快照一致的正式素材包。"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    project_name = getattr(task, "name", None) or getattr(task, "theme", None) or task_id
    state = material_package_state(task_id, project_name, segments, Config.BASE_DIR)
    if snapshot_key != state["snapshot_key"]:
        raise HTTPException(status_code=409, detail="素材已变化，请重新打包")
    package_path = current_material_package(
        task_id,
        project_name,
        segments,
        Config.BASE_DIR,
        snapshot_key,
    )
    if not package_path:
        raise HTTPException(status_code=404, detail="素材包尚未生成或已经失效")
    return FileResponse(
        path=str(package_path),
        media_type="application/zip",
        filename=package_path.name,
    )


@router.get("/tasks/{task_id}/assets")
async def list_task_assets(
    task_id: str,
    request: Request,
    type: Optional[str] = Query(None, description="image/audio/subtitle/upload"),
    segment_index: Optional[int] = Query(None),
):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    _ensure_task_assets(task, segments)
    if type and type not in {"image", "audio", "subtitle", "upload"}:
        raise HTTPException(status_code=400, detail="type 必须是 image/audio/subtitle/upload")
    assets = mysql_client.list_task_assets(task_id, asset_type=type, segment_index=segment_index)
    return [_asset_to_response(asset, request) for asset in assets]


@router.get("/tasks/{task_id}/asset-library")
async def get_task_asset_library(
    task_id: str,
    request: Request,
    asset_type: Optional[str] = Query(None, description="image/audio/subtitle"),
    scope: str = Query("project", description="project/segment"),
    segment_index: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if asset_type and asset_type not in {"image", "audio", "subtitle"}:
        raise HTTPException(status_code=400, detail="asset_type 必须是 image/audio/subtitle")
    if scope not in {"project", "segment"}:
        raise HTTPException(status_code=400, detail="scope 必须是 project/segment")
    if scope == "segment" and segment_index is None:
        raise HTTPException(status_code=400, detail="分镜范围必须提供 segment_index")
    segments = mysql_client.get_segments(task_id)
    _ensure_task_assets(task, segments)
    segments = mysql_client.get_segments(task_id)
    selected = {
        ("image", segment.get("selected_image_asset_id")): segment.get("segment_index")
        for segment in segments if segment.get("selected_image_asset_id")
    }
    selected.update({
        ("audio", segment.get("selected_audio_asset_id")): segment.get("segment_index")
        for segment in segments if segment.get("selected_audio_asset_id")
    })
    assets = mysql_client.list_task_assets(
        task_id,
        asset_type=asset_type,
        segment_index=segment_index if scope == "segment" else None,
    )
    total = len(assets)
    start = (page - 1) * page_size
    items = []
    for asset in assets[start:start + page_size]:
        item = _asset_to_response(asset, request)
        item["is_selected"] = (asset.get("asset_type"), asset.get("asset_id")) in selected
        item["selected_by_segment"] = selected.get((asset.get("asset_type"), asset.get("asset_id")))
        items.append(item)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.post("/tasks/{task_id}/segments/{segment_index}/select-asset")
async def select_segment_asset(
    task_id: str,
    segment_index: int,
    request: Request,
    payload: dict = Body(...),
):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task_row = mysql_client.get_task(task_id) or {}
    _ensure_workspace_mutable(task_row)
    if mysql_client.get_active_task_operation(task_id) or task_runtime.is_running(task_id):
        raise HTTPException(status_code=409, detail="当前已有项目操作正在执行")
    segments = mysql_client.get_segments(task_id)
    segment = next(
        (item for item in segments if int(item.get("segment_index") or 0) == int(segment_index)),
        None,
    )
    if not segment:
        raise HTTPException(status_code=404, detail="段落不存在")
    snapshot_key = str(payload.get("snapshot_key") or "")
    if snapshot_key and snapshot_key != _plan_fingerprint(task_row, segments):
        raise HTTPException(status_code=409, detail={"code": "stale_snapshot", "message": "预案已更新，请刷新素材列表"})
    _ensure_task_assets(task, segments)
    asset_id = str(payload.get("asset_id") or "")
    asset_type = str(payload.get("asset_type") or "")
    if asset_type not in {"image", "audio"}:
        raise HTTPException(status_code=400, detail="asset_type 必须是 image/audio")
    asset = mysql_client.get_task_asset(task_id, asset_id)
    if not asset or asset.get("asset_type") != asset_type:
        raise HTTPException(status_code=404, detail="素材不存在")
    if _workspace_resolve_file(asset.get("path")) is None:
        raise HTTPException(status_code=404, detail="素材文件不存在")
    confirm_mismatch = bool(payload.get("confirm_text_mismatch"))
    mismatch = False
    if asset_type == "audio":
        source_text = normalize_subtitle_text(asset.get("text") or "").strip()
        target_text = normalize_subtitle_text(segment.get("text") or "").strip()
        mismatch = bool(source_text and target_text and source_text != target_text)
        if mismatch and not confirm_mismatch:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "audio_text_mismatch",
                    "message": "这段配音的原文与当前分镜不一致，确认后才能复用",
                    "source_text": asset.get("text") or "",
                    "target_text": segment.get("text") or "",
                },
            )
    selected_asset = asset
    source_segment_index = asset.get("segment_index")
    if source_segment_index is None or int(source_segment_index) != int(segment_index):
        selected_asset = _record_asset(
            task_id,
            asset_type,
            "selected",
            path=asset.get("path"),
            url=asset.get("url"),
            segment_index=segment_index,
            prompt=asset.get("prompt"),
            text=asset.get("text"),
            voice_type=asset.get("voice_type"),
            metadata={
                "source_segment_index": source_segment_index,
                "source_asset_id": asset.get("asset_id"),
            },
            origin_asset_id=asset.get("asset_id"),
        )
        if not selected_asset:
            raise HTTPException(status_code=500, detail="素材复用版本保存失败")
    success = mysql_client.select_segment_asset(
        task_id,
        segment_index,
        selected_asset,
        asset_type,
        confirm_text_mismatch=mismatch and confirm_mismatch,
    )
    if not success:
        raise HTTPException(status_code=500, detail="切换素材失败")
    _invalidate_review_first_draft(task_id, task_row)
    invalidate = getattr(task_manager, "invalidate_task_cache", None)
    if callable(invalidate):
        invalidate(task_id)
    refreshed = mysql_client.get_segments(task_id)
    try:
        file_url = _normalize_local_media_url(selected_asset.get("url"), request)
    except RuntimeError:
        file_url = selected_asset.get("url")
    selected_asset_id = selected_asset.get("asset_id")
    if not file_url:
        try:
            file_url = str(request.url_for(
                "download_task_asset_file", task_id=task_id, asset_id=selected_asset_id
            ))
        except RuntimeError:
            file_url = f"/tasks/{task_id}/assets/{selected_asset_id}/file"
    response = {
        "message": "素材已切换",
        "asset_id": selected_asset_id,
        "origin_asset_id": selected_asset.get("origin_asset_id"),
        "asset_type": asset_type,
        "segment_index": segment_index,
        "url": file_url,
        "audio_text_mismatch": mismatch,
        "snapshot_key": _plan_fingerprint(task_row, refreshed),
    }
    if asset_type == "image":
        response.update({
            "previous_image_path": segment.get("image_path"),
            "image_path": selected_asset.get("path"),
        })
    else:
        response.update({
            "previous_audio_path": segment.get("audio_path"),
            "audio_path": selected_asset.get("path"),
        })
    return response


@router.get("/tasks/{task_id}/subtitle.srt")
async def download_task_subtitle(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task.result or not task.result.draft_path:
        raise HTTPException(status_code=404, detail="草稿路径不存在")
    segments = mysql_client.get_segments(task_id)
    srt_path = _write_task_srt(task, segments)
    _record_asset(
        task_id,
        "subtitle",
        "subtitle",
        path=str(srt_path),
        label="项目字幕 SRT",
        text="\n".join(normalize_subtitle_text(seg.get("text") or "") for seg in segments),
    )
    return FileResponse(
        path=str(srt_path),
        media_type="application/x-subrip",
        filename=f"{Path(task.result.draft_path).name}.srt",
    )


@router.get("/tasks/{task_id}/assets/download")
async def download_task_assets(
    task_id: str,
    type: str = Query("all", description="all/image/audio/subtitle/upload"),
):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    _ensure_task_assets(task, segments)
    if type not in {"all", "image", "audio", "subtitle", "upload"}:
        raise HTTPException(status_code=400, detail="type 必须是 all/image/audio/subtitle/upload")

    assets = mysql_client.list_task_assets(task_id, None if type == "all" else type)
    if not assets:
        raise HTTPException(status_code=404, detail="没有可下载的素材")

    folder_map = {"image": "images", "audio": "audio", "subtitle": "subtitles"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        added = 0
        used_names = set()
        used_paths = set()
        for asset in assets:
            path = _workspace_resolve_file(asset.get("path"))
            if path is None:
                continue
            path_key = str(path.resolve())
            if path_key in used_paths:
                continue
            used_paths.add(path_key)
            folder = folder_map.get(asset.get("asset_type"), "assets")
            prefix = "uploads" if asset.get("source") == "upload" else folder
            name = f"{asset.get('segment_index') + 1:02d}_" if asset.get("segment_index") is not None else ""
            arcname = f"{prefix}/{name}{path.name}"
            if arcname in used_names:
                arcname = f"{prefix}/{added + 1:03d}_{name}{path.name}"
            used_names.add(arcname)
            zf.write(path, arcname)
            added += 1
        if added == 0:
            raise HTTPException(status_code=404, detail="素材文件不存在")

    buf.seek(0)
    filename = f"{Path(task.result.draft_path).name}_assets.zip" if task.result else f"{task_id}_assets.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/tasks/{task_id}/assets/{asset_id}/file")
async def download_task_asset_file(task_id: str, asset_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    asset = mysql_client.get_task_asset(task_id, asset_id)
    if not asset:
        segments = mysql_client.get_segments(task_id)
        _ensure_task_assets(task, segments)
        asset = mysql_client.get_task_asset(task_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="素材不存在")
    path = _workspace_resolve_file(asset.get("path"))
    if path is None:
        raise HTTPException(status_code=404, detail="素材文件不存在")
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".srt": "application/x-subrip",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path=str(path), media_type=media_type, filename=path.name)


@router.post("/tasks/{task_id}/segments/{segment_index}/select-image")
async def select_segment_image(task_id: str, segment_index: int, request: Request, payload: dict = Body(...)):
    compatibility_payload = dict(payload or {})
    compatibility_payload["asset_type"] = "image"
    return await select_segment_asset(
        task_id,
        segment_index,
        request,
        compatibility_payload,
    )


@router.put("/tasks/{task_id}/segments/{segment_index}")
async def update_segment(
    task_id: str,
    segment_index: int,
    text: Optional[str] = Body(None),
    image_prompt: Optional[str] = Body(None),
    image_path: Optional[str] = Body(None),
    image_url: Optional[str] = Body(None),
    audio_url: Optional[str] = Body(None),
    audio_voice_type: Optional[str] = Body(None),
    audio_tts_options: Optional[dict] = Body(None),
    expected_plan_version: Optional[int] = Body(None),
):
    """
    更新段落内容

    - **task_id**: 任务ID
    - **segment_index**: 段落索引
    - **text**: 新文案（可选）
    - **image_url**: 新图片URL（可选）
    - **audio_url**: 新音频URL（可选）
    """
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    _ensure_workspace_mutable(mysql_client.get_task(task_id) or {})

    segments = mysql_client.get_segments(task_id)
    current = next((segment for segment in segments if segment.get("segment_index") == segment_index), None)
    if not current:
        raise HTTPException(status_code=404, detail="段落不存在")

    updates = {}
    if text is not None:
        updates['text'] = text
        if text != current.get("text"):
            updates['audio_status'] = 'stale' if current.get('audio_path') else 'pending'
            updates['audio_mismatch_confirmed'] = 0
            updates['prompt_needs_review'] = 1
    if image_prompt is not None:
        updates['image_prompt'] = image_prompt
        if image_prompt != (current.get("image_prompt") or ""):
            updates['image_status'] = 'stale' if current.get('image_path') else 'pending'
            updates['prompt_status'] = 'completed'
            updates['prompt_error'] = None
            updates['prompt_manual'] = 1
            updates['prompt_needs_review'] = 0
    if image_path is not None:
        updates['image_path'] = image_path
    if image_url is not None:
        updates['image_url'] = image_url
    if audio_url is not None:
        updates['audio_url'] = audio_url
    if audio_voice_type is not None:
        updates['audio_voice_type'] = audio_voice_type
        if audio_voice_type != (current.get('audio_voice_type') or task.voice_type):
            updates['audio_status'] = 'stale' if current.get('audio_path') else 'pending'
    if audio_tts_options is not None:
        updates['audio_tts_options_json'] = json.dumps(audio_tts_options, ensure_ascii=False)
        updates['audio_status'] = 'stale' if current.get('audio_path') else 'pending'

    if not updates:
        raise HTTPException(status_code=400, detail="至少需要提供一个更新字段")

    next_version = mysql_client.update_segment_plan(
        task_id,
        segment_index,
        updates,
        expected_plan_version=expected_plan_version,
    )
    if next_version == -1:
        raise HTTPException(status_code=409, detail="预案已在其他页面更新，请刷新后重试")
    if next_version is None:
        raise HTTPException(status_code=500, detail="更新段落失败")
    task_manager.invalidate_task_cache(task_id)
    refreshed = mysql_client.get_task(task_id) or {}
    refreshed_segments = mysql_client.get_segments(task_id)
    return {
        "message": "更新成功",
        "plan_version": next_version,
        "snapshot_key": _plan_fingerprint(refreshed, refreshed_segments),
    }


@router.post(
    "/tasks/{task_id}/segments/{segment_index}/regenerate-image",
    deprecated=True,
)
async def regenerate_image(task_id: str, segment_index: int, response: Response):
    """
    重新生成段落图片

    - **task_id**: 任务ID
    - **segment_index**: 段落索引
    """
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = (
        '299 - "regenerate-image is deprecated; use retry-assets with an exact target"'
    )
    logger.warning("[%s] 调用已废弃 regenerate-image 兼容入口", task_id)
    task_row = mysql_client.get_task(task_id)
    if not task_row:
        raise HTTPException(status_code=404, detail="任务不存在")
    segments = mysql_client.get_segments(task_id)
    return await retry_task_assets(
        task_id,
        response,
        {
            "snapshot_key": _plan_fingerprint(task_row, segments),
            "scope": "selected",
            "targets": [
                {"segment_index": segment_index, "asset_type": "image"}
            ],
        },
    )

@router.post(
    "/tasks/{task_id}/segments/{segment_index}/regenerate-audio",
    deprecated=True,
)
async def regenerate_audio(
    task_id: str,
    segment_index: int,
    response: Response,
    payload: Optional[RegenerateAudioRequest] = Body(None),
    voice_type: Optional[str] = Query(None),
):
    """
    重新生成段落音频

    - **task_id**: 任务ID
    - **segment_index**: 段落索引
    - **voice_type**: TTS 音色 ID（可选，如果不提供则使用任务创建时的音色）
    """
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = (
        '299 - "regenerate-audio is deprecated; use retry-assets with an exact target"'
    )
    logger.warning("[%s] 调用已废弃 regenerate-audio 兼容入口", task_id)
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task_row = mysql_client.get_task(task_id) or {}
    segments = mysql_client.get_segments(task_id)
    segment = next(
        (item for item in segments if item.get("segment_index") == segment_index),
        None,
    )
    if not segment:
        raise HTTPException(status_code=404, detail="段落不存在")

    body_voice = payload.voice_type if payload else None
    requested_voice = body_voice or voice_type
    if requested_voice:
        effective_voice = _resolve_new_task_voice(requested_voice)
    else:
        effective_voice = segment.get("audio_voice_type") or task.voice_type
    if not effective_voice:
        effective_voice = _resolve_new_task_voice(None)
    inherited_options = (
        _parse_options_json(segment.get("audio_tts_options_json"))
        or dict(getattr(task, "tts_options", {}) or {})
    )
    body_options = _model_dict(payload.tts_options, exclude_none=True) if payload else {}
    effective_options = _snapshot_tts_options(
        effective_voice,
        body_options or inherited_options,
    )
    return await retry_task_assets(
        task_id,
        response,
        {
            "snapshot_key": _plan_fingerprint(task_row, segments),
            "scope": "selected",
            "targets": [
                {
                    "segment_index": segment_index,
                    "asset_type": "audio",
                    "voice_type": effective_voice,
                    "tts_options": effective_options,
                }
            ],
        },
    )

@router.post("/tasks/{task_id}/segments/{segment_index}/upload-image")
async def upload_image(task_id: str, segment_index: int, file: UploadFile = File(...)):
    """
    上传自定义图片

    - **task_id**: 任务ID
    - **segment_index**: 段落索引
    - **file**: 图片文件
    """
    from src.utils.local_uploader import LocalUploader
    from pathlib import Path
    import shutil

    logger.info(f"[{task_id}] ========== 开始上传自定义图片 ==========")
    logger.info(f"[{task_id}] 段落索引: {segment_index}")
    logger.info(f"[{task_id}] 文件名: {file.filename}")

    task = task_manager.get_task(task_id)
    if not task:
        logger.error(f"[{task_id}] 任务不存在")
        raise HTTPException(status_code=404, detail="任务不存在")

    task_row = mysql_client.get_task(task_id) or {}
    _ensure_workspace_mutable(task_row)
    if mysql_client.get_active_task_operation(task_id) or task_runtime.is_running(task_id):
        raise HTTPException(status_code=409, detail="当前已有项目操作正在执行")
    segments = mysql_client.get_segments(task_id)
    segment = next(
        (item for item in segments if int(item.get("segment_index") or 0) == int(segment_index)),
        None,
    )
    if not segment:
        logger.error(f"[{task_id}] 分镜不存在: {segment_index}")
        raise HTTPException(status_code=404, detail="段落不存在")

    # 验证文件类型
    allowed_types = ["image/jpeg", "image/jpg", "image/png", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="只支持 JPG、PNG、WEBP 格式的图片")

    # 保存到本地临时文件
    draft_path = _workspace_output_dir(task, segments)
    images_dir = draft_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    import time
    timestamp = int(time.time())
    file_ext = Path(file.filename).suffix or ".jpg"
    local_filename = f"seg_{segment_index:03d}_upload_{timestamp}{file_ext}"
    local_path = images_dir / local_filename

    logger.info(f"[{task_id}] 保存到本地: {local_path}")
    with open(local_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # 保存到本地媒体目录
    image_url = None
    storage_warning = None
    try:
        local_uploader = LocalUploader()
        storage_path = f"{task_id}/images/{local_filename}"
        logger.info(f"[{task_id}] 本地媒体路径: {storage_path}")
        image_url = local_uploader.upload(str(local_path), storage_path)
        logger.info(f"[{task_id}] 图片保存成功: {image_url}")
    except Exception as e:
        storage_warning = classify_exception(e, provider="local_storage")
        if storage_warning.code is not ErrorCode.DISK:
            storage_warning = make_safe_error(
                ErrorCode.DISK, provider="local_storage"
            )
        logger.error(
            "[%s] 图片本地归档失败: %s",
            task_id,
            storage_warning.safe_message,
        )

    # 先保留上传版本，再切换当前分镜；切换失败时历史版本仍可找回。
    logger.info(f"[{task_id}] 更新数据库...")
    uploaded_asset = _record_asset(
        task_id,
        "image",
        "upload",
        path=str(local_path),
        url=image_url,
        segment_index=segment_index,
        text=segment.get("text"),
    )
    if not uploaded_asset:
        raise HTTPException(status_code=500, detail="上传图片版本保存失败")
    if not mysql_client.select_segment_asset(
        task_id,
        segment_index,
        uploaded_asset,
        "image",
    ):
        raise HTTPException(
            status_code=500,
            detail="图片已保存到历史版本，但切换当前分镜失败",
        )
    if storage_warning:
        mysql_client.update_segment(task_id, segment_index, {
            'image_error': storage_warning.safe_message,
            'image_error_code': storage_warning.code.value,
            'image_error_meta': storage_warning.metadata(),
        })
    _invalidate_review_first_draft(task_id, task_row)
    logger.info(f"[{task_id}] 数据库更新完成")

    logger.info(f"[{task_id}] ========== 图片上传完成 ==========")
    updated_task = mysql_client.get_task(task_id) or task_row
    updated_segments = mysql_client.get_segments(task_id)
    return {
        "message": "图片上传成功",
        "asset_id": uploaded_asset.get("asset_id"),
        "image_path": str(local_path),
        "image_url": image_url,
        "previous_image_path": segment.get("image_path"),
        "previous_image_url": segment.get("image_url"),
        "snapshot_key": _plan_fingerprint(updated_task, updated_segments),
    }


@router.post("/tasks/{task_id}/rebuild", deprecated=True)
async def rebuild_draft(task_id: str, response: Response):
    """
    兼容旧客户端：根据当前分镜重新构建草稿。

    该入口不再隐式生成 MP4；需要 MP4 时应在 finalize 后创建导出作业。

    - **task_id**: 任务ID
    """
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = (
        '299 - "rebuild is deprecated; use finalize, then create an export job when MP4 is needed"'
    )
    logger.warning("[%s] 调用已废弃 rebuild 兼容入口", task_id)
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    segments = mysql_client.get_segments(task_id)
    if not segments:
        raise HTTPException(status_code=404, detail="段落数据不存在")

    task_row = mysql_client.get_task(task_id) or {}
    return await finalize_task_workspace(
        task_id,
        response,
        {
            "snapshot_key": _plan_fingerprint(task_row, segments),
            "force": True,
        },
    )

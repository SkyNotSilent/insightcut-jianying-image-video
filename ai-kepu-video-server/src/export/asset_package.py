"""Build deterministic delivery ZIPs from the assets currently selected by segments."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from src.draft.subtitle import SubtitleWriter


ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg"}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class NoMaterialAssetsError(RuntimeError):
    """Raised when a task has no current image or audio file to package."""


def _task_lock(task_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(task_id, threading.Lock())


def _safe_name(value: str, fallback: str = "InsightCut") -> str:
    invalid = set('<>:"/\\|?*\n\r\t')
    safe = "".join("_" if char in invalid or ord(char) < 32 else char for char in str(value or ""))
    safe = safe.strip(" ._")[:60] or fallback
    if safe.upper() in WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"
    return safe


def _safe_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("任务 ID 不安全")
    return value


def _storage_roots(base_dir: Path) -> tuple[Path, Path]:
    return (
        (base_dir / "output").resolve(),
        (base_dir / "data" / "media").resolve(),
    )


def _resolve_current_file(raw_path, base_dir: Path, allowed_extensions: set[str]) -> tuple[Optional[Path], str]:
    if not raw_path:
        return None, "missing"
    try:
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "invalid_path"
    if resolved.suffix.lower() not in allowed_extensions:
        return None, "unsupported_format"
    if not any(resolved == root or root in resolved.parents for root in _storage_roots(base_dir)):
        return None, "outside_storage"
    if not resolved.is_file():
        return None, "file_missing"
    return resolved, "available"


def _segment_duration(segment: dict) -> float:
    try:
        value = float(segment.get("duration") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 4.0


def _ordered_segments(segments: Iterable[dict]) -> list[dict]:
    return sorted(
        (dict(segment) for segment in (segments or [])),
        key=lambda segment: int(segment.get("segment_index") or 0),
    )


def _material_snapshot(task_id: str, project_name: str, segments: Iterable[dict], base_dir: Path) -> dict:
    ordered = _ordered_segments(segments)
    width = max(3, len(str(max(1, len(ordered)))))
    entries = []
    image_count = 0
    audio_count = 0

    for order, segment in enumerate(ordered, start=1):
        image_path, image_reason = _resolve_current_file(
            segment.get("image_path"), base_dir, ALLOWED_IMAGE_EXTENSIONS
        )
        audio_path, audio_reason = _resolve_current_file(
            segment.get("audio_path"), base_dir, ALLOWED_AUDIO_EXTENSIONS
        )
        if image_path:
            image_count += 1
        if audio_path:
            audio_count += 1
        entries.append({
            "order": order,
            "number": str(order).zfill(width),
            "segment_index": int(segment.get("segment_index") or 0),
            "text": segment.get("text") or "",
            "duration_seconds": _segment_duration(segment),
            "image_path": image_path,
            "audio_path": audio_path,
            "image_status": segment.get("image_status") or image_reason,
            "audio_status": segment.get("audio_status") or audio_reason,
            "image_reason": image_reason,
            "audio_reason": audio_reason,
            "voice_type": segment.get("audio_voice_type") or "",
        })

    fingerprint_entries = []
    for entry in entries:
        item = {
            key: entry[key]
            for key in (
                "order", "segment_index", "text", "duration_seconds",
                "image_status", "audio_status", "image_reason", "audio_reason", "voice_type",
            )
        }
        for asset_type in ("image", "audio"):
            path = entry[f"{asset_type}_path"]
            if path:
                stat = path.stat()
                item[asset_type] = {
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            else:
                item[asset_type] = None
        fingerprint_entries.append(item)

    fingerprint_payload = {
        "task_id": task_id,
        "project_name": project_name,
        "segments": fingerprint_entries,
    }
    snapshot_key = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    segment_count = len(entries)
    return {
        "task_id": task_id,
        "project_name": project_name,
        "safe_project_name": _safe_name(project_name, f"InsightCut_{task_id[:8]}"),
        "snapshot_key": snapshot_key,
        "segment_count": segment_count,
        "image_count": image_count,
        "audio_count": audio_count,
        "complete": bool(segment_count and image_count == segment_count and audio_count == segment_count),
        "missing_image_orders": [entry["order"] for entry in entries if not entry["image_path"]],
        "missing_audio_orders": [entry["order"] for entry in entries if not entry["audio_path"]],
        "entries": entries,
    }


def _output_paths(task_id: str, project_name: str, base_dir: Path) -> tuple[Path, Path]:
    task_id = _safe_task_id(task_id)
    safe_name = _safe_name(project_name, f"InsightCut_{task_id[:8]}")
    output_dir = base_dir / "data" / "media" / task_id / "exports"
    zip_path = output_dir / f"{safe_name}_素材包.zip"
    return zip_path, zip_path.with_suffix(".manifest.json")


def _cached_snapshot_key(sidecar_path: Path) -> Optional[str]:
    if not sidecar_path.is_file():
        return None
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data.get("snapshot_key")


def material_package_state(task_id: str, project_name: str, segments: Iterable[dict], base_dir: Path) -> dict:
    base_dir = Path(base_dir).resolve()
    snapshot = _material_snapshot(task_id, project_name, segments, base_dir)
    zip_path, sidecar_path = _output_paths(task_id, project_name, base_dir)
    package_ready = zip_path.is_file() and _cached_snapshot_key(sidecar_path) == snapshot["snapshot_key"]
    return {
        "available": snapshot["image_count"] + snapshot["audio_count"] > 0,
        "complete": snapshot["complete"],
        "segment_count": snapshot["segment_count"],
        "image_count": snapshot["image_count"],
        "audio_count": snapshot["audio_count"],
        "missing_image_orders": snapshot["missing_image_orders"],
        "missing_audio_orders": snapshot["missing_audio_orders"],
        "snapshot_key": snapshot["snapshot_key"],
        "package_ready": package_ready,
        "filename": zip_path.name if package_ready else None,
    }


def _manifest(snapshot: dict, package_root: str) -> dict:
    segments = []
    for entry in snapshot["entries"]:
        image_file = (
            f'images/{entry["number"]}{entry["image_path"].suffix.lower()}'
            if entry["image_path"] else None
        )
        audio_file = (
            f'audio/{entry["number"]}{entry["audio_path"].suffix.lower()}'
            if entry["audio_path"] else None
        )
        segments.append({
            "order": entry["order"],
            "segment_index": entry["segment_index"],
            "text": entry["text"],
            "duration_seconds": entry["duration_seconds"],
            "image_file": image_file,
            "audio_file": audio_file,
            "image_status": entry["image_status"],
            "audio_status": entry["audio_status"],
            "image_missing_reason": None if image_file else entry["image_reason"],
            "audio_missing_reason": None if audio_file else entry["audio_reason"],
            "voice_type": entry["voice_type"],
        })
    return {
        "schema_version": 1,
        "task_id": snapshot["task_id"],
        "project_name": snapshot["project_name"],
        "generated_at": datetime.now().isoformat(),
        "snapshot_key": snapshot["snapshot_key"],
        "package_root": package_root,
        "segment_count": snapshot["segment_count"],
        "image_count": snapshot["image_count"],
        "audio_count": snapshot["audio_count"],
        "complete": snapshot["complete"],
        "missing_image_orders": snapshot["missing_image_orders"],
        "missing_audio_orders": snapshot["missing_audio_orders"],
        "segments": segments,
    }


def _storyboard_csv(manifest: dict) -> bytes:
    text_buffer = io.StringIO(newline="")
    fieldnames = [
        "order", "segment_index", "text", "duration_seconds", "image_file", "audio_file",
        "image_status", "audio_status", "voice_type",
    ]
    writer = csv.DictWriter(text_buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(manifest["segments"])
    return text_buffer.getvalue().encode("utf-8-sig")


def _readme_text() -> str:
    return (
        "InsightCut 分镜素材包\n\n"
        "1. images/001.* 与 audio/001.* 属于同一个分镜。\n"
        "2. 请按文件编号升序使用图片和配音。\n"
        "3. 缺失素材以 metadata/storyboard.csv 和 metadata/manifest.json 为准。\n"
        "4. 本素材包不等同于剪映草稿，不包含可直接打开的剪映时间轴。\n"
    )


def build_material_package(task_id: str, project_name: str, segments: Iterable[dict], base_dir: Path) -> dict:
    base_dir = Path(base_dir).resolve()
    with _task_lock(_safe_task_id(task_id)):
        snapshot = _material_snapshot(task_id, project_name, segments, base_dir)
        if snapshot["image_count"] + snapshot["audio_count"] == 0:
            raise NoMaterialAssetsError("暂无可打包素材")

        zip_path, sidecar_path = _output_paths(task_id, project_name, base_dir)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        if zip_path.is_file() and _cached_snapshot_key(sidecar_path) == snapshot["snapshot_key"]:
            return _package_result(snapshot, zip_path, cached=True)

        package_root = f'{snapshot["safe_project_name"]}_素材包'
        manifest = _manifest(snapshot, package_root)
        temp_zip = None
        temp_sidecar = None
        try:
            zip_fd, temp_zip = tempfile.mkstemp(prefix="material-package-", suffix=".zip", dir=zip_path.parent)
            os.close(zip_fd)
            with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for folder in ("images", "audio", "metadata"):
                    archive.writestr(f"{package_root}/{folder}/", b"")
                for entry, manifest_entry in zip(snapshot["entries"], manifest["segments"]):
                    if entry["image_path"]:
                        archive.write(entry["image_path"], f'{package_root}/{manifest_entry["image_file"]}')
                    if entry["audio_path"]:
                        archive.write(entry["audio_path"], f'{package_root}/{manifest_entry["audio_file"]}')
                archive.writestr(
                    f"{package_root}/metadata/storyboard.csv",
                    _storyboard_csv(manifest),
                )
                archive.writestr(
                    f"{package_root}/metadata/manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
                archive.writestr(
                    f"{package_root}/metadata/subtitles.srt",
                    SubtitleWriter().render(snapshot["entries"], "srt").encode("utf-8"),
                )
                archive.writestr(f"{package_root}/README.txt", _readme_text().encode("utf-8"))

            sidecar_fd, temp_sidecar = tempfile.mkstemp(
                prefix="material-package-", suffix=".json", dir=zip_path.parent
            )
            os.close(sidecar_fd)
            Path(temp_sidecar).write_text(
                json.dumps({
                    "snapshot_key": snapshot["snapshot_key"],
                    "filename": zip_path.name,
                    "created_at": manifest["generated_at"],
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_zip, zip_path)
            temp_zip = None
            os.replace(temp_sidecar, sidecar_path)
            temp_sidecar = None
        finally:
            for temp_path in (temp_zip, temp_sidecar):
                if temp_path:
                    Path(temp_path).unlink(missing_ok=True)

        return _package_result(snapshot, zip_path, cached=False)


def _package_result(snapshot: dict, zip_path: Path, cached: bool) -> dict:
    return {
        "target": "materials",
        "zip_path": str(zip_path),
        "filename": zip_path.name,
        "snapshot_key": snapshot["snapshot_key"],
        "segment_count": snapshot["segment_count"],
        "image_count": snapshot["image_count"],
        "audio_count": snapshot["audio_count"],
        "complete": snapshot["complete"],
        "missing_image_orders": snapshot["missing_image_orders"],
        "missing_audio_orders": snapshot["missing_audio_orders"],
        "cached": cached,
    }


def current_material_package(
    task_id: str,
    project_name: str,
    segments: Iterable[dict],
    base_dir: Path,
    snapshot_key: str,
) -> Optional[Path]:
    base_dir = Path(base_dir).resolve()
    snapshot = _material_snapshot(task_id, project_name, segments, base_dir)
    if snapshot_key != snapshot["snapshot_key"]:
        return None
    zip_path, sidecar_path = _output_paths(task_id, project_name, base_dir)
    if not zip_path.is_file() or _cached_snapshot_key(sidecar_path) != snapshot_key:
        return None
    return zip_path

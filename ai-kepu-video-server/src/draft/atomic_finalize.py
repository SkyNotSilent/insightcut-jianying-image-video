"""Crash-safe filesystem workspace for publishing an editable draft version."""

import errno
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Tuple


@dataclass(frozen=True)
class FinalizeMedia:
    segment_index: int
    image_path: Path
    audio_path: Path


@dataclass(frozen=True)
class FinalizeWorkspace:
    source_root: Path
    staging_dir: Path
    published_dir: Path
    draft_name: str
    operation_id: str


def _safe_component(value: str) -> str:
    safe = "".join(
        character for character in str(value or "")
        if character.isalnum() or character in {"-", "_", "."}
    ).strip(".")
    if not safe:
        raise ValueError("草稿版本标识无效")
    return safe[:96]


def prepare_finalize_workspace(
    source_root: Path,
    draft_name: str,
    operation_id: str,
) -> FinalizeWorkspace:
    """Create an operation-scoped staging directory beside immutable versions."""
    source_root = source_root.resolve()
    safe_operation = _safe_component(operation_id)
    staging_root = source_root / ".finalize" / "staging" / safe_operation
    published_root = source_root / ".finalize" / "versions" / safe_operation
    staging_dir = staging_root / draft_name
    published_dir = published_root / draft_name
    if published_dir.exists():
        raise FileExistsError(f"草稿版本已存在: {safe_operation}")
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_dir.mkdir(parents=True, exist_ok=False)
    return FinalizeWorkspace(
        source_root=source_root,
        staging_dir=staging_dir,
        published_dir=published_dir,
        draft_name=draft_name,
        operation_id=safe_operation,
    )


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno not in {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            errno.EMLINK,
            errno.ENOTSUP,
        }:
            raise
        shutil.copy2(source, target)


def materialize_finalize_media(
    workspace: FinalizeWorkspace,
    media: Sequence[FinalizeMedia],
) -> Tuple[list, list]:
    """Materialize a stable media snapshot inside staging for portable paths."""
    image_paths = []
    audio_paths = []
    for item in media:
        image_suffix = item.image_path.suffix or ".png"
        audio_suffix = item.audio_path.suffix or ".wav"
        image_target = workspace.staging_dir / "images" / (
            f"segment_{item.segment_index:03d}{image_suffix}"
        )
        audio_target = workspace.staging_dir / "voiceovers" / (
            f"seg_{item.segment_index:03d}{audio_suffix}"
        )
        _link_or_copy(item.image_path, image_target)
        _link_or_copy(item.audio_path, audio_target)
        image_paths.append(str(image_target))
        audio_paths.append(str(audio_target))
    return image_paths, audio_paths


def validate_staged_draft(draft_dir: Path) -> None:
    """Reject partial draft output before it can become a task result."""
    import json

    content_path = draft_dir / "draft_content.json"
    meta_path = draft_dir / "draft_meta_info.json"
    for required in (content_path, meta_path):
        if not required.is_file():
            raise ValueError(f"草稿构建不完整，缺少 {required.name}")
        try:
            json.loads(required.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"草稿文件无法读取: {required.name}") from error
    content = json.loads(content_path.read_text(encoding="utf-8"))
    if not content.get("tracks"):
        raise ValueError("草稿构建不完整，没有轨道数据")
    video_tracks = [
        track for track in content.get("tracks", []) if track.get("type") == "video"
    ]
    if not video_tracks or not any(track.get("segments") for track in video_tracks):
        raise ValueError("草稿构建不完整，视频轨道为空")
    for group in ("videos", "audios"):
        for material in (content.get("materials") or {}).get(group, []):
            raw_path = str(material.get("path") or "").strip()
            if not raw_path:
                raise ValueError(f"草稿 {group} 存在空素材路径")
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = draft_dir / candidate
            if not candidate.is_file():
                raise ValueError(f"草稿素材不存在: {raw_path}")


def build_finalize_archive(draft_dir: Path, draft_name: str) -> Path:
    """Write the archive under a temporary name and publish only a complete ZIP."""
    zip_path = draft_dir / f"{draft_name}.zip"
    temporary_path = draft_dir / f".{draft_name}.zip.tmp"
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in draft_dir.rglob("*"):
                if not file_path.is_file() or file_path in {zip_path, temporary_path}:
                    continue
                rel_path = file_path.relative_to(draft_dir)
                if rel_path.parts and rel_path.parts[0] == "previews":
                    continue
                if file_path.suffix.lower() == ".mp4":
                    continue
                archive.write(file_path, rel_path)
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise ValueError("草稿压缩包生成失败")
        os.replace(temporary_path, zip_path)
        return zip_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def publish_finalize_workspace(workspace: FinalizeWorkspace) -> Path:
    """Publish the completed version with one same-filesystem directory rename."""
    workspace.published_dir.parent.mkdir(parents=True, exist_ok=True)
    if workspace.published_dir.exists():
        raise FileExistsError(f"草稿版本已存在: {workspace.operation_id}")
    workspace.staging_dir.rename(workspace.published_dir)
    workspace.staging_dir.parent.rmdir()
    return workspace.published_dir


def cleanup_finalize_workspace(
    workspace: FinalizeWorkspace,
    *,
    remove_published: bool = False,
) -> None:
    """Remove only paths owned by this operation; never touch the source root."""
    staging_root = workspace.staging_dir.parent
    published_root = workspace.published_dir.parent
    if staging_root.exists():
        shutil.rmtree(staging_root)
    if remove_published and published_root.exists():
        shutil.rmtree(published_root)

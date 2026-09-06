"""MiMo 本地克隆音色的参考音频存储与状态管理。"""

import base64
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Dict, Optional

from src.database.sqlite_client import SQLiteClient


MAX_REFERENCE_DATA_URL_BYTES = 10 * 1024 * 1024
SUPPORTED_REFERENCE_SUFFIXES = {".mp3", ".wav", ".webm", ".ogg"}


class VoiceCloneStore:
    def __init__(
        self,
        base_dir: Path,
        db: SQLiteClient,
        max_data_url_bytes: int = MAX_REFERENCE_DATA_URL_BYTES,
    ):
        self.base_dir = Path(base_dir).resolve()
        self.db = db
        self.max_data_url_bytes = max_data_url_bytes
        self.root = self.base_dir / "data" / "media" / "_voice_clones"
        self.root.mkdir(parents=True, exist_ok=True)

    def _decorate(self, record: Optional[Dict]) -> Optional[Dict]:
        if not record:
            return None
        result = dict(record)
        result["is_enabled"] = bool(result.get("is_enabled"))
        result["consent_confirmed"] = bool(result.get("consent_confirmed"))
        result["voice_type"] = f'mimo-clone:{result["clone_id"]}'
        if result.get("preview_path"):
            result["preview_url"] = f'/media/_voice_clones/{result["clone_id"]}/preview.wav'
        else:
            result["preview_url"] = None
        return result

    def _clone_root(self, clone_id: str) -> Path:
        if not clone_id or any(char not in "0123456789abcdef" for char in clone_id.lower()):
            raise ValueError("无效的克隆音色 ID")
        target = (self.root / clone_id).resolve()
        if target.parent != self.root.resolve():
            raise ValueError("克隆音色路径越界")
        return target

    def _probe(self, audio_path: Path) -> Dict:
        try:
            completed = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=codec_type", "-show_entries", "format=duration",
                    "-of", "json", str(audio_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            payload = json.loads(completed.stdout or "{}")
            streams = payload.get("streams") or []
            duration = float((payload.get("format") or {}).get("duration") or 0)
        except (subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("参考音频无法解析") from exc
        if not streams or duration <= 0:
            raise ValueError("参考音频没有有效音轨")
        return {"duration": duration}

    def _normalize(self, source: Path, destination: Path) -> Dict:
        source = Path(source)
        if not source.exists() or not source.is_file():
            raise ValueError("参考音频文件不存在")
        if source.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
            raise ValueError("只支持 MP3、WAV 或浏览器录音")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}-{uuid.uuid4().hex}.wav")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
                    "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
                    str(temporary),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            metadata = self._probe(temporary)
            encoded_size = len(base64.b64encode(temporary.read_bytes()))
            if encoded_size > self.max_data_url_bytes:
                raise ValueError("参考音频 Base64 后大小超过 10 MB 限制")
            temporary.replace(destination)
            metadata["file_size"] = destination.stat().st_size
            return metadata
        except subprocess.SubprocessError as exc:
            raise ValueError("参考音频无法解析") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def create(self, name: str, upload_path: Path, consent_confirmed: bool) -> Dict:
        clean_name = str(name or "").strip()[:80]
        if not clean_name:
            raise ValueError("请输入克隆音色名称")
        if not consent_confirmed:
            raise ValueError("请先确认已获得该声音的使用授权")
        clone_id = uuid.uuid4().hex
        clone_root = self._clone_root(clone_id)
        reference = clone_root / "reference.wav"
        try:
            metadata = self._normalize(Path(upload_path), reference)
            record = self.db.create_voice_clone(
                {
                    "clone_id": clone_id,
                    "name": clean_name,
                    "reference_path": str(reference),
                    "duration": metadata["duration"],
                    "file_size": metadata["file_size"],
                    "status": "draft",
                    "is_enabled": False,
                    "consent_confirmed": True,
                }
            )
            if not record:
                raise RuntimeError("克隆音色记录保存失败")
            return self._decorate(record)
        except Exception:
            shutil.rmtree(clone_root, ignore_errors=True)
            raise

    def get(self, clone_id: str) -> Optional[Dict]:
        return self._decorate(self.db.get_voice_clone(clone_id))

    def list(self, include_hidden: bool = False):
        return [self._decorate(row) for row in self.db.list_voice_clones(include_hidden)]

    def reference_data_url(self, clone_id: str) -> str:
        record = self.get(clone_id)
        if not record:
            raise ValueError("克隆音色不存在")
        reference = Path(record["reference_path"])
        if not reference.exists():
            raise ValueError("克隆音色参考音频不存在")
        encoded = base64.b64encode(reference.read_bytes())
        if len(encoded) > self.max_data_url_bytes:
            raise ValueError("参考音频 Base64 后大小超过 10 MB 限制")
        return f"data:audio/wav;base64,{encoded.decode('ascii')}"

    def mark_ready(self, clone_id: str, preview_path: Path) -> Dict:
        record = self.get(clone_id)
        if not record:
            raise ValueError("克隆音色不存在")
        source = Path(preview_path)
        self._probe(source)
        target = self._clone_root(clone_id) / "preview.wav"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        updated = self.db.update_voice_clone(
            clone_id,
            {"status": "ready", "preview_path": str(target), "error_message": None},
        )
        return self._decorate(updated)

    def mark_failed(self, clone_id: str, error_message: str) -> Dict:
        updated = self.db.update_voice_clone(
            clone_id,
            {"status": "failed", "error_message": str(error_message or "克隆试听失败")[:500]},
        )
        if not updated:
            raise ValueError("克隆音色不存在")
        return self._decorate(updated)

    def replace_reference(self, clone_id: str, upload_path: Path) -> Dict:
        record = self.get(clone_id)
        if not record:
            raise ValueError("克隆音色不存在")
        if self.db.is_voice_clone_referenced(clone_id):
            return self.create(record["name"] + "（新录音）", upload_path, bool(record.get("consent_confirmed")))
        target = self._clone_root(clone_id) / "reference.wav"
        metadata = self._normalize(Path(upload_path), target)
        preview = self._clone_root(clone_id) / "preview.wav"
        if preview.exists():
            preview.unlink()
        updated = self.db.update_voice_clone(
            clone_id,
            {
                "reference_path": str(target),
                "duration": metadata["duration"],
                "file_size": metadata["file_size"],
                "status": "draft",
                "preview_path": None,
                "error_message": None,
                "is_enabled": False,
            },
        )
        return self._decorate(updated)

    def update(self, clone_id: str, patch: Dict) -> Dict:
        if "status" in (patch or {}):
            raise ValueError("音色状态只能在试听完成后更新")
        allowed = {key: value for key, value in (patch or {}).items() if key in {"name", "is_enabled"}}
        if "name" in allowed:
            allowed["name"] = str(allowed["name"] or "").strip()[:80]
            if not allowed["name"]:
                raise ValueError("克隆音色名称不能为空")
        if allowed.get("is_enabled") and (self.get(clone_id) or {}).get("status") != "ready":
            raise ValueError("克隆音色必须试听成功后才能启用")
        updated = self.db.update_voice_clone(clone_id, allowed)
        if not updated:
            raise ValueError("克隆音色不存在")
        return self._decorate(updated)

    def delete_or_hide(self, clone_id: str) -> Dict:
        record = self.get(clone_id)
        if not record:
            raise ValueError("克隆音色不存在")
        if self.db.is_voice_clone_referenced(clone_id):
            self.db.update_voice_clone(
                clone_id, {"status": "hidden", "is_enabled": False}
            )
            return {"outcome": "hidden", "clone_id": clone_id, "reason": "referenced"}
        self.db.delete_voice_clone(clone_id)
        shutil.rmtree(self._clone_root(clone_id), ignore_errors=True)
        return {"outcome": "deleted", "clone_id": clone_id}

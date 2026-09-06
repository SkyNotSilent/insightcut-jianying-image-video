import base64
import subprocess
import wave
from pathlib import Path

import pytest

from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient
from src.draft.voice_clone import VoiceCloneStore


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


def write_wav(path: Path, seconds: float = 0.2, sample_rate: int = 24000):
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x01\x00" * frames)


def test_requires_consent_and_a_named_audio_reference(tmp_path, temp_db):
    source = tmp_path / "source.wav"
    write_wav(source)
    store = VoiceCloneStore(tmp_path, temp_db)

    with pytest.raises(ValueError, match="授权"):
        store.create("我的声音", source, consent_confirmed=False)
    with pytest.raises(ValueError, match="名称"):
        store.create(" ", source, consent_confirmed=True)


def test_creates_normalized_local_reference_and_data_url(tmp_path, temp_db):
    source = tmp_path / "source.wav"
    write_wav(source, seconds=0.35)
    store = VoiceCloneStore(tmp_path, temp_db)

    clone = store.create("我的声音", source, consent_confirmed=True)

    assert clone["status"] == "draft"
    assert clone["voice_type"] == f'mimo-clone:{clone["clone_id"]}'
    reference = Path(clone["reference_path"])
    assert reference == tmp_path / "data" / "media" / "_voice_clones" / clone["clone_id"] / "reference.wav"
    assert reference.exists()
    assert clone["duration"] == pytest.approx(0.35, abs=0.05)
    data_url = store.reference_data_url(clone["clone_id"])
    assert data_url.startswith("data:audio/wav;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == reference.read_bytes()


def test_accepts_browser_webm_recording_and_normalizes_to_wav(tmp_path, temp_db):
    source_wav = tmp_path / "source.wav"
    source_webm = tmp_path / "recording.webm"
    write_wav(source_wav)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source_wav), str(source_webm)],
        check=True,
    )
    store = VoiceCloneStore(tmp_path, temp_db)

    clone = store.create("浏览器录音", source_webm, consent_confirmed=True)

    assert Path(clone["reference_path"]).suffix == ".wav"
    assert clone["duration"] > 0


def test_rejects_invalid_or_oversized_audio(tmp_path, temp_db):
    invalid = tmp_path / "invalid.wav"
    invalid.write_text("not audio")
    store = VoiceCloneStore(tmp_path, temp_db)
    with pytest.raises(ValueError, match="无法解析"):
        store.create("坏音频", invalid, consent_confirmed=True)

    source = tmp_path / "source.wav"
    write_wav(source)
    limited = VoiceCloneStore(tmp_path, temp_db, max_data_url_bytes=32)
    with pytest.raises(ValueError, match="10 MB|大小"):
        limited.create("过大音频", source, consent_confirmed=True)


def test_preview_state_and_reference_replacement_are_persisted(tmp_path, temp_db):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    preview = tmp_path / "preview.wav"
    write_wav(first, seconds=0.2)
    write_wav(second, seconds=0.4)
    write_wav(preview, seconds=0.1)
    store = VoiceCloneStore(tmp_path, temp_db)
    clone = store.create("可重试音色", first, consent_confirmed=True)

    ready = store.mark_ready(clone["clone_id"], preview)
    assert ready["status"] == "ready"
    assert Path(ready["preview_path"]).exists()
    assert ready["preview_url"] == f'/media/_voice_clones/{clone["clone_id"]}/preview.wav'

    replaced = store.replace_reference(clone["clone_id"], second)
    assert replaced["status"] == "draft"
    assert replaced["preview_path"] is None
    assert replaced["duration"] == pytest.approx(0.4, abs=0.05)

    failed = store.mark_failed(clone["clone_id"], "provider failed")
    assert failed["status"] == "failed"
    assert failed["error_message"] == "provider failed"
    assert Path(failed["reference_path"]).exists()


def test_delete_hides_referenced_clone_and_removes_unreferenced_clone(tmp_path, temp_db):
    source = tmp_path / "source.wav"
    write_wav(source)
    store = VoiceCloneStore(tmp_path, temp_db)
    referenced = store.create("已引用", source, consent_confirmed=True)
    temp_db.create_task(
        "task-1", "文案", "知识科普", 100,
        voice_type=referenced["voice_type"],
    )

    hidden = store.delete_or_hide(referenced["clone_id"])
    assert hidden["outcome"] == "hidden"
    assert store.get(referenced["clone_id"])["status"] == "hidden"
    assert Path(referenced["reference_path"]).exists()

    removable = store.create("无引用", source, consent_confirmed=True)
    removed_root = Path(removable["reference_path"]).parent
    removed = store.delete_or_hide(removable["clone_id"])
    assert removed["outcome"] == "deleted"
    assert store.get(removable["clone_id"]) is None
    assert not removed_root.exists()


def test_referenced_voice_replacement_creates_version_and_keeps_old_audio(tmp_path, temp_db):
    source=tmp_path/'first.wav';replacement=tmp_path/'second.wav'
    write_wav(source,.2);write_wav(replacement,.4)
    store=VoiceCloneStore(tmp_path,temp_db)
    old=store.create('原音色',source,consent_confirmed=True)
    store.mark_ready(old['clone_id'],source)
    temp_db.create_task('uses-old','旧项目','风格',100,voice_type=old['voice_type'])
    old_bytes=Path(old['reference_path']).read_bytes()
    new=store.replace_reference(old['clone_id'],replacement)
    assert new['clone_id'] != old['clone_id']
    assert new['status']=='draft' and not new['is_enabled']
    assert temp_db.get_task('uses-old')['voice_type']==old['voice_type']
    assert Path(old['reference_path']).read_bytes()==old_bytes

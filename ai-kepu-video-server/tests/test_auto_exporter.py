import json

import pytest

from src.export.auto_export import AutoExporter, ExportVerificationError


def _draft(tmp_path):
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / "draft_content.json").write_text(json.dumps({
        "tracks": [{"type": "video", "segments": [{}]}],
        "materials": {"videos": [], "audios": []},
    }), encoding="utf-8")
    (draft / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    return draft


def test_exporter_rejects_boolean_or_missing_artifact(tmp_path):
    with pytest.raises(ExportVerificationError, match="结构化"):
        AutoExporter({"mp4": lambda: True}).export("mp4")
    with pytest.raises(ExportVerificationError, match="有效文件"):
        AutoExporter({"mp4": lambda: {"video_path": str(tmp_path / "missing.mp4")}}).export("mp4")


def test_exporter_verifies_draft_zip_and_returns_structured_result(tmp_path):
    draft = _draft(tmp_path)
    archive = tmp_path / "draft.zip"
    archive.write_bytes(b"zip")
    opened = []
    result = AutoExporter(
        {"draft": lambda: {"target": "draft", "draft_path": str(draft), "zip_path": str(archive)}},
        directory_opener=opened.append,
    ).export("draft", reveal_output=True)

    assert result["success"] is True
    assert result["verified"] is True
    assert result["revealed_output"] is True
    assert opened == [draft.resolve()]


def test_reveal_failure_is_only_a_warning(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    def fail(_path):
        raise OSError("not available")

    result = AutoExporter(
        {"mp4": lambda: {"target": "mp4", "video_path": str(video)}},
        directory_opener=fail,
    ).export("mp4", reveal_output=True)

    assert result["success"] is True
    assert result["revealed_output"] is False
    assert result["warnings"] == ["导出已完成，但无法自动打开输出目录。"]

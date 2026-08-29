from pathlib import Path

from src.draft.subtitle import SubtitleWriter


def test_subtitle_writer_sorts_segments_accumulates_duration_and_keeps_unicode():
    segments = [
        {"segment_index": 10, "text": "第三段🌊", "duration": 1.25},
        {"segment_index": 0, "text": "第一段。", "duration": 2},
        {"segment_index": 2, "text": "第二段", "duration": None},
    ]
    writer = SubtitleWriter(default_duration=4)

    srt = writer.render(segments, "srt")
    vtt = writer.render(segments, "vtt")

    assert "00:00:00,000 --> 00:00:02,000\n第一段" in srt
    assert "00:00:02,000 --> 00:00:06,000\n第二段" in srt
    assert "00:00:06,000 --> 00:00:07,250\n第三段🌊" in srt
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:06.000 --> 00:00:07.250" in vtt


def test_subtitle_writer_atomically_replaces_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "字幕.srt"
    target.write_text("old", encoding="utf-8")
    replacements = []

    import src.draft.subtitle as module

    real_replace = module.os.replace

    def observed_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        assert Path(source).parent == target.parent
        assert Path(source).read_text(encoding="utf-8").startswith("1\n")
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", observed_replace)
    result = SubtitleWriter().write(
        target,
        [{"segment_index": 0, "text": "你好，世界。", "duration": 1}],
        "srt",
    )

    assert result == target
    assert replacements and replacements[0][1] == target
    assert "你好世界" in target.read_text(encoding="utf-8")

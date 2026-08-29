import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("test_jianying_compatibility.py")
SPEC = importlib.util.spec_from_file_location("jianying_compatibility_tool", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
JianyingCompatibilityTester = MODULE.JianyingCompatibilityTester


def _write_draft(tmp_path, *, video_segments, audio_segments=None, duration=2_000_000):
    content = {
        "id": "draft-id",
        "duration": duration,
        "fps": 30,
        "canvas_config": {},
        "platform": {"os": "mac"},
        "materials": {"videos": [], "audios": [], "images": [], "texts": []},
        "tracks": [
            {"type": "video", "segments": video_segments},
            {"type": "audio", "segments": audio_segments or []},
        ],
    }
    (tmp_path / "draft_content.json").write_text(
        json.dumps(content), encoding="utf-8"
    )
    (tmp_path / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    return JianyingCompatibilityTester(str(tmp_path))


def _segment(start, duration=1_000_000):
    return {"target_timerange": {"start": start, "duration": duration}}


def test_empty_video_track_is_an_error(tmp_path):
    tester = _write_draft(tmp_path, video_segments=[])
    assert tester.run_all_tests() is False
    assert any("视频轨道为空" in error for error in tester.errors)


def test_track_segment_count_mismatch_is_an_error(tmp_path):
    tester = _write_draft(
        tmp_path,
        video_segments=[_segment(0), _segment(1_000_000)],
        audio_segments=[_segment(0)],
    )
    assert tester.run_all_tests() is False
    assert any("分镜数量不一致" in error for error in tester.errors)


def test_gap_over_one_frame_and_duration_difference_are_errors(tmp_path):
    tester = _write_draft(
        tmp_path,
        video_segments=[_segment(0), _segment(1_100_000)],
        audio_segments=[_segment(0), _segment(1_000_000)],
        duration=2_500_000,
    )
    assert tester.run_all_tests() is False
    assert any("间隙" in error for error in tester.errors)
    assert any("时长" in error for error in tester.errors)


def test_one_frame_tolerance_remains_valid(tmp_path):
    tester = _write_draft(
        tmp_path,
        video_segments=[_segment(0), _segment(1_000_000 + 33_333)],
        audio_segments=[_segment(0), _segment(1_000_000 + 33_333)],
        duration=2_033_333,
    )
    assert tester.run_all_tests() is True

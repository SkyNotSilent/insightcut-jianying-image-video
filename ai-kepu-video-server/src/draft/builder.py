"""
草稿构建器
构建剪映草稿的核心模块
"""

import json
import logging
import os
import random
import shutil
from pathlib import Path
from typing import List, Optional

import pyJianYingDraft as draft
from pyJianYingDraft import (
    AudioMaterial,
    AudioSegment,
    TextSegment,
    TrackType,
    VideoMaterial,
    VideoSegment,
    Timerange,
    KeyframeProperty,
)
from pyJianYingDraft.metadata.font_meta import FontType

from src.draft.animation_scheduler import AnimationScheduler
from src.utils.rendering import subtitle_preset_for_canvas
from src.utils.subtitle_text import normalize_subtitle_text

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_DURATION_US = 4_000_000  # 4 秒（无配音时使用）
SUBTITLE_SAFE_WIDTH_RATIO = 0.86
SUBTITLE_MIN_LINE_CHARS = 22


class DraftBuilder:
    """草稿构建器 - 构建剪映草稿"""

    def __init__(
        self,
        config_path: str = "config/settings.json",
        template_dir: str = "config/templates",
        canvas: Optional[dict] = None,
        subtitle_options: Optional[dict] = None,
    ):
        self.config_path = Path(config_path)
        self.template_dir = Path(template_dir)
        self.canvas_override = canvas or {}
        self.subtitle_options = subtitle_options or {}
        self.config = self._load_config()

    def _load_config(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.warning(f"加载配置失败，使用默认值: {e}")
            config = {
                "canvas": {"width": 1920, "height": 1080, "fps": 30},
                "draft_path": "output/drafts",
            }
        if self.canvas_override:
            config["canvas"] = {**config.get("canvas", {}), **self.canvas_override}
        return config

    def _add_ken_burns(self, video_seg: VideoSegment, duration_us: int, rng: random.Random):
        """给图片段落加 Ken Burns 关键帧动画（轻微放大为主，偶尔平移）"""
        # 起始缩放：1.0，结束缩放：1.05~1.12（轻微放大）
        scale_end = rng.uniform(1.05, 1.12)

        # 动画类型：0=纯放大，1=放大+左移，2=放大+右移，3=放大+斜移
        anim_type = rng.choices([0, 1, 2, 3], weights=[40, 20, 20, 20])[0]

        # scale 关键帧：开始 1.0 → 结束 scale_end
        video_seg.add_keyframe(KeyframeProperty.scale_x, 0, 1.0)
        video_seg.add_keyframe(KeyframeProperty.scale_x, duration_us, scale_end)
        video_seg.add_keyframe(KeyframeProperty.scale_y, 0, 1.0)
        video_seg.add_keyframe(KeyframeProperty.scale_y, duration_us, scale_end)

        # position 关键帧（轻微平移，单位是画布比例）
        if anim_type == 1:   # 向左平移
            video_seg.add_keyframe(KeyframeProperty.position_x, 0, 0.02)
            video_seg.add_keyframe(KeyframeProperty.position_x, duration_us, -0.02)
        elif anim_type == 2: # 向右平移
            video_seg.add_keyframe(KeyframeProperty.position_x, 0, -0.02)
            video_seg.add_keyframe(KeyframeProperty.position_x, duration_us, 0.02)
        elif anim_type == 3: # 斜移（左上→右下）
            video_seg.add_keyframe(KeyframeProperty.position_x, 0, -0.015)
            video_seg.add_keyframe(KeyframeProperty.position_x, duration_us, 0.015)
            video_seg.add_keyframe(KeyframeProperty.position_y, 0, -0.015)
            video_seg.add_keyframe(KeyframeProperty.position_y, duration_us, 0.015)

    def _get_sfx_material(self, sfx_key: str):
        """音效接口保留（未使用）"""
        return None

    @staticmethod
    def _subtitle_visual_units(text: str) -> float:
        units = 0.0
        for char in text or "":
            if char.isspace():
                units += 0.35
            elif ord(char) < 128:
                units += 0.55
            else:
                units += 1.0
        return units

    @staticmethod
    def _single_line_subtitle_size(text: str, width: int, height: int, base_size: float, base_ratio: float) -> float:
        fit_units = max(SUBTITLE_MIN_LINE_CHARS, DraftBuilder._subtitle_visual_units(text))
        base_px = height * base_ratio
        single_line_px = (width * SUBTITLE_SAFE_WIDTH_RATIO) / fit_units
        return max(3.0, base_size * min(1.0, single_line_px / base_px))

    def build(
        self,
        segments: List[str],
        media_paths: List[str],
        draft_name: str = "auto_video",
        voiceover_files: Optional[List[str]] = None,
        subtitle_files: Optional[List[str]] = None,
        animation_seed: Optional[int] = None,
        subtitle_y: Optional[float] = None,  # 字幕垂直位置，None=按画布比例自动选默认值
        output_dir: Optional[str] = None,  # 草稿输出目录
    ) -> str:
        logger.info(f"开始构建草稿: {draft_name}，共 {len(segments)} 段")

        canvas = self.config.get("canvas", {})
        width  = canvas.get("width", 1920)
        height = canvas.get("height", 1080)
        fps    = canvas.get("fps", 30)

        # 使用传入的 output_dir 或配置中的 draft_path
        if output_dir:
            draft_path_str = output_dir
        else:
            draft_path_str = self.config.get("draft_path", "output/drafts")

        subtitle_preset = subtitle_preset_for_canvas(width, height, self.subtitle_options)

        # 使用传入的 output_dir 作为草稿目录
        draft_path = Path(draft_path_str)
        draft_path.mkdir(parents=True, exist_ok=True)

        # 保存素材目录（如果存在），因为 create_draft 会删除整个目录
        import tempfile
        temp_backup = None
        preserve_dirs = ["images", "voiceovers", "previews"]
        preserve_files = [p for p in draft_path.glob("*.mp4") if p.is_file()]

        if any((draft_path / name).exists() for name in preserve_dirs) or preserve_files:
            temp_backup = Path(tempfile.mkdtemp())
            logger.info(f"备份素材到临时目录: {temp_backup}")

            for dirname in preserve_dirs:
                source_dir = draft_path / dirname
                if source_dir.exists():
                    shutil.copytree(source_dir, temp_backup / dirname)
                    logger.info(f"备份 {dirname} 目录: {len(list(source_dir.glob('*')))} 个文件")

            if preserve_files:
                files_dir = temp_backup / "_root_files"
                files_dir.mkdir(exist_ok=True)
                for file_path in preserve_files:
                    shutil.copy2(file_path, files_dir / file_path.name)
                logger.info(f"备份根目录 MP4: {len(preserve_files)} 个文件")

        # 直接在 draft_path 的父目录创建草稿，使用 draft_path 的名称
        draft_folder = draft.DraftFolder(draft_path.parent)
        script = draft_folder.create_draft(
            draft_path.name, width, height, fps, allow_replace=True
        )
        draft_dir = draft_path

        # 恢复素材目录
        if temp_backup:
            logger.info(f"恢复素材到草稿目录: {draft_dir}")

            for dirname in preserve_dirs:
                backup_dir = temp_backup / dirname
                if backup_dir.exists():
                    shutil.copytree(backup_dir, draft_dir / dirname)
                    logger.info(f"恢复 {dirname} 目录")

            root_files = temp_backup / "_root_files"
            if root_files.exists():
                for file_path in root_files.glob("*"):
                    shutil.copy2(file_path, draft_dir / file_path.name)
                logger.info("恢复根目录 MP4")

            # 清理临时目录
            shutil.rmtree(temp_backup)
            logger.info(f"清理临时备份目录")

        # 轨道：视频 / 配音 / 字幕（无音效轨）
        script.add_track(TrackType.video, "视频轨")
        script.add_track(TrackType.audio, "配音轨")
        script.add_track(TrackType.text,  "字幕轨")

        # 生成动画方案
        scheduler = AnimationScheduler(seed=animation_seed)
        plans = scheduler.schedule(segments)
        logger.info(f"动画方案生成完成 (seed={animation_seed})")

        rng = random.Random(animation_seed)
        cursor_us = 0

        for i, (text, img_path) in enumerate(zip(segments, media_paths)):
            plan = plans[i]
            intro_name = plan.intro.name if plan.intro else "渐显"
            sfx_name   = plan.sfx or "无"
            dur_ms     = plan.intro_duration // 1000 if plan.intro_duration else 0
            logger.info(f"  [{i+1}/{len(segments)}] 入场={intro_name}({dur_ms}ms) 音效={sfx_name}")

            # ── 确定此段时长 ──────────────────────────────────────────────
            segment_dur = DEFAULT_IMAGE_DURATION_US
            audio_mat   = None

            if voiceover_files and i < len(voiceover_files):
                vo = voiceover_files[i]
                if vo and Path(vo).exists():
                    audio_mat   = AudioMaterial(vo)
                    segment_dur = audio_mat.duration

            t_range = Timerange(cursor_us, segment_dur)

            # ── 视频/图片轨道 ─────────────────────────────────────────────
            if img_path and Path(img_path).exists():
                vm = VideoMaterial(img_path)
                if vm.material_type == "photo":
                    vs = VideoSegment(vm, t_range)
                    self._add_ken_burns(vs, segment_dur, rng)
                    script.add_segment(vs, "视频轨")
                else:
                    actual = min(segment_dur, vm.duration)
                    vs = VideoSegment(vm, Timerange(cursor_us, actual),
                                      source_timerange=Timerange(0, actual))
                    script.add_segment(vs, "视频轨")
            else:
                logger.warning(f"素材不存在: {img_path}")

            # ── 配音轨道 ──────────────────────────────────────────────────
            if audio_mat:
                script.add_segment(
                    AudioSegment(audio_mat, t_range, volume=1.0),
                    "配音轨",
                )

            # ── 字幕轨道（带动画）────────────────────────────────────────
            # 字幕参数和 FFmpeg/前端预览共用同一套 ratio preset；首尾不含标点。
            clean_text = normalize_subtitle_text(text)
            subtitle_size = self._single_line_subtitle_size(
                clean_text,
                width,
                height,
                subtitle_preset["draft_base_size"],
                subtitle_preset["font_size_ratio"],
            )
            text_style = draft.TextStyle(size=subtitle_size, color=(1.0, 1.0, 1.0), align=1)
            default_y = subtitle_preset["draft_transform_y"]
            text_clip = draft.ClipSettings(transform_y=subtitle_y if subtitle_y is not None else default_y)
            border = draft.TextBorder(color=(0.0, 0.0, 0.0), width=subtitle_preset["draft_border_width"])
            text_seg = TextSegment(clean_text, t_range, font=FontType.SourceHanSansCN_Bold,
                                   style=text_style, border=border, clip_settings=text_clip)

            if plan.intro is not None:
                # 入场时长不能超过 segment 时长的一半
                safe_dur = min(plan.intro_duration, segment_dur // 2) if plan.intro_duration else None
                text_seg.add_animation(plan.intro, duration=safe_dur)

            if plan.loop is not None and segment_dur > 1_500_000:
                text_seg.add_animation(plan.loop)

            script.add_segment(text_seg, "字幕轨")

            cursor_us += segment_dur

        script.save()

        self._convert_paths_to_relative(draft_dir)

        result = str(draft_dir)
        logger.info(f"草稿构建成功: {result}")
        return result

    def _convert_paths_to_relative(self, draft_dir: Path):
        """将 draft_content.json 中的绝对路径转换为相对路径（相对于草稿目录）"""
        json_path = draft_dir / "draft_content.json"
        if not json_path.exists():
            return

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        draft_dir_str = str(draft_dir)
        converted = 0

        if "materials" in data and "videos" in data["materials"]:
            for video in data["materials"]["videos"]:
                if "path" in video and os.path.isabs(video["path"]):
                    try:
                        rel = os.path.relpath(video["path"], draft_dir_str)
                        video["path"] = rel.replace("\\", "/")
                        converted += 1
                    except ValueError:
                        pass

        if "materials" in data and "audios" in data["materials"]:
            for audio in data["materials"]["audios"]:
                if "path" in audio and os.path.isabs(audio["path"]):
                    try:
                        rel = os.path.relpath(audio["path"], draft_dir_str)
                        audio["path"] = rel.replace("\\", "/")
                        converted += 1
                    except ValueError:
                        pass

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"已将 {converted} 个绝对路径转为相对路径")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("DraftBuilder 初始化成功")

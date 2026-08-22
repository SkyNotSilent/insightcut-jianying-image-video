"""
FFmpeg 视频导出模块
将图片 + 配音 + 字幕直接拼接为 MP4，与剪映草稿并行输出。
"""

import json
import logging
import random
import shutil
import subprocess
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional

from src.utils.rendering import subtitle_preset_for_canvas
from src.utils.subtitle_text import normalize_subtitle_text

logger = logging.getLogger(__name__)

DEFAULT_DURATION_US = 4_000_000  # 4s fallback
DEFAULT_FADE_SECONDS = 0.3
RENDER_PIPELINE_VERSION = 2
SUBTITLE_SAFE_WIDTH_RATIO = 0.86
SUBTITLE_MIN_LINE_CHARS = 22


class RenderCancelled(RuntimeError):
    """Raised at a safe render checkpoint after the caller requests cancel."""


def _load_render_config(config_path: str = "config/settings.json", canvas_override: Optional[dict] = None, subtitle_options: Optional[dict] = None) -> dict:
    cfg = FFmpegExporter._load_config(config_path)
    canvas = {**cfg.get("canvas", {}), **(canvas_override or {})}
    width = canvas.get("width", 1920)
    height = canvas.get("height", 1080)
    fps = canvas.get("fps", 30)
    subtitle_preset = subtitle_preset_for_canvas(width, height, subtitle_options)
    fontsize = int(height * subtitle_preset["font_size_ratio"])
    border_width = int(subtitle_preset.get("border_width") or 0)

    return {
        "pipeline_version": RENDER_PIPELINE_VERSION,
        "canvas": {
            "width": width,
            "height": height,
            "fps": fps,
            "aspect_ratio": width / height,
        },
        "duration": {
            "default_seconds": DEFAULT_DURATION_US / 1_000_000,
        },
        "subtitle": {
            "font_size": fontsize,
            "font_size_ratio": fontsize / height,
            "font_color": "#ffffff",
            "border_color": "#000000",
            "border_width": border_width,
            "x": "center",
            "y_ratio": subtitle_preset["y_ratio"],
            "draft_transform_y": subtitle_preset["draft_transform_y"],
            "draft_border_width": subtitle_preset["draft_border_width"],
            "font_family": "SourceHanSansCN_Bold",
        },
        "fade": {
            "seconds": DEFAULT_FADE_SECONDS,
        },
        "ken_burns": {
            "scale_min": 1.05,
            "scale_max": 1.12,
            "types": [0, 1, 2, 3, 4, 5, 6],
            "weights": [30, 12, 12, 12, 12, 11, 11],
        },
    }


def build_animation_params(segment_count: int, animation_seed: Optional[int] = None) -> list:
    rng = random.Random(animation_seed)
    params = []
    for _ in range(segment_count):
        params.append({
            "scale_end": rng.uniform(1.05, 1.12),
            "anim_type": rng.choices(
                [0, 1, 2, 3, 4, 5, 6],
                weights=[30, 12, 12, 12, 12, 11, 11],
            )[0],
        })
    return params


class FFmpegExporter:
    """FFmpeg 视频导出器"""

    def __init__(self, config_path: str = "config/settings.json", canvas: Optional[dict] = None, subtitle_options: Optional[dict] = None):
        self._cfg = self._load_config(config_path)
        self.render_config = _load_render_config(config_path, canvas, subtitle_options)
        canvas = self.render_config["canvas"]
        self.width = canvas["width"]
        self.height = canvas["height"]
        self.fps = canvas["fps"]

        ff_cfg = self._cfg.get("ffmpeg", {})
        self.crf = ff_cfg.get("crf", 20)
        self.preset = ff_cfg.get("preset", "fast")

        self._ffmpeg = self._find_ffmpeg()
        self._encoder, self._use_crf = self._detect_encoder()
        self._font = self._find_font(ff_cfg.get("font_path"))

    @staticmethod
    def get_render_config(config_path: str = "config/settings.json", canvas: Optional[dict] = None, subtitle_options: Optional[dict] = None) -> dict:
        return _load_render_config(config_path, canvas, subtitle_options)

    # ── 初始化辅助 ─────────────────────────────────────────────────

    def _detect_encoder(self) -> tuple:
        """检测可用的 H.264 编码器，返回 (encoder_name, supports_crf)"""
        # 优先 libx264（支持 crf），fallback 到 Windows MediaFoundation
        for enc, crf in [("libx264", True), ("h264_nvenc", False), ("h264_mf", False)]:
            try:
                r = subprocess.run(
                    [self._ffmpeg, "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=5,
                )
                if enc in r.stdout:
                    logger.info(f"使用编码器: {enc}")
                    return enc, crf
            except Exception:
                pass
        logger.warning("未检测到 H.264 编码器，fallback 到 libx264")
        return "libx264", True

    @staticmethod
    def _load_config(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _find_ffmpeg() -> str:
        path = shutil.which("ffmpeg")
        if path:
            return path
        # 通过 imageio-ffmpeg 获取完整版 ffmpeg
        try:
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and Path(path).is_file():
                return path
        except ImportError:
            pass
        raise RuntimeError(
            "找不到 ffmpeg，请 pip install imageio-ffmpeg 或安装到系统 PATH"
        )

    @staticmethod
    def _find_font(override: Optional[str] = None) -> str:
        candidates = []
        if override:
            candidates.append(override)
        candidates += [
            # Shared subtitle target: Source Han / Noto CJK bold-ish sans.
            "/System/Library/Fonts/SourceHanSansCN-Bold.otf",
            "/Library/Fonts/SourceHanSansCN-Bold.otf",
            "/System/Library/Fonts/NotoSansCJKsc-Bold.otf",
            "/Library/Fonts/NotoSansCJKsc-Bold.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            # macOS
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
            # Linux (Docker / 云端)
            "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
            # Windows
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
        return ""

    # ── WAV 时长 ───────────────────────────────────────────────────

    @staticmethod
    def _get_wav_duration_us(wav_path: str) -> int:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        return int(frames / rate * 1_000_000)

    # ── Ken Burns 滤镜 ─────────────────────────────────────────────

    @staticmethod
    def _zoompan_expr(
        anim_type: int,
        scale_end: float,
        total_frames: int,
        width: int,
        height: int,
    ) -> str:
        delta = scale_end - 1.0
        d = total_frames
        z = f"1.0+{delta:.6f}*on/{d}"
        cx = "iw/2-(iw/zoom/2)"
        cy = "ih/2-(ih/zoom/2)"

        px = int(0.02 * width)
        py = int(0.015 * height)

        if anim_type == 1:  # zoom + 左移
            x = f"{cx}+{px}*(1-2*on/{d})"
            y = cy
        elif anim_type == 2:  # zoom + 右移
            x = f"{cx}-{px}*(1-2*on/{d})"
            y = cy
        elif anim_type == 3:  # zoom + 斜移
            x = f"{cx}-{px}*(1-2*on/{d})"
            y = f"{cy}-{py}*(1-2*on/{d})"
        else:  # type 0 纯放大
            x = cx
            y = cy

        return (
            f"zoompan=z='{z}':x='{x}':y='{y}'"
            f":d={d}:s={width}x{height}:fps={width}"  # fps 占位，下面覆盖
        )

    # ── drawtext 字幕 ──────────────────────────────────────────────

    def _drawtext_expr(self, text: str) -> str:
        clean = normalize_subtitle_text(text)

        escaped = (
            clean.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\u2019")  # 智能单引号替换，避免 shell 问题
            .replace("%", "%%")
        )

        font_esc = self._font.replace("\\", "/").replace(":", "\\:") if self._font else ""

        fontsize = self._subtitle_font_size(clean)
        subtitle_cfg = self.render_config["subtitle"]
        y_pos = f"h*{subtitle_cfg['y_ratio']}"

        parts = [
            f"drawtext=text='{escaped}'",
            f"fontsize={fontsize}",
            "fontcolor=white",
            f"borderw={subtitle_cfg['border_width']}",
            "bordercolor=black",
            "x=(w-text_w)/2",
            f"y={y_pos}",
        ]
        if font_esc:
            parts.insert(1, f"fontfile='{font_esc}'")

        return ":".join(parts)

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

    def _subtitle_font_size(self, text: str) -> int:
        base_ratio = self.render_config["subtitle"]["font_size_ratio"]
        base_size = self.height * base_ratio
        fit_units = max(SUBTITLE_MIN_LINE_CHARS, self._subtitle_visual_units(text))
        single_line_size = (self.width * SUBTITLE_SAFE_WIDTH_RATIO) / fit_units
        return max(12, int(min(base_size, single_line_size)))

    # ── 单段编码 ───────────────────────────────────────────────────

    def _build_segment_clip(
        self,
        index: int,
        image_path: str,
        audio_path: Optional[str],
        text: str,
        anim_type: int,
        scale_end: float,
        tmp_dir: str,
    ) -> str:
        if audio_path and Path(audio_path).exists():
            duration_us = self._get_wav_duration_us(audio_path)
        else:
            duration_us = DEFAULT_DURATION_US
            audio_path = None

        duration_s = duration_us / 1_000_000
        total_frames = int(duration_s * self.fps)
        if total_frames < 1:
            total_frames = 1

        # 构建滤镜链
        # 优化：先缩小到目标分辨率附近，再做动画处理，避免在超高分辨率上计算
        # zoompan 工作区设为目标分辨率的 1.2 倍（足够容纳缩放+平移）
        margin = max(scale_end, 1.0) + 0.2
        zw = int(self.width * margin)
        zh = int(self.height * margin)
        # 保持偶数
        zw += zw % 2
        zh += zh % 2

        delta = scale_end - 1.0
        d = total_frames
        z_expr = f"1.0+{delta:.6f}*on/{d}"
        cx = "iw/2-(iw/zoom/2)"
        cy = "ih/2-(ih/zoom/2)"
        # 单方向匀速平移（从 A→B，不来回）
        # 平移总量控制在画面的 1%，配合放大不会露出黑边
        px = 0.01 * zw
        py = 0.01 * zh

        if anim_type == 1:    # 放大 + 缓慢左移
            x_expr = f"{cx}+{px:.1f}*(1-on/{d})"
            y_expr = cy
        elif anim_type == 2:  # 放大 + 缓慢右移
            x_expr = f"{cx}-{px:.1f}*(1-on/{d})"
            y_expr = cy
        elif anim_type == 3:  # 放大 + 缓慢上移
            x_expr = cx
            y_expr = f"{cy}+{py:.1f}*(1-on/{d})"
        elif anim_type == 4:  # 放大 + 缓慢下移
            x_expr = cx
            y_expr = f"{cy}-{py:.1f}*(1-on/{d})"
        elif anim_type == 5:  # 放大 + 左上→右下
            x_expr = f"{cx}+{px:.1f}*(1-on/{d})"
            y_expr = f"{cy}+{py:.1f}*(1-on/{d})"
        elif anim_type == 6:  # 放大 + 右下→左上
            x_expr = f"{cx}-{px:.1f}*(1-on/{d})"
            y_expr = f"{cy}-{py:.1f}*(1-on/{d})"
        else:                 # type 0: 纯居中放大
            x_expr = cx
            y_expr = cy

        zoompan = (
            f"zoompan=z={z_expr}:x={x_expr}:y={y_expr}"
            f":d={d}:s={zw}x{zh}:fps={self.fps}"
        )

        drawtext = self._drawtext_expr(text)

        # 视频滤镜链：先缩放覆盖工作区 → 居中裁切 → Ken Burns 动画 → 缩放到目标尺寸 → 字幕
        vf = (
            f"scale={zw}:{zh}:force_original_aspect_ratio=increase,"
            f"crop={zw}:{zh},"
            f"{zoompan},"
            f"scale={self.width}:{self.height}:flags=lanczos,"
            f"{drawtext}"
        )

        output_path = str(Path(tmp_dir) / f"seg_{index:04d}.mp4")

        cmd = [
            self._ffmpeg,
            "-y",
            "-loop", "1",
            "-i", image_path,
        ]

        if audio_path:
            cmd += ["-i", audio_path]

        cmd += [
            "-vf", vf,
        ]

        if audio_path:
            # TTS 已输出统一的 24 kHz 单声道干净音频。这里不再逐段添加
            # 高通、频谱降噪或淡入淡出，避免不同句子产生可感知的音色漂移。
            cmd += ["-map", "0:v", "-map", "1:a"]

        cmd += [
            "-c:v", self._encoder,
        ]
        if self._use_crf:
            cmd += ["-preset", self.preset, "-crf", str(self.crf)]
        cmd += [
            "-pix_fmt", "yuv420p",
            "-profile:v", "baseline",
            "-level", "3.0",
        ]

        if audio_path:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "24000", "-ac", "1"]

        cmd += [
            "-t", f"{duration_s:.3f}",
            "-shortest",
            output_path,
        ]

        logger.debug(f"  [FFmpeg 段 {index+1}] 编码 {duration_s:.1f}s ...")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            logger.error(f"  [FFmpeg 段 {index+1}] 编码失败:\n{result.stderr[-500:]}")
            raise RuntimeError(f"FFmpeg segment {index} failed: {result.stderr[-200:]}")

        return output_path

    # ── 拼接 ──────────────────────────────────────────────────────

    def _concat_clips(self, clip_paths: List[str], output_path: str) -> str:
        """使用 concat demuxer 拼接视频片段（简单高效）"""
        if len(clip_paths) == 1:
            # 只有一段，直接复制
            shutil.copy2(clip_paths[0], output_path)
            return output_path

        logger.debug(f"  [FFmpeg] 拼接 {len(clip_paths)} 段视频")

        # 创建 concat 列表文件
        import tempfile
        concat_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        try:
            for clip in clip_paths:
                # 使用绝对路径，避免路径问题
                abs_path = str(Path(clip).resolve()).replace('\\', '/')
                concat_file.write(f"file '{abs_path}'\n")
            concat_file.close()

            # 使用 concat demuxer 拼接（无需重新编码，速度快）
            cmd = [
                self._ffmpeg, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file.name,
                "-c", "copy",
                "-movflags", "+faststart",
                output_path,
            ]

            logger.debug(f"  [FFmpeg] 执行拼接命令...")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                logger.error(f"  [FFmpeg] 拼接失败:\n{result.stderr[-500:]}")
                raise RuntimeError(f"FFmpeg concat failed: {result.stderr[-200:]}")

            return output_path

        finally:
            # 清理临时文件
            try:
                Path(concat_file.name).unlink()
            except Exception:
                pass

    # ── 主入口 ─────────────────────────────────────────────────────

    def export(
        self,
        segments: List[str],
        media_paths: List[str],
        voiceover_files: Optional[List[str]],
        output_path: str,
        animation_seed: Optional[int] = None,
        animation_params: Optional[List[dict]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> str:
        logger.info(f"[FFmpeg] 开始导出 MP4，共 {len(segments)} 段")

        def raise_if_cancelled() -> None:
            if should_cancel and should_cancel():
                raise RenderCancelled("视频生成已取消")

        raise_if_cancelled()

        anim_params = animation_params or build_animation_params(len(segments), animation_seed)

        tmp_dir = tempfile.mkdtemp(prefix="ffmpeg_export_")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            # 并行编码各段
            clip_paths = [None] * len(segments)

            def encode_one(i):
                # Futures are submitted together but queued work still checks the
                # shared cancellation flag before spending another FFmpeg call.
                raise_if_cancelled()
                vo = None
                if voiceover_files and i < len(voiceover_files):
                    vo = voiceover_files[i]
                param = anim_params[i]
                at = param["anim_type"] if isinstance(param, dict) else param[0]
                se = param["scale_end"] if isinstance(param, dict) else param[1]
                result = self._build_segment_clip(
                    index=i,
                    image_path=media_paths[i],
                    audio_path=vo,
                    text=segments[i],
                    anim_type=at,
                    scale_end=se,
                    tmp_dir=tmp_dir,
                )
                raise_if_cancelled()
                return i, result

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(encode_one, i) for i in range(len(segments))]
                for f in as_completed(futures):
                    idx, path = f.result()
                    clip_paths[idx] = path

            # 拼接
            raise_if_cancelled()
            self._concat_clips(clip_paths, output_path)
            raise_if_cancelled()
            logger.info(f"[FFmpeg] MP4 导出完成: {output_path}")
            return output_path

        finally:
            # 清理临时目录
            shutil.rmtree(tmp_dir, ignore_errors=True)

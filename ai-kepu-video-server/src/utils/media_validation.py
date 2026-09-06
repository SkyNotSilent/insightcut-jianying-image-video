"""Validate media by content, without restricting user-selected local locations."""
import json
import math
import re
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_BATCH_IMAGE_BYTES = 100 * 1024 * 1024


def validate_image(stream):
    stream.seek(0, 2)
    size = stream.tell()
    if not size or size > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 20 MiB")
    stream.seek(0)
    try:
        with Image.open(stream) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("只支持有效的 JPG、PNG、WEBP 图片")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片不能超过 6400 万像素")
            result = {"format": image.format, "width": image.width, "height": image.height, "file_size": size}
            image.verify()
        stream.seek(0)
        with Image.open(stream) as image:
            image.load()
        return result
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片内容损坏或格式无效") from exc
    finally:
        stream.seek(0)


def validate_asset_file(path, asset_type):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("素材文件不存在")
    if asset_type == "image":
        with path.open("rb") as stream:
            return validate_image(stream)
    if asset_type == "audio":
        try:
            result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
                capture_output=True, text=True, check=True, timeout=15)
            data = json.loads(result.stdout)
            duration = float(data.get("format", {}).get("duration") or 0)
            if not data.get("streams") or not math.isfinite(duration) or duration <= 0:
                raise ValueError("音频没有有效时长")
            return {"duration": duration}
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
            raise ValueError("音频无法解析，请重新选择有效配音") from exc
    if asset_type == "subtitle":
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("字幕文件过大")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeError, OSError) as exc:
            raise ValueError("字幕编码无效") from exc
        if path.suffix.lower() not in {".srt", ".vtt"} or not re.search(r"\d{2}:\d{2}[:.]\d{2}.*-->", text):
            raise ValueError("文件不是有效字幕")
        return {}
    raise ValueError("无法确认素材类型，请重新导入有效素材")

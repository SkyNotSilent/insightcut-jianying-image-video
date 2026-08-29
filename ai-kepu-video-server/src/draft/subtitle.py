"""Deterministic script-based subtitle rendering for delivery surfaces."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

from src.utils.subtitle_text import normalize_subtitle_text


class SubtitleWriter:
    """Render ordered storyboard text as SRT or WebVTT and publish atomically."""

    SUPPORTED_FORMATS = {"srt", "vtt"}

    def __init__(self, default_duration: float = 4.0):
        self.default_duration = max(0.001, float(default_duration))

    def _duration(self, segment: dict) -> float:
        value = segment.get("duration")
        if value in (None, ""):
            value = segment.get("duration_seconds")
        try:
            duration = float(value or 0)
        except (TypeError, ValueError):
            duration = 0
        return duration if duration > 0 else self.default_duration

    @staticmethod
    def _timestamp(seconds: float, separator: str) -> str:
        millis = int(max(0.0, float(seconds)) * 1000)
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        secs, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"

    @staticmethod
    def _ordered(segments: Iterable[dict]):
        return sorted(
            (dict(segment) for segment in (segments or [])),
            key=lambda segment: int(segment.get("segment_index") or 0),
        )

    def render(self, segments: Iterable[dict], format: str = "srt") -> str:
        format = str(format or "srt").lower()
        if format not in self.SUPPORTED_FORMATS:
            raise ValueError("字幕格式必须是 srt 或 vtt")
        separator = "," if format == "srt" else "."
        cursor = 0.0
        blocks = []
        for index, segment in enumerate(self._ordered(segments), start=1):
            start = cursor
            cursor += self._duration(segment)
            text = normalize_subtitle_text(segment.get("text") or "")
            blocks.append(
                f"{index}\n"
                f"{self._timestamp(start, separator)} --> "
                f"{self._timestamp(cursor, separator)}\n{text}\n"
            )
        body = "\n".join(blocks)
        return f"WEBVTT\n\n{body}" if format == "vtt" else body

    def write(self, path, segments: Iterable[dict], format: str = "srt") -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.render(segments, format))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise
        return target


__all__ = ["SubtitleWriter"]

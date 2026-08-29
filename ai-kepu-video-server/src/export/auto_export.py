"""Verified export orchestration shared by API delivery targets."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional

from src.draft.atomic_finalize import validate_staged_draft


class ExportVerificationError(RuntimeError):
    """The handler returned before publishing a usable output."""


def open_output_directory(path: Path) -> None:
    directory = Path(path).resolve()
    if not directory.is_dir():
        raise FileNotFoundError("输出目录不存在")
    system = platform.system().lower()
    if system == "windows":
        os.startfile(str(directory))  # type: ignore[attr-defined]
        return
    command = ["open", str(directory)] if system == "darwin" else ["xdg-open", str(directory)]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


class AutoExporter:
    """Dispatch real handlers, verify their artifacts, then optionally reveal them."""

    def __init__(
        self,
        handlers: Dict[str, Callable[[], dict]],
        *,
        directory_opener: Optional[Callable[[Path], None]] = None,
    ):
        self.handlers = dict(handlers or {})
        self.directory_opener = directory_opener or open_output_directory

    @staticmethod
    def _file(result: dict, key: str) -> Path:
        path = Path(str(result.get(key) or ""))
        if not result.get(key) or not path.is_file() or path.stat().st_size <= 0:
            raise ExportVerificationError(f"导出结果缺少有效文件: {key}")
        return path.resolve()

    @staticmethod
    def _draft(result: dict) -> Path:
        path = Path(str(result.get("draft_path") or ""))
        if not result.get("draft_path") or not path.is_dir():
            raise ExportVerificationError("导出结果缺少有效草稿目录")
        try:
            validate_staged_draft(path)
        except (OSError, ValueError) as error:
            raise ExportVerificationError("剪映草稿预检未通过") from error
        return path.resolve()

    def _verify(self, target: str, result: dict) -> Path:
        if not isinstance(result, dict):
            raise ExportVerificationError("导出处理器未返回结构化结果")
        if target == "mp4":
            return self._file(result, "video_path")
        if target == "materials":
            return self._file(result, "zip_path")
        if target == "draft":
            draft = self._draft(result)
            self._file(result, "zip_path")
            return draft
        if target == "draft_local":
            return self._draft(result)
        raise ExportVerificationError("不支持的导出类型")

    def export(self, target: str, *, reveal_output: bool = False) -> dict:
        handler = self.handlers.get(target)
        if handler is None:
            raise ExportVerificationError("不支持的导出类型")
        raw_result = handler()
        if not isinstance(raw_result, dict):
            raise ExportVerificationError("导出处理器未返回结构化结果")
        result = dict(raw_result)
        output = self._verify(target, result)
        warnings = list(result.get("warnings") or [])
        revealed = False
        if reveal_output:
            directory = output if output.is_dir() else output.parent
            try:
                self.directory_opener(directory)
                revealed = True
            except Exception:
                warnings.append("导出已完成，但无法自动打开输出目录。")
        result.update({
            "success": True,
            "verified": True,
            "revealed_output": revealed,
            "warnings": warnings,
        })
        return result


__all__ = ["AutoExporter", "ExportVerificationError", "open_output_directory"]

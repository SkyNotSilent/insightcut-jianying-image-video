"""
剪映草稿路径修复工具
- 将 draft_content.json 中的相对路径转换为绝对路径
- 生成 fix_paths.bat 修复脚本（兜底方案）
"""

import copy
import json
import time


def _clean_path(value: str) -> str:
    """移除用户复制路径时常带上的引号和尾部分隔符。"""
    return str(value or "").strip().strip("'\"").strip()


def _target_separator(target_os: str) -> str:
    return "/" if target_os == "mac" else "\\"


def normalize_extract_path(extract_path: str, target_os: str = "windows") -> str:
    """规范化用户填写的剪映草稿目录。只处理规则明确、不会改变语义的情况。"""
    target_os = "mac" if target_os == "mac" else "windows"
    path = _clean_path(extract_path)
    if not path:
        return ""
    if target_os == "mac":
        return path.replace("\\", "/").rstrip("/")
    return path.replace("/", "\\").rstrip("\\")


def validate_extract_path(extract_path: str, target_os: str = "windows") -> tuple[bool, str, list[str]]:
    """返回 (是否可用, 规范化路径, 问题列表)。"""
    target_os = "mac" if target_os == "mac" else "windows"
    normalized = normalize_extract_path(extract_path, target_os)
    issues = []
    raw = str(extract_path or "").strip()

    if not normalized:
        issues.append("请填写剪映草稿解压路径")
        return False, normalized, issues
    if raw and raw != _clean_path(raw):
        issues.append("路径两侧的引号会被自动移除")
    if target_os == "mac":
        if "\\" in raw:
            issues.append("检测到 Windows 反斜杠，已按 Mac 路径规范转换")
        if not normalized.startswith("/"):
            issues.append("Mac 路径必须以 / 开头")
    else:
        if "/" in raw:
            issues.append("检测到正斜杠，已按 Windows 路径规范转换")
        if not _is_target_absolute(normalized, "windows"):
            issues.append("Windows 路径必须是盘符路径或 UNC 路径，例如 D:\\JianyingPro Drafts")

    blocking = any("必须" in item or "请填写" in item for item in issues)
    return not blocking, normalized, issues


def _is_target_absolute(path: str, target_os: str) -> bool:
    if not path:
        return False
    if target_os == "mac":
        return path.startswith("/")
    return (
        len(path) >= 3
        and path[1] == ":"
        and path[2] in {"\\", "/"}
    ) or path.startswith("\\\\")


def _join_target_path(extract_path: str, draft_name: str, rel_path: str, target_os: str) -> str:
    sep = _target_separator(target_os)
    base = normalize_extract_path(extract_path, target_os)
    clean_rel = _clean_path(rel_path).lstrip("/\\").replace("\\", sep).replace("/", sep)
    return sep.join(part for part in [base, draft_name, clean_rel] if part)


def _join_target_dir(extract_path: str, draft_name: str, target_os: str) -> str:
    sep = _target_separator(target_os)
    base = normalize_extract_path(extract_path, target_os)
    return sep.join(part for part in [base, draft_name] if part)


def _material_rel_path(path: str, default_dir: str) -> str:
    """把素材路径压回草稿内相对路径，避免旧绝对路径污染新位置。"""
    clean = _clean_path(path).replace("\\", "/")
    if not clean:
        return ""
    parts = [part for part in clean.split("/") if part]
    for dirname in ("images", "voiceovers", "audios"):
        if dirname in parts:
            idx = parts.index(dirname)
            return "/".join(parts[idx:])
    return "/".join([default_dir, parts[-1]])


def apply_extract_path(
    draft_json: dict,
    extract_path: str,
    draft_name: str,
    target_os: str = "windows",
    force: bool = False,
) -> dict:
    r"""将 draft_content.json 中的相对路径转为基于 extract_path 的绝对路径。

    target_os=windows: C:\...\草稿名\images\seg_000.png
    target_os=mac: /Users/.../草稿名/images/seg_000.png
    """
    data = copy.deepcopy(draft_json)
    target_os = "mac" if target_os == "mac" else "windows"

    for video in data.get("materials", {}).get("videos", []):
        path = _clean_path(video.get("path", ""))
        if path and (force or not _is_target_absolute(path, target_os)):
            video["path"] = _join_target_path(
                extract_path,
                draft_name,
                _material_rel_path(path, "images"),
                target_os,
            )

    for audio in data.get("materials", {}).get("audios", []):
        path = _clean_path(audio.get("path", ""))
        if path and (force or not _is_target_absolute(path, target_os)):
            audio["path"] = _join_target_path(
                extract_path,
                draft_name,
                _material_rel_path(path, "voiceovers"),
                target_os,
            )

    return data


def apply_meta_info(meta_json: dict, extract_path: str, draft_name: str, target_os: str = "windows", now_us: int = None) -> dict:
    """补齐剪映草稿列表依赖的 meta 字段。"""
    data = copy.deepcopy(meta_json)
    target_os = "mac" if target_os == "mac" else "windows"
    root_path = normalize_extract_path(extract_path, target_os)
    draft_path = _join_target_dir(root_path, draft_name, target_os)

    data["draft_name"] = draft_name
    data["draft_root_path"] = root_path
    data["draft_fold_path"] = draft_path
    data.setdefault("draft_need_rename_folder", False)
    data.setdefault("draft_is_cloud_temp_draft", False)
    data.setdefault("cloud_draft_sync", False)
    data.setdefault("cloud_draft_cover", False)
    data.setdefault("tm_draft_cloud_entry_id", -1)
    data.setdefault("tm_draft_cloud_parent_entry_id", -1)
    data.setdefault("tm_draft_cloud_space_id", -1)
    data.setdefault("tm_draft_cloud_user_id", -1)

    now_us = now_us or int(time.time() * 1_000_000)
    data["tm_draft_create"] = data.get("tm_draft_create") or now_us
    data["tm_draft_modified"] = now_us

    return data


def apply_content_info(
    content_json: dict,
    draft_name: str,
    target_os: str = "windows",
    now_us: int = None,
) -> dict:
    """补齐 draft_content.json 中剪映首页和工程打开依赖的基础字段。"""
    data = copy.deepcopy(content_json)
    target_os = "mac" if target_os == "mac" else "windows"
    now_us = now_us or int(time.time() * 1_000_000)

    data["name"] = draft_name
    data["create_time"] = data.get("create_time") or now_us
    data["update_time"] = now_us

    platform = data.get("platform")
    if isinstance(platform, dict):
        platform["os"] = target_os
    else:
        data["platform"] = {"os": target_os}

    return data


def generate_fix_bat() -> str:
    """生成 fix_paths.bat 修复脚本。

    用户解压到非预期位置时，双击此脚本即可将 draft_content.json
    中的素材路径替换为当前文件夹的绝对路径。
    """
    return r'''@echo off
chcp 65001 >nul
echo ===================================
echo  剪映草稿路径修复工具
echo ===================================
echo.

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "JSON_FILE=%SCRIPT_DIR%\draft_content.json"

if not exist "%JSON_FILE%" (
    echo [错误] 未找到 draft_content.json，请确保此脚本与草稿文件在同一目录。
    pause
    exit /b 1
)

echo 当前目录: %SCRIPT_DIR%
echo 正在修复路径...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$dir = '%SCRIPT_DIR%';" ^
    "$jsonPath = '%JSON_FILE%';" ^
    "$data = Get-Content $jsonPath -Raw -Encoding UTF8;" ^
    "$json = $data | ConvertFrom-Json;" ^
    "$changed = 0;" ^
    "foreach ($v in $json.materials.videos) {" ^
    "  if ($v.path -and -not [System.IO.Path]::IsPathRooted($v.path)) {" ^
    "    $v.path = [System.IO.Path]::Combine($dir, $v.path).Replace('/', '\');" ^
    "    $changed++;" ^
    "  }" ^
    "}" ^
    "foreach ($a in $json.materials.audios) {" ^
    "  if ($a.path -and -not [System.IO.Path]::IsPathRooted($a.path)) {" ^
    "    $a.path = [System.IO.Path]::Combine($dir, $a.path).Replace('/', '\');" ^
    "    $changed++;" ^
    "  }" ^
    "}" ^
    "$json | ConvertTo-Json -Depth 100 | Set-Content $jsonPath -Encoding UTF8;" ^
    "Write-Host \"已修复 $changed 个路径\";"

echo.
echo 修复完成！现在可以在剪映中打开此草稿。
pause
'''


def generate_fix_sh() -> str:
    """生成 macOS/Linux 兜底路径修复脚本。"""
    return r'''#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JSON_FILE="$SCRIPT_DIR/draft_content.json"

if [ ! -f "$JSON_FILE" ]; then
  echo "[错误] 未找到 draft_content.json，请确保此脚本与草稿文件在同一目录。"
  exit 1
fi

python3 - "$JSON_FILE" "$SCRIPT_DIR" <<'PY'
import json
import sys
from pathlib import Path

json_path = Path(sys.argv[1])
base = Path(sys.argv[2])
data = json.loads(json_path.read_text(encoding="utf-8"))
changed = 0

for group in ("videos", "audios"):
    for item in data.get("materials", {}).get(group, []):
        path = str(item.get("path") or "").strip().strip("'\"")
        if path and not path.startswith("/"):
            item["path"] = str(base / path.replace("\\", "/"))
            changed += 1

json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已修复 {changed} 个路径")
PY

echo "修复完成！现在可以在剪映中打开此草稿。"
'''

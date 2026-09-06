#!/usr/bin/env python3
"""Report local storage/log/database health and optionally delete unreferenced media.

Default mode is read-only. Use --apply to delete only files that are not referenced
by the SQLite database.
"""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.utils.file_lock import file_lock

SERVER_ROOT = Path(os.environ.get("INSIGHTCUT_SERVER_ROOT") or os.environ.get("INSIGHTCUT_DATA_ROOT") or Path(__file__).resolve().parents[1]).resolve()
DB_PATH = Path(os.environ.get("INSIGHTCUT_DB_PATH") or SERVER_ROOT / "data" / "local.db")
OUTPUT_DIR = SERVER_ROOT / "output"
MEDIA_DIR = SERVER_ROOT / "data" / "media"
LOG_DIR = SERVER_ROOT / "logs"


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(file_size(item) for item in path.rglob("*") if item.is_file())


def iter_files(path: Path):
    if not path.exists():
        return
    for item in path.rglob("*"):
        if not item.is_symlink() and item.is_file() and not any(part.is_symlink() for part in item.parents):
            yield item.resolve()


def local_path_from_db_value(value: str):
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    if raw.startswith(("http://", "https://", "/media/")):
        parsed = urlparse(raw)
        marker = "/media/"
        if marker not in parsed.path:
            return None
        relative = parsed.path.split(marker, 1)[1].lstrip("/")
        preferred = OUTPUT_DIR / relative
        return (preferred if preferred.exists() else MEDIA_DIR / relative).resolve()

    path = Path(raw)
    if not path.is_absolute():
        path = SERVER_ROOT / path
    return path.resolve()


def collect_referenced_paths():
    referenced = set()
    if not DB_PATH.exists():
        raise RuntimeError("数据库不存在，无法确定引用；不执行删除")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        queries = [
            ("task_segments", ["image_path", "image_url", "audio_path", "audio_url"]),
            ("task_assets", ["path", "url"]),
            ("task_results", ["draft_path", "draft_url", "video_url"]),
            ("tts_voice_clones", ["reference_path", "preview_path"]),
        ]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in queries:
            selected = ", ".join(columns)
            for row in conn.execute(f"SELECT {selected} FROM {table}"):
                for column in columns:
                    local_path = local_path_from_db_value(row[column])
                    if local_path:
                        referenced.add(local_path)
                        for root, alternate in ((OUTPUT_DIR, MEDIA_DIR), (MEDIA_DIR, OUTPUT_DIR)):
                            if is_within(local_path, root):
                                referenced.add((alternate / local_path.relative_to(root.resolve())).resolve())
        def collect_nested(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if (key.endswith(("_path", "_url")) or key in {"path", "url"}) and isinstance(item, str):
                        local_path = local_path_from_db_value(item)
                        if local_path:
                            referenced.add(local_path)
                    else:
                        collect_nested(item)
            elif isinstance(value, list):
                for item in value:
                    collect_nested(item)
        for table, column in (("task_plan_revisions", "snapshot_json"), ("export_jobs", "job_json")):
            if table in tables:
                for row in conn.execute(f"SELECT {column} FROM {table}"):
                    collect_nested(json.loads(row[0]))
        for directory in list(referenced):
            if directory.is_dir() and any(is_within(directory, root) for root in (OUTPUT_DIR, MEDIA_DIR)):
                referenced.update(iter_files(directory))
    finally:
        conn.close()
    return referenced


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def unreferenced_media_files(referenced):
    if OUTPUT_DIR.is_symlink() or MEDIA_DIR.is_symlink():
        raise RuntimeError("媒体根目录是符号链接，不执行删除")
    roots = [OUTPUT_DIR.resolve(), MEDIA_DIR.resolve()]
    candidates = []
    for root in roots:
        for path in iter_files(root):
            if path not in referenced and ".insightcut" not in path.parts:
                candidates.append(path)
    return sorted(candidates)


def cleanup_empty_dirs(root: Path):
    removed = []
    if not root.exists():
        return removed
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            path.rmdir()
            removed.append(str(path.relative_to(SERVER_ROOT)))
        except OSError:
            pass
    return removed


def db_counts():
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        tables = ["tasks", "task_steps", "task_segments", "task_assets", "task_results", "tts_voices"]
        counts = {}
        for table in tables:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return counts
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report only; this is the default.")
    parser.add_argument("--apply", action="store_true", help="Delete unreferenced media files.")
    parser.add_argument("--max-items", type=int, default=50, help="Maximum unreferenced files to list.")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply cannot be used together")

    try:
        lock = file_lock(SERVER_ROOT / "data" / "maintenance.lock", blocking=False) if args.apply else nullcontext()
        with lock:
            run_report(args)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        parser.exit(1, "无法安全清理：服务可能正在运行，或引用数据不可用。" + str(error) + "\n")


def run_report(args):
    referenced = collect_referenced_paths()
    unreferenced = unreferenced_media_files(referenced)
    unreferenced_bytes = sum(file_size(path) for path in unreferenced)
    deleted = []
    failed = []
    removed_dirs = []

    if args.apply:
        for path in unreferenced:
            if not (is_within(path, OUTPUT_DIR) or is_within(path, MEDIA_DIR)):
                continue
            try:
                path.unlink()
                deleted.append(str(path.relative_to(SERVER_ROOT)))
            except OSError as error:
                failed.append({"path": str(path.relative_to(SERVER_ROOT)), "reason": type(error).__name__})
        removed_dirs = cleanup_empty_dirs(OUTPUT_DIR) + cleanup_empty_dirs(MEDIA_DIR)

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "sizes_bytes": {
            "logs": tree_size(LOG_DIR),
            "output": tree_size(OUTPUT_DIR),
            "data_media": tree_size(MEDIA_DIR),
            "database": file_size(DB_PATH),
        },
        "database_counts": db_counts(),
        "referenced_paths": len(referenced),
        "unreferenced_media": {
            "count": len(unreferenced),
            "bytes": unreferenced_bytes,
            "sample": [str(path.relative_to(SERVER_ROOT)) for path in unreferenced[: args.max_items]],
        },
        "preserved_file_count": sum(1 for root in (OUTPUT_DIR, MEDIA_DIR) for path in iter_files(root) if path in referenced),
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "failed": failed,
        "deleted": deleted,
        "removed_empty_dirs": removed_dirs,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

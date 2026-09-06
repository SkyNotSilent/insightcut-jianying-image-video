"""Publish a fully prepared draft on the destination volume without losing edits."""
import json
import os
import shutil
import uuid
from pathlib import Path

from src.utils.file_lock import file_lock


def _write_journal(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _recover(root):
    for journal in (root / ".insightcut" / "publications").glob("*.json"):
        state = json.loads(journal.read_text(encoding="utf-8"))
        target, staging = Path(state["target"]), Path(state["staging"])
        backup = Path(state["backup"]) if state.get("backup") else None
        # Journals may have been edited externally. Never act outside this root.
        if any(not p.resolve().is_relative_to(root) for p in (target, staging, backup) if p):
            raise RuntimeError("草稿恢复记录包含无效位置")
        if state["phase"] != "committed":
            if backup and backup.exists():
                if target.exists():
                    failed = staging.with_name(staging.name + "-unverified")
                    os.replace(target, failed)
                os.replace(backup, target)
            elif state["phase"] == "publishing" and not staging.exists() and target.exists():
                # No prior draft: retain the unverified output separately for inspection.
                os.replace(target, staging)
        journal.unlink()


def publish_draft(source, root, *, policy="copy", prepare, verify, should_cancel=None):
    source, root = Path(source).resolve(), Path(root).resolve()
    if policy not in {"copy", "backup_replace", "error"}:
        raise ValueError("无效的同名草稿处理方式")
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / ".insightcut" / "publication.lock"):
        _recover(root)
        name = source.name
        target = root / name
        if policy == "copy":
            suffix = 2
            while target.exists():
                target = root / f"{name}（{suffix}）"
                suffix += 1
        elif target.exists() and policy == "error":
            raise FileExistsError(f"剪映草稿已存在：{target}")
        run = uuid.uuid4().hex
        staging = root / ".insightcut" / "staging" / run
        backup = root / ".insightcut" / "backups" / f"{target.name}-{run}" if target.exists() else None
        journal = root / ".insightcut" / "publications" / f"{run}.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        state = {"target": str(target), "staging": str(staging), "backup": str(backup) if backup else None, "phase": "preparing"}
        def check():
            if should_cancel:
                should_cancel()
        check()
        try:
            shutil.copytree(source, staging, ignore=lambda _, names: {n for n in names if n == "previews" or n.endswith((".zip", ".mp4"))})
            prepare(staging, staging)
            verify(staging)
            prepare(staging, target)
            check()
            _write_journal(journal, state)
            if backup:
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
            state["phase"] = "publishing"
            _write_journal(journal, state)
            check()
            os.replace(staging, target)
            verify(target)
            check()
            state["phase"] = "committed"
            _write_journal(journal, state)
            journal.unlink()
            return {"draft_path": str(target), "draft_name": target.name, "backup_path": str(backup) if backup else None}
        except BaseException:
            if journal.exists():
                _recover(root)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

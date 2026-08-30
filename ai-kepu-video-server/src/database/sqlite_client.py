"""
SQLite 数据库客户端
本地持久化任务、分镜和素材数据
"""

import logging
import sqlite3
import os
import uuid
import json
import threading
from datetime import datetime
from typing import Optional, List, Dict
from contextlib import contextmanager
from pathlib import Path

from src.api.error_model import (
    ErrorCode,
    normalize_error_code,
    normalize_error_metadata,
    sanitize_persisted_error_text,
)
from src.draft.voice_catalog import (
    DOUBAO_DEFAULT_ENABLED_IDS,
    DOUBAO_PRESET_VOICES,
    MIMO_DEFAULT_ENABLED_IDS,
    MIMO_PRESET_VOICES,
    build_voice_key,
    parse_voice_key,
)

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX platforms
    msvcrt = None

DB_PATH = Path(
    os.getenv("INSIGHTCUT_DB_PATH")
    or Path(__file__).parent.parent.parent / "data" / "local.db"
).expanduser().resolve()


class SQLiteClient:
    """SQLite 数据库客户端"""

    TASK_CHECKPOINT_COLUMNS = frozenset({
        "script_text", "script_source", "summary", "input_mode", "delete_files_on_delete",
        "execution_mode", "workflow_phase", "script_policy", "voice_confirmed",
        "error_code", "error_meta_json", "source_draft_id", "template_id",
        "generation_options_json", "subtitle_options_json",
    })
    SEGMENT_CHECKPOINT_COLUMNS = frozenset({
        "text", "image_prompt", "image_path", "image_url", "image_status",
        "image_error", "audio_path", "audio_url", "audio_status", "audio_error",
        "duration", "audio_voice_type", "audio_tts_options_json",
        "prompt_status", "prompt_error", "prompt_manual", "prompt_needs_review",
        "prompt_error_code", "prompt_error_meta_json",
        "image_error_code", "image_error_meta_json",
        "audio_error_code", "audio_error_meta_json",
        "selected_image_asset_id", "selected_audio_asset_id",
        "audio_mismatch_confirmed",
    })
    CLEARABLE_SEGMENT_ERROR_COLUMNS = frozenset({
        "image_error", "audio_error", "prompt_error",
        "prompt_error_code", "prompt_error_meta_json",
        "image_error_code", "image_error_meta_json",
        "audio_error_code", "audio_error_meta_json",
    })

    def __init__(self):
        self._initialized = False
        self._batch_scheduler_lock_guard = threading.Lock()
        self._batch_scheduler_lock_handles = {}
        self._batch_launch_thread_lock = threading.RLock()
        self._batch_launch_local = threading.local()

    def _init_db(self):
        """初始化数据库和表结构"""
        if self._initialized:
            return

        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    name TEXT,
                    theme TEXT NOT NULL,
                    style TEXT NOT NULL DEFAULT '温暖感人',
                    length INTEGER NOT NULL DEFAULT 300,
                    ratio TEXT NOT NULL DEFAULT '16:9',
                    voice_type TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    current_step TEXT DEFAULT 'pending',
                    error TEXT,
                    error_code TEXT,
                    error_meta_json TEXT,
                    extract_path TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    completed_at TEXT,
                    exported_at TEXT,
                    last_export_target TEXT,
                    delete_files_on_delete INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS task_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    draft_path TEXT NOT NULL,
                    draft_url TEXT,
                    video_url TEXT,
                    segments_count INTEGER NOT NULL DEFAULT 0,
                    total_duration REAL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS task_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress INTEGER,
                    total INTEGER,
                    duration REAL,
                    started_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS tts_voices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL DEFAULT 'doubao',
                    voice_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    gender TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'zh',
                    description TEXT,
                    source TEXT NOT NULL DEFAULT 'builtin',
                    capabilities_json TEXT,
                    preview_url TEXT,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    UNIQUE(provider, voice_id)
                );

                CREATE TABLE IF NOT EXISTS tts_voice_clones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clone_id TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL DEFAULT 'mimo',
                    name TEXT NOT NULL,
                    reference_path TEXT NOT NULL,
                    duration REAL,
                    file_size INTEGER,
                    status TEXT NOT NULL DEFAULT 'draft',
                    preview_path TEXT,
                    error_message TEXT,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    consent_confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS task_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    image_prompt TEXT,
                    image_path TEXT,
                    image_url TEXT,
                    image_status TEXT DEFAULT 'completed',
                    image_error TEXT,
                    image_error_code TEXT,
                    image_error_meta_json TEXT,
                    audio_path TEXT,
                    audio_url TEXT,
                    audio_status TEXT DEFAULT 'completed',
                    audio_error TEXT,
                    audio_error_code TEXT,
                    audio_error_meta_json TEXT,
                    prompt_error_code TEXT,
                    prompt_error_meta_json TEXT,
                    duration REAL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    UNIQUE(task_id, segment_index)
                );

                CREATE TABLE IF NOT EXISTS task_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    segment_index INTEGER,
                    asset_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    path TEXT,
                    url TEXT,
                    label TEXT,
                    prompt TEXT,
                    text TEXT,
                    voice_type TEXT,
                    metadata_json TEXT,
                    operation_id TEXT,
                    origin_asset_id TEXT,
                    snapshot_json TEXT,
                    status TEXT DEFAULT 'completed',
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS production_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT,
                    visual_style TEXT NOT NULL DEFAULT '电影质感',
                    text_style TEXT NOT NULL DEFAULT '知识科普',
                    ratio TEXT NOT NULL DEFAULT '16:9',
                    voice_type TEXT,
                    tts_options_json TEXT,
                    subtitle_options_json TEXT NOT NULL DEFAULT '{}',
                    generation_options_json TEXT NOT NULL DEFAULT '{}',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS task_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    snapshot_key TEXT,
                    targets_json TEXT NOT NULL DEFAULT '[]',
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    error_code TEXT,
                    error_meta_json TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
            """)

            self._migrate_voice_catalog(cursor)
            self._apply_migration(
                cursor,
                "20260716_replace_ungranted_doubao_voice",
                """
                DELETE FROM tts_voices
                WHERE provider = 'doubao'
                  AND voice_id = 'zh_male_yangguangxiaolei_moon_bigtts'
                  AND source = 'builtin';
                UPDATE tts_voices
                SET is_enabled = 1,
                    updated_at = datetime('now','localtime')
                WHERE provider = 'doubao'
                  AND voice_id = 'zh_male_jieshuoxiaoming_moon_bigtts';
                """,
            )
            self._seed_voice_catalog(cursor)

            # 为已有数据库添加 ratio 字段（兼容旧表结构）
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN ratio TEXT NOT NULL DEFAULT '16:9'")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN voice_type TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            try:
                cursor.execute("ALTER TABLE task_segments ADD COLUMN image_prompt TEXT")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            try:
                cursor.execute("ALTER TABLE task_segments ADD COLUMN image_status TEXT DEFAULT 'completed'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE task_segments ADD COLUMN image_error TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE task_segments ADD COLUMN audio_status TEXT DEFAULT 'completed'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE task_segments ADD COLUMN audio_error TEXT")
            except sqlite3.OperationalError:
                pass

            self._apply_migration(
                cursor,
                "20260623_operational_indexes",
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_status_created_at
                    ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_steps_task_step
                    ON task_steps(task_id, step_name);
                CREATE INDEX IF NOT EXISTS idx_task_assets_task_type_segment
                    ON task_assets(task_id, asset_type, segment_index);
                """,
            )
            self._apply_column_migration(
                cursor,
                "20260711_task_recovery_checkpoints",
                "tasks",
                {
                    "script_text": "TEXT",
                    "summary": "TEXT",
                    "input_mode": "TEXT NOT NULL DEFAULT 'script'",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260829_legacy_script_recovery_source",
                "tasks",
                {"script_source": "TEXT"},
            )
            self._backfill_legacy_script_text(cursor)
            self._apply_column_migration(
                cursor,
                "20260823_global_shell_task_snapshots",
                "tasks",
                {
                    "source_draft_id": "TEXT",
                    "template_id": "TEXT",
                    "generation_options_json": "TEXT",
                    "subtitle_options_json": "TEXT",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260823_segment_asset_selection",
                "task_segments",
                {
                    "selected_image_asset_id": "TEXT",
                    "selected_audio_asset_id": "TEXT",
                    "audio_mismatch_confirmed": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260823_immutable_asset_metadata",
                "task_assets",
                {
                    "operation_id": "TEXT",
                    "origin_asset_id": "TEXT",
                    "snapshot_json": "TEXT",
                },
            )
            self._apply_migration(
                cursor,
                "20260823_production_templates",
                """
                CREATE TABLE IF NOT EXISTS production_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT,
                    visual_style TEXT NOT NULL DEFAULT '电影质感',
                    text_style TEXT NOT NULL DEFAULT '知识科普',
                    ratio TEXT NOT NULL DEFAULT '16:9',
                    voice_type TEXT,
                    tts_options_json TEXT,
                    subtitle_options_json TEXT NOT NULL DEFAULT '{}',
                    generation_options_json TEXT NOT NULL DEFAULT '{}',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_templates_default_updated
                    ON production_templates(is_default, updated_at);
                CREATE INDEX IF NOT EXISTS idx_assets_task_origin
                    ON task_assets(task_id, origin_asset_id);
                """,
            )
            self._apply_migration(
                cursor,
                "20260823_dedupe_legacy_asset_backfill",
                """
                UPDATE task_segments
                SET selected_image_asset_id = (
                    SELECT canonical.asset_id
                    FROM task_assets duplicate
                    JOIN task_assets canonical
                      ON canonical.task_id = duplicate.task_id
                     AND canonical.asset_type = duplicate.asset_type
                     AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                     AND canonical.path = duplicate.path
                     AND canonical.asset_id <> duplicate.asset_id
                    WHERE duplicate.asset_id = task_segments.selected_image_asset_id
                      AND duplicate.source = 'legacy'
                    ORDER BY CASE WHEN canonical.source = 'legacy' THEN 1 ELSE 0 END,
                             canonical.id ASC
                    LIMIT 1
                )
                WHERE selected_image_asset_id IN (
                    SELECT duplicate.asset_id
                    FROM task_assets duplicate
                    WHERE duplicate.source = 'legacy'
                      AND EXISTS (
                          SELECT 1 FROM task_assets canonical
                          WHERE canonical.task_id = duplicate.task_id
                            AND canonical.asset_type = duplicate.asset_type
                            AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                            AND canonical.path = duplicate.path
                            AND canonical.asset_id <> duplicate.asset_id
                      )
                );

                UPDATE task_segments
                SET selected_audio_asset_id = (
                    SELECT canonical.asset_id
                    FROM task_assets duplicate
                    JOIN task_assets canonical
                      ON canonical.task_id = duplicate.task_id
                     AND canonical.asset_type = duplicate.asset_type
                     AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                     AND canonical.path = duplicate.path
                     AND canonical.asset_id <> duplicate.asset_id
                    WHERE duplicate.asset_id = task_segments.selected_audio_asset_id
                      AND duplicate.source = 'legacy'
                    ORDER BY CASE WHEN canonical.source = 'legacy' THEN 1 ELSE 0 END,
                             canonical.id ASC
                    LIMIT 1
                )
                WHERE selected_audio_asset_id IN (
                    SELECT duplicate.asset_id
                    FROM task_assets duplicate
                    WHERE duplicate.source = 'legacy'
                      AND EXISTS (
                          SELECT 1 FROM task_assets canonical
                          WHERE canonical.task_id = duplicate.task_id
                            AND canonical.asset_type = duplicate.asset_type
                            AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                            AND canonical.path = duplicate.path
                            AND canonical.asset_id <> duplicate.asset_id
                      )
                );

                DELETE FROM task_assets AS duplicate
                WHERE duplicate.source = 'legacy'
                  AND duplicate.path IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM task_assets canonical
                      WHERE canonical.task_id = duplicate.task_id
                        AND canonical.asset_type = duplicate.asset_type
                        AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                        AND canonical.path = duplicate.path
                        AND canonical.asset_id <> duplicate.asset_id
                        AND (canonical.source <> 'legacy' OR canonical.id < duplicate.id)
                  );
                """,
            )
            self._apply_migration(
                cursor,
                "20260823_asset_path_identity",
                """
                CREATE TEMP TABLE legacy_asset_replacements (
                    duplicate_asset_id TEXT PRIMARY KEY,
                    canonical_asset_id TEXT NOT NULL
                );
                INSERT INTO legacy_asset_replacements
                    (duplicate_asset_id, canonical_asset_id)
                SELECT duplicate.asset_id,
                       (
                           SELECT canonical.asset_id
                           FROM task_assets canonical
                           WHERE canonical.task_id = duplicate.task_id
                             AND canonical.asset_type = duplicate.asset_type
                             AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                             AND canonical.path = duplicate.path
                             AND canonical.asset_id <> duplicate.asset_id
                           ORDER BY CASE WHEN canonical.source = 'legacy' THEN 1 ELSE 0 END,
                                    canonical.id ASC
                           LIMIT 1
                       )
                FROM task_assets duplicate
                WHERE duplicate.source = 'legacy'
                  AND duplicate.path IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM task_assets canonical
                      WHERE canonical.task_id = duplicate.task_id
                        AND canonical.asset_type = duplicate.asset_type
                        AND COALESCE(canonical.segment_index, -1) = COALESCE(duplicate.segment_index, -1)
                        AND canonical.path = duplicate.path
                        AND canonical.asset_id <> duplicate.asset_id
                        AND (canonical.source <> 'legacy' OR canonical.id < duplicate.id)
                  );
                UPDATE task_segments
                SET selected_image_asset_id = (
                    SELECT canonical_asset_id
                    FROM legacy_asset_replacements
                    WHERE duplicate_asset_id = task_segments.selected_image_asset_id
                )
                WHERE selected_image_asset_id IN (
                    SELECT duplicate_asset_id FROM legacy_asset_replacements
                );
                UPDATE task_segments
                SET selected_audio_asset_id = (
                    SELECT canonical_asset_id
                    FROM legacy_asset_replacements
                    WHERE duplicate_asset_id = task_segments.selected_audio_asset_id
                )
                WHERE selected_audio_asset_id IN (
                    SELECT duplicate_asset_id FROM legacy_asset_replacements
                );
                DELETE FROM task_assets
                WHERE asset_id IN (
                    SELECT duplicate_asset_id FROM legacy_asset_replacements
                );
                DROP TABLE legacy_asset_replacements;
                CREATE INDEX IF NOT EXISTS idx_task_assets_path_lookup
                    ON task_assets(
                        task_id,
                        asset_type,
                        COALESCE(segment_index, -1),
                        path
                    )
                    WHERE path IS NOT NULL;
                """,
            )
            self._apply_migration(
                cursor,
                "20260819_task_operations",
                """
                CREATE TABLE IF NOT EXISTS task_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    snapshot_key TEXT,
                    targets_json TEXT NOT NULL DEFAULT '[]',
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    error_code TEXT,
                    error_meta_json TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE INDEX IF NOT EXISTS idx_task_operations_task_state
                    ON task_operations(task_id, state, updated_at);
                """,
            )
            self._apply_migration(
                cursor,
                "20260828_batch_planning",
                """
                CREATE TABLE IF NOT EXISTS task_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    concurrency INTEGER NOT NULL DEFAULT 1 CHECK(concurrency BETWEEN 1 AND 3),
                    config_json TEXT NOT NULL DEFAULT '{}',
                    total_count INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS task_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL UNIQUE,
                    batch_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT,
                    theme TEXT NOT NULL,
                    normalized_theme TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    task_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    error_code TEXT,
                    error_meta_json TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(batch_id, normalized_theme),
                    UNIQUE(batch_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_task_batches_status_fifo
                    ON task_batches(status, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_task_batch_items_dispatch
                    ON task_batch_items(status, batch_id, position, id);
                CREATE INDEX IF NOT EXISTS idx_task_batch_items_task
                    ON task_batch_items(task_id);
                """,
            )
            self._apply_column_migration(
                cursor,
                "20260829_task_export_state",
                "tasks",
                {
                    "exported_at": "TEXT",
                    "last_export_target": "TEXT",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260716_tts_task_options",
                "tasks",
                {"tts_options_json": "TEXT"},
            )
            self._apply_column_migration(
                cursor,
                "20260716_segment_tts_options",
                "task_segments",
                {
                    "audio_voice_type": "TEXT",
                    "audio_tts_options_json": "TEXT",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260712_task_deletion_intent",
                "tasks",
                {"delete_files_on_delete": "INTEGER NOT NULL DEFAULT 0"},
            )
            self._apply_column_migration(
                cursor,
                "20260816_review_first_workspace_tasks",
                "tasks",
                {
                    "execution_mode": "TEXT NOT NULL DEFAULT 'full'",
                    "workflow_phase": "TEXT NOT NULL DEFAULT 'pending'",
                    "plan_version": "INTEGER NOT NULL DEFAULT 0",
                    "script_policy": "TEXT NOT NULL DEFAULT 'rewrite'",
                    "voice_confirmed": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260816_review_first_workspace_segments",
                "task_segments",
                {
                    "prompt_status": "TEXT NOT NULL DEFAULT 'completed'",
                    "prompt_error": "TEXT",
                    "prompt_manual": "INTEGER NOT NULL DEFAULT 0",
                    "prompt_needs_review": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260819_structured_errors_tasks",
                "tasks",
                {
                    "error_code": "TEXT",
                    "error_meta_json": "TEXT",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260819_structured_errors_segments",
                "task_segments",
                {
                    "prompt_error_code": "TEXT",
                    "prompt_error_meta_json": "TEXT",
                    "image_error_code": "TEXT",
                    "image_error_meta_json": "TEXT",
                    "audio_error_code": "TEXT",
                    "audio_error_meta_json": "TEXT",
                },
            )
            self._apply_column_migration(
                cursor,
                "20260819_structured_errors_operations",
                "task_operations",
                {
                    "error_code": "TEXT",
                    "error_meta_json": "TEXT",
                },
            )
            self._sanitize_legacy_error_storage(cursor)

            conn.commit()
            conn.close()
            self._initialized = True
            logger.info(f"SQLite 数据库初始化成功: {DB_PATH}")
        except Exception as e:
            logger.error(f"SQLite 数据库初始化失败: {e}")
            self._initialized = False

    def _apply_migration(self, cursor, version: str, sql: str) -> None:
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,))
        if cursor.fetchone():
            return
        cursor.executescript(sql)
        cursor.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        logger.info(f"SQLite 迁移已应用: {version}")

    def _migrate_voice_catalog(self, cursor) -> None:
        cursor.execute("PRAGMA table_info(tts_voices)")
        columns = {row[1] for row in cursor.fetchall()}
        if "provider" in columns and "language" in columns and "source" in columns:
            return
        cursor.executescript(
            """
            ALTER TABLE tts_voices RENAME TO tts_voices_legacy_20260716;
            CREATE TABLE tts_voices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL DEFAULT 'doubao',
                voice_id TEXT NOT NULL,
                name TEXT NOT NULL,
                gender TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'zh',
                description TEXT,
                source TEXT NOT NULL DEFAULT 'builtin',
                capabilities_json TEXT,
                preview_url TEXT,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(provider, voice_id)
            );
            INSERT INTO tts_voices
                (id, provider, voice_id, name, gender, language, description, source,
                 is_enabled, sort_order, created_at, updated_at)
            SELECT id, 'doubao', voice_id, name, gender, 'zh', description, 'builtin',
                   CASE WHEN voice_id IN (
                       'zh_female_shuangkuaisisi_moon_bigtts',
                       'zh_male_jieshuoxiaoming_moon_bigtts'
                   ) THEN 1 ELSE 0 END,
                   sort_order, created_at, updated_at
            FROM tts_voices_legacy_20260716;
            DROP TABLE tts_voices_legacy_20260716;
            """
        )

    def _seed_voice_catalog(self, cursor) -> None:
        capabilities = json.dumps(
            {"preview": True, "speed": "numeric", "volume": True},
            ensure_ascii=False,
        )
        for voice_id, name, gender, description, sort_order in DOUBAO_PRESET_VOICES:
            cursor.execute(
                """INSERT OR IGNORE INTO tts_voices
                   (provider, voice_id, name, gender, language, description, source,
                    capabilities_json, is_enabled, sort_order)
                   VALUES ('doubao', ?, ?, ?, 'zh', ?, 'builtin', ?, ?, ?)""",
                (
                    voice_id,
                    name,
                    gender,
                    description,
                    capabilities,
                    1 if voice_id in DOUBAO_DEFAULT_ENABLED_IDS else 0,
                    sort_order,
                ),
            )
        mimo_capabilities = json.dumps(
            {"preview": True, "speed": "style", "style": True},
            ensure_ascii=False,
        )
        for voice in MIMO_PRESET_VOICES:
            cursor.execute(
                """INSERT OR IGNORE INTO tts_voices
                   (provider, voice_id, name, gender, language, description, source,
                    capabilities_json, is_enabled, sort_order)
                   VALUES ('mimo', ?, ?, ?, ?, ?, 'builtin', ?, ?, ?)""",
                (
                    voice["voice_id"],
                    voice["name"],
                    voice["gender"],
                    voice["language"],
                    voice["description"],
                    mimo_capabilities,
                    1 if voice["voice_id"] in MIMO_DEFAULT_ENABLED_IDS else 0,
                    voice["sort_order"],
                ),
            )

    def _apply_column_migration(self, cursor, version: str, table: str,
                                columns: Dict[str, str]) -> None:
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,))
        if cursor.fetchone():
            return

        cursor.execute(f"PRAGMA table_info({table})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        for column_name, column_definition in columns.items():
            if column_name in existing_columns:
                continue
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN {column_name} {column_definition}"
            )

        cursor.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        logger.info(f"SQLite 迁移已应用: {version}")

    def _backfill_legacy_script_text(self, cursor) -> int:
        """Recover legacy full scripts from preserved segment copy without model calls."""
        rows = cursor.execute(
            """SELECT task_id FROM tasks
               WHERE TRIM(COALESCE(script_text, '')) = ''
                 AND EXISTS (
                     SELECT 1 FROM task_segments
                     WHERE task_segments.task_id = tasks.task_id
                       AND TRIM(COALESCE(task_segments.text, '')) != ''
                 )"""
        ).fetchall()
        recovered = 0
        for row in rows:
            task_id = row[0]
            segment_rows = cursor.execute(
                """SELECT text FROM task_segments
                   WHERE task_id=? AND TRIM(COALESCE(text, '')) != ''
                   ORDER BY segment_index ASC""",
                (task_id,),
            ).fetchall()
            script_text = "\n".join(str(segment[0]).strip() for segment in segment_rows)
            if not script_text:
                continue
            cursor.execute(
                """UPDATE tasks
                   SET script_text=?, script_source='reconstructed_segments'
                   WHERE task_id=? AND TRIM(COALESCE(script_text, '')) = ''""",
                (script_text, task_id),
            )
            recovered += max(0, cursor.rowcount)
        if recovered:
            logger.info("已从分镜恢复 %s 个历史任务的完整文案", recovered)
        return recovered

    @classmethod
    def _safe_structured_error_values(
        cls,
        error,
        error_code,
        error_meta,
    ):
        """Return canonical public text/code/metadata for one stored error."""

        if isinstance(error_meta, str):
            try:
                error_meta = json.loads(error_meta)
            except (TypeError, ValueError):
                error_meta = None
        has_error = bool(error or error_code or isinstance(error_meta, dict))
        code = normalize_error_code(error_code, has_error=has_error)
        if code is None:
            return None, None, None
        # Historical metadata may itself contain a provider response in
        # safe_message.  Keep only whitelisted classification fields and always
        # regenerate the public message from the stable code.
        metadata_source = dict(error_meta) if isinstance(error_meta, dict) else {}
        metadata_source.pop("safe_message", None)
        metadata = normalize_error_metadata(code, metadata_source)
        # Keep an explicitly authored, harmless operator message (for example
        # which segment needs repair).  Suspicious provider payloads collapse
        # to a generic string in sanitize_persisted_error_text; metadata still
        # carries the stable code-specific action for clients.
        public_error = sanitize_persisted_error_text(error)
        return (
            public_error or metadata["safe_message"],
            code.value,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )

    @classmethod
    def _sanitize_operation_targets(cls, targets):
        """Canonicalize durable per-target errors without changing target scope."""

        if not isinstance(targets, list):
            return []
        sanitized = []
        for raw_target in targets:
            if not isinstance(raw_target, dict):
                continue
            target = dict(raw_target)
            if any(
                target.get(key) not in (None, "")
                for key in ("error", "error_code", "error_meta", "error_meta_json")
            ):
                metadata = target.get("error_meta")
                if metadata is None:
                    metadata = target.get("error_meta_json")
                error, code, metadata_json = cls._safe_structured_error_values(
                    target.get("error"),
                    target.get("error_code"),
                    metadata,
                )
                target["error"] = error
                target["error_code"] = code
                target["error_meta"] = json.loads(metadata_json) if metadata_json else None
                target.pop("error_meta_json", None)
            sanitized.append(target)
        return sanitized

    def _sanitize_legacy_error_storage(self, cursor) -> None:
        """One-time, idempotent cleanup of pre-structured provider failures."""

        version = "20260819_sanitize_legacy_error_payloads_v1"
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,))
        if cursor.fetchone():
            return

        structured_fields = {
            "tasks": ("error",),
            "task_segments": ("prompt_error", "image_error", "audio_error"),
            "task_operations": ("error",),
        }
        for table, error_fields in structured_fields.items():
            for error_field in error_fields:
                code_field = f"{error_field}_code"
                meta_field = f"{error_field}_meta_json"
                rows = cursor.execute(
                    f"SELECT id, {error_field}, {code_field}, {meta_field} "
                    f"FROM {table} WHERE {error_field} IS NOT NULL "
                    f"OR {code_field} IS NOT NULL OR {meta_field} IS NOT NULL"
                ).fetchall()
                for row in rows:
                    error, code, metadata_json = self._safe_structured_error_values(
                        row[error_field], row[code_field], row[meta_field]
                    )
                    cursor.execute(
                        f"UPDATE {table} SET {error_field}=?, {code_field}=?, "
                        f"{meta_field}=? WHERE id=?",
                        (error, code, metadata_json, row["id"]),
                    )

        operation_rows = cursor.execute(
            "SELECT id, targets_json FROM task_operations"
        ).fetchall()
        for row in operation_rows:
            try:
                targets = json.loads(row["targets_json"] or "[]")
            except (TypeError, ValueError):
                targets = []
            sanitized_targets = self._sanitize_operation_targets(targets)
            cursor.execute(
                "UPDATE task_operations SET targets_json=? WHERE id=?",
                (
                    json.dumps(sanitized_targets, ensure_ascii=False, sort_keys=True),
                    row["id"],
                ),
            )

        for table in ("task_assets", "tts_voice_clones"):
            rows = cursor.execute(
                f"SELECT id, error_message FROM {table} "
                "WHERE error_message IS NOT NULL AND TRIM(error_message) != ''"
            ).fetchall()
            for row in rows:
                cursor.execute(
                    f"UPDATE {table} SET error_message=? WHERE id=?",
                    (sanitize_persisted_error_text(row["error_message"]), row["id"]),
                )

        cursor.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        logger.info(f"SQLite 迁移已应用: {version}")

    def _get_conn(self):
        """获取数据库连接"""
        if not self._initialized:
            self._init_db()
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def get_connection(self):
        """获取数据库连接（上下文管理器）"""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            logger.warning("SQLite 不可用，跳过数据库操作")
            yield None
            return
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            conn.close()

    def _row_to_dict(self, row):
        """将 sqlite3.Row 转为 dict"""
        if row is None:
            return None
        return dict(row)

    def _rows_to_dicts(self, rows):
        return [dict(r) for r in rows]

    @staticmethod
    def _encode_error_metadata(error_code, error_meta, *, has_error: bool):
        _error, code, metadata_json = SQLiteClient._safe_structured_error_values(
            "error" if has_error else None,
            error_code,
            error_meta,
        )
        return code, metadata_json

    @classmethod
    def _encode_error_record(cls, error, error_code, error_meta):
        return cls._safe_structured_error_values(error, error_code, error_meta)

    @classmethod
    def _decode_error_field(cls, record: Dict, error_field: str) -> Dict:
        """Add a safe parsed view while retaining raw JSON columns for compatibility."""
        if not record:
            return record
        code_field = f"{error_field}_code"
        json_field = f"{error_field}_meta_json"
        parsed_field = f"{error_field}_meta"
        raw_json = record.get(json_field)
        try:
            raw_meta = json.loads(raw_json) if raw_json else None
        except (TypeError, ValueError):
            raw_meta = None
        has_error = bool(record.get(error_field) or raw_json or record.get(code_field))
        code = normalize_error_code(record.get(code_field), has_error=has_error)
        if code is None:
            record[code_field] = None
            record[parsed_field] = None
            return record
        record[code_field] = code.value
        record[parsed_field] = normalize_error_metadata(code, raw_meta)
        return record

    @classmethod
    def _decode_task_error(cls, record: Dict) -> Dict:
        return cls._decode_error_field(record, "error")

    @classmethod
    def _decode_segment_errors(cls, record: Dict) -> Dict:
        for error_field in ("prompt_error", "image_error", "audio_error"):
            cls._decode_error_field(record, error_field)
        return record

    @classmethod
    def _prepare_segment_error_updates(cls, updates: Dict) -> Dict:
        prepared = dict(updates or {})
        for error_field in ("prompt_error", "image_error", "audio_error"):
            code_field = f"{error_field}_code"
            meta_field = f"{error_field}_meta"
            json_field = f"{error_field}_meta_json"
            error_was_supplied = error_field in prepared
            code_was_supplied = code_field in prepared
            meta_was_supplied = meta_field in prepared
            json_was_supplied = json_field in prepared
            raw_error = prepared.get(error_field)
            raw_meta = (
                prepared.pop(meta_field, None)
                if meta_was_supplied
                else prepared.get(json_field) if json_was_supplied else None
            )

            if (
                error_was_supplied
                and not raw_error
                and not code_was_supplied
                and not meta_was_supplied
                and not json_was_supplied
            ):
                prepared[code_field] = None
                prepared[json_field] = None
                continue
            if raw_error or code_was_supplied or meta_was_supplied or json_was_supplied:
                safe_error, code, metadata_json = cls._encode_error_record(
                    raw_error,
                    prepared.get(code_field),
                    raw_meta,
                )
                prepared[error_field] = safe_error
                prepared[code_field] = code
                prepared[json_field] = metadata_json
        return prepared

    def create_task(
        self,
        task_id: str,
        theme: str,
        style: str,
        length: int,
        name: str = None,
        ratio: str = "16:9",
        voice_type: str = None,
        tts_options: Dict = None,
        execution_mode: str = "full",
        script_policy: str = "rewrite",
        source_draft_id: str = None,
        template_id: str = None,
        generation_options: Dict = None,
        subtitle_options: Dict = None,
    ) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO tasks
                   (task_id, name, theme, style, length, ratio, voice_type,
                    tts_options_json, status, current_step, execution_mode,
                    workflow_phase, script_policy, voice_confirmed, source_draft_id,
                    template_id, generation_options_json, subtitle_options_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, name or theme[:20], theme, style, length, ratio,
                    voice_type,
                    json.dumps(tts_options, ensure_ascii=False) if tts_options else None,
                    'pending', 'pending', execution_mode,
                    'planning' if execution_mode == 'review_first' else 'pending',
                    script_policy, 0, source_draft_id, template_id,
                    json.dumps(generation_options or {}, ensure_ascii=False),
                    json.dumps(subtitle_options or {}, ensure_ascii=False),
                )
            )
            steps = [
                "text_generation",
                "image_prompt_generation",
                "voiceover_generation",
                "image_generation",
                "draft_building",
            ]
            for step in steps:
                cur.execute(
                    "INSERT INTO task_steps (task_id, step_name, status) VALUES (?,?,?)",
                    (task_id, step, 'pending')
                )
            conn.commit()
            conn.close()
            logger.info(f"任务记录创建成功: {task_id}")
            return True
        except Exception as e:
            logger.error(f"创建任务记录失败: {e}")
            return False

    def delete_task(self, task_id: str) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM task_operations WHERE task_id=?", (task_id,))
            cur.execute("DELETE FROM task_segments WHERE task_id=?", (task_id,))
            cur.execute("DELETE FROM task_assets WHERE task_id=?", (task_id,))
            cur.execute("DELETE FROM task_steps WHERE task_id=?", (task_id,))
            cur.execute("DELETE FROM task_results WHERE task_id=?", (task_id,))
            cur.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            conn.commit()
            conn.close()
            logger.info(f"任务记录已删除: {task_id}")
            return True
        except Exception as e:
            logger.error(f"删除任务记录失败: {e}")
            return False

    @staticmethod
    def _operation_row(row) -> Dict:
        if not row:
            return {}
        result = dict(row)
        try:
            result["targets"] = json.loads(result.get("targets_json") or "[]")
        except (TypeError, ValueError):
            result["targets"] = []
        return SQLiteClient._decode_error_field(result, "error")

    def create_task_operation(
        self,
        task_id: str,
        kind: str,
        idempotency_key: str,
        snapshot_key: str,
        targets: List[Dict],
    ) -> Dict:
        """Create one durable task operation, deduplicating active requests."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return {"outcome": "error"}
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "SELECT * FROM task_operations WHERE idempotency_key=? LIMIT 1",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing:
                if existing["state"] in {"pending", "running"}:
                    conn.commit()
                    return {"outcome": "duplicate", "operation": self._operation_row(existing)}
                # A terminal attempt must not permanently block the same explicit retry.
                idempotency_key = f"{idempotency_key}:{uuid.uuid4().hex}"
            cur.execute(
                """SELECT * FROM task_operations
                   WHERE task_id=? AND state IN ('pending','running')
                   ORDER BY id DESC LIMIT 1""",
                (task_id,),
            )
            active = cur.fetchone()
            if active:
                conn.commit()
                return {"outcome": "conflict", "operation": self._operation_row(active)}
            operation_id = uuid.uuid4().hex
            cur.execute(
                """INSERT INTO task_operations
                   (operation_id, task_id, kind, state, idempotency_key,
                    snapshot_key, targets_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    task_id,
                    kind,
                    "pending",
                    idempotency_key,
                    snapshot_key,
                    json.dumps(
                        self._sanitize_operation_targets(targets),
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
            return {
                "outcome": "created",
                "operation": self.get_task_operation(operation_id),
            }
        except Exception as exc:
            conn.rollback()
            logger.error(f"创建任务操作失败: {exc}")
            return {"outcome": "error"}
        finally:
            conn.close()

    def get_task_operation(self, operation_id: str) -> Dict:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return {}
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            return self._operation_row(row)
        finally:
            conn.close()

    def get_active_task_operation(self, task_id: str) -> Dict:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return {}
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM task_operations
                   WHERE task_id=? AND state IN ('pending','running')
                   ORDER BY id DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            return self._operation_row(row)
        finally:
            conn.close()

    def update_task_operation(
        self,
        operation_id: str,
        *,
        state: str = None,
        targets: List[Dict] = None,
        completed_count: int = None,
        failed_count: int = None,
        error: str = None,
        error_code: str = None,
        error_meta: Dict = None,
    ) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        fields = ["updated_at=datetime('now','localtime')"]
        values = []
        for key, value in (
            ("state", state),
            ("completed_count", completed_count),
            ("failed_count", failed_count),
        ):
            if value is not None:
                fields.append(f"{key}=?")
                values.append(value)
        if error is not None or error_code is not None or error_meta is not None:
            safe_error, code, metadata_json = self._encode_error_record(
                error,
                error_code,
                error_meta,
            )
            fields.extend(["error=?", "error_code=?", "error_meta_json=?"])
            values.extend([safe_error if safe_error is not None else error, code, metadata_json])
        if targets is not None:
            fields.append("targets_json=?")
            values.append(json.dumps(
                self._sanitize_operation_targets(targets),
                ensure_ascii=False,
            ))
        values.append(operation_id)
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE task_operations SET {', '.join(fields)} WHERE operation_id=?",
                values,
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as exc:
            logger.error(f"更新任务操作失败: {exc}")
            return False
        finally:
            conn.close()

    def start_task_operation(
        self,
        operation_id: str,
        task_id: str,
        *,
        operation_targets: List[Dict],
        workflow_phase: str,
        current_step: str,
        mark_asset_targets_processing: bool = False,
        mark_prompt_targets_processing: bool = False,
    ) -> bool:
        """Atomically expose an operation and its matching task running state."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """UPDATE task_operations
                   SET state='running', targets_json=?, completed_count=0,
                       failed_count=0, error='', error_code=NULL, error_meta_json=NULL,
                       updated_at=datetime('now','localtime')
                   WHERE operation_id=? AND task_id=? AND state='pending'""",
                (
                    json.dumps(
                        self._sanitize_operation_targets(operation_targets or []),
                        ensure_ascii=False,
                    ),
                    operation_id,
                    task_id,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            if mark_asset_targets_processing:
                for target in operation_targets or []:
                    asset_type = target.get("asset_type")
                    segment_index = target.get("segment_index")
                    if (
                        asset_type not in {"image", "audio"}
                        or segment_index is None
                        or target.get("mode") == "replace"
                    ):
                        continue
                    cur.execute(
                        f"""UPDATE task_segments
                            SET {asset_type}_status='processing', {asset_type}_error=NULL,
                                {asset_type}_error_code=NULL,
                                {asset_type}_error_meta_json=NULL,
                                updated_at=datetime('now','localtime')
                            WHERE task_id=? AND segment_index=?""",
                        (task_id, int(segment_index)),
                    )
                    if cur.rowcount != 1:
                        conn.rollback()
                        return False
            if mark_prompt_targets_processing:
                for target in operation_targets or []:
                    if (
                        target.get("asset_type") != "prompt"
                        or target.get("segment_index") is None
                    ):
                        continue
                    cur.execute(
                        """UPDATE task_segments
                           SET prompt_status='processing', prompt_error=NULL,
                               prompt_error_code=NULL, prompt_error_meta_json=NULL,
                               updated_at=datetime('now','localtime')
                           WHERE task_id=? AND segment_index=?""",
                        (task_id, int(target["segment_index"])),
                    )
                    if cur.rowcount != 1:
                        conn.rollback()
                        return False
            cur.execute(
                """UPDATE tasks
                   SET status='processing', workflow_phase=?, current_step=?, error=NULL,
                       error_code=NULL, error_meta_json=NULL,
                       updated_at=datetime('now','localtime')
                   WHERE task_id=?""",
                (workflow_phase, current_step, task_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            logger.error(f"原子启动任务操作失败: {exc}")
            return False
        finally:
            conn.close()

    def finish_task_operation(
        self,
        operation_id: str,
        task_id: str,
        *,
        operation_state: str,
        operation_targets: List[Dict],
        completed_count: int,
        failed_count: int,
        operation_error: Optional[str],
        task_status: str,
        workflow_phase: str,
        current_step: str,
        task_error: Optional[str],
        result: Optional[Dict] = None,
        clear_result: bool = False,
        operation_error_code: Optional[str] = None,
        operation_error_meta: Optional[Dict] = None,
        task_error_code: Optional[str] = None,
        task_error_meta: Optional[Dict] = None,
    ) -> bool:
        """Atomically finish an operation and expose its resulting task state."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            safe_operation_error, encoded_operation_code, encoded_operation_meta = self._encode_error_record(
                operation_error,
                operation_error_code,
                operation_error_meta,
            )
            safe_task_error, encoded_task_code, encoded_task_meta = self._encode_error_record(
                task_error,
                task_error_code,
                task_error_meta,
            )
            cur.execute(
                """UPDATE task_operations
                   SET state=?, targets_json=?, completed_count=?, failed_count=?,
                       error=?, error_code=?, error_meta_json=?,
                       updated_at=datetime('now','localtime')
                   WHERE operation_id=? AND task_id=?
                     AND state IN ('pending','running')""",
                (
                    operation_state,
                    json.dumps(
                        self._sanitize_operation_targets(operation_targets or []),
                        ensure_ascii=False,
                    ),
                    int(completed_count or 0),
                    int(failed_count or 0),
                    safe_operation_error if safe_operation_error is not None else operation_error,
                    encoded_operation_code,
                    encoded_operation_meta,
                    operation_id,
                    task_id,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            completed_at_sql = (
                "datetime('now','localtime')" if task_status == "completed" else "completed_at"
            )
            cur.execute(
                f"""UPDATE tasks
                    SET status=?, workflow_phase=?, current_step=?, error=?,
                        error_code=?, error_meta_json=?,
                        completed_at={completed_at_sql},
                        updated_at=datetime('now','localtime')
                    WHERE task_id=?""",
                (
                    task_status,
                    workflow_phase,
                    current_step,
                    safe_task_error if safe_task_error is not None else task_error,
                    encoded_task_code,
                    encoded_task_meta,
                    task_id,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            if result is not None:
                cur.execute(
                    """INSERT INTO task_results
                       (task_id, draft_path, draft_url, video_url, segments_count, total_duration)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(task_id) DO UPDATE SET
                       draft_path=excluded.draft_path, draft_url=excluded.draft_url,
                       video_url=excluded.video_url, segments_count=excluded.segments_count,
                       total_duration=excluded.total_duration""",
                    (
                        task_id,
                        result.get("draft_path"),
                        result.get("draft_url"),
                        result.get("video_url"),
                        int(result.get("segments_count") or 0),
                        result.get("total_duration"),
                    ),
                )
            elif clear_result:
                cur.execute("DELETE FROM task_results WHERE task_id=?", (task_id,))
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            logger.error(f"原子完成任务操作失败: {exc}")
            return False
        finally:
            conn.close()

    def interrupt_orphaned_task_operation(self, task_id: str) -> Dict:
        """Expose a previous-process operation as retryable without touching completed assets."""
        operation = self.get_active_task_operation(task_id)
        if not operation:
            return {}
        targets = operation.get("targets") or []
        for target in targets:
            if target.get("status") in {"pending", "processing"}:
                target["status"] = "failed"
                target["error"] = "服务重启导致本次操作中断，可再次重试"
                if (
                    target.get("asset_type") in {"image", "audio"}
                    and target.get("mode") != "replace"
                    and target.get("segment_index") is not None
                ):
                    self.update_segment(
                        task_id,
                        int(target["segment_index"]),
                        {
                            f"{target['asset_type']}_status": "failed",
                            f"{target['asset_type']}_error": target["error"],
                        },
                    )
                elif (
                    target.get("asset_type") == "prompt"
                    and target.get("segment_index") is not None
                ):
                    self.update_segment(
                        task_id,
                        int(target["segment_index"]),
                        {
                            "prompt_status": "failed",
                            "prompt_error": target["error"],
                        },
                    )
        self.update_task_operation(
            operation["operation_id"],
            state="interrupted",
            targets=targets,
            completed_count=sum(1 for item in targets if item.get("status") == "completed"),
            failed_count=sum(1 for item in targets if item.get("status") == "failed"),
            error="服务重启导致本次操作中断",
        )
        return self.get_task_operation(operation["operation_id"])

    def update_task_status(
        self,
        task_id: str,
        status: str,
        current_step: str = None,
        error: str = None,
        error_code: str = None,
        error_meta: Dict = None,
    ) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            safe_error, encoded_code, encoded_meta = self._encode_error_record(
                error,
                error_code,
                error_meta,
            )
            stored_error = safe_error if safe_error is not None else error
            if status == "completed":
                cur.execute(
                    """UPDATE tasks SET status=?, current_step=?, error=?,
                       error_code=?, error_meta_json=?,
                       completed_at=datetime('now','localtime'),
                       updated_at=datetime('now','localtime') WHERE task_id=?""",
                    (status, current_step, stored_error, encoded_code, encoded_meta, task_id)
                )
            else:
                cur.execute(
                    """UPDATE tasks SET status=?, current_step=?, error=?,
                       error_code=?, error_meta_json=?,
                       updated_at=datetime('now','localtime') WHERE task_id=?""",
                    (status, current_step, stored_error, encoded_code, encoded_meta, task_id)
                )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")
            return False

    def mark_task_interrupted(
        self,
        task_id: str,
        current_step: str = None,
        error: str = None,
        error_code: str = None,
        error_meta: Dict = None,
    ) -> bool:
        """Mark a task interrupted unless deletion has already claimed it."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            safe_error, encoded_code, encoded_meta = self._encode_error_record(
                error,
                error_code,
                error_meta,
            )
            cur.execute(
                """UPDATE tasks
                   SET status='interrupted', current_step=?, error=?, error_code=?,
                       error_meta_json=?,
                       updated_at=datetime('now','localtime')
                   WHERE task_id=? AND status != 'deleting'""",
                (
                    current_step,
                    safe_error if safe_error is not None else error,
                    encoded_code,
                    encoded_meta,
                    task_id,
                ),
            )
            updated = cur.rowcount > 0
            conn.commit()
            conn.close()
            return updated
        except Exception as exc:
            logger.error(f"标记任务中断失败: {exc}")
            return False

    def _update_task_fields(self, task_id: str, updates: Dict) -> bool:
        fields = {
            key: value for key, value in updates.items()
            if key in self.TASK_CHECKPOINT_COLUMNS
        }
        if not fields:
            return False

        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            set_parts = [f"{key}=?" for key in fields]
            values = list(fields.values())
            values.append(task_id)
            cur.execute(
                f"UPDATE tasks SET {', '.join(set_parts)}, "
                "updated_at=datetime('now','localtime') WHERE task_id=?",
                values,
            )
            updated = cur.rowcount > 0
            conn.commit()
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"更新任务检查点失败: {e}")
            return False

    def save_task_checkpoint(self, task_id: str, script_text: str = None,
                             summary: str = None, input_mode: str = None,
                             execution_mode: str = None,
                             workflow_phase: str = None,
                             script_policy: str = None,
                             voice_confirmed: int = None,
                             script_source: str = None) -> bool:
        values = {
            "script_text": script_text,
            "script_source": script_source,
            "summary": summary,
            "input_mode": input_mode,
            "execution_mode": execution_mode,
            "workflow_phase": workflow_phase,
            "script_policy": script_policy,
            "voice_confirmed": voice_confirmed,
        }
        updates = {key: value for key, value in values.items() if value is not None}
        return self._update_task_fields(task_id, updates)

    def update_task_workflow(
        self,
        task_id: str,
        workflow_phase: str,
        status: str = None,
        current_step: str = None,
    ) -> bool:
        """Persist a workflow transition without losing the task checkpoint."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            fields = ["workflow_phase=?", "updated_at=datetime('now','localtime')"]
            values = [workflow_phase]
            if status is not None:
                fields.append("status=?")
                values.append(status)
            if current_step is not None:
                fields.append("current_step=?")
                values.append(current_step)
            values.append(task_id)
            cur.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id=?",
                values,
            )
            updated = cur.rowcount > 0
            conn.commit()
            conn.close()
            return updated
        except Exception as exc:
            logger.error(f"更新任务工作流失败: {exc}")
            return False

    def update_task_plan_fields(
        self,
        task_id: str,
        updates: Dict,
        expected_plan_version: int = None,
    ) -> Optional[int]:
        """Atomically update task settings and advance the plan version."""
        allowed = {
            "style", "ratio", "voice_type", "tts_options_json", "voice_confirmed",
            "script_text", "script_source", "summary", "workflow_phase", "template_id",
            "generation_options_json", "subtitle_options_json",
        }
        fields = {key: value for key, value in updates.items() if key in allowed}
        if not fields:
            return None
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT plan_version FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            current = int(row["plan_version"] or 0)
            if expected_plan_version is not None and current != int(expected_plan_version):
                return -1
            next_version = current + 1
            parts = [f"{key}=?" for key in fields]
            values = list(fields.values())
            parts.extend(["plan_version=?", "updated_at=datetime('now','localtime')"])
            values.extend([next_version, task_id])
            cur.execute(
                f"UPDATE tasks SET {', '.join(parts)} WHERE task_id=?",
                values,
            )
            conn.commit()
            return next_version
        except Exception as exc:
            conn.rollback()
            logger.error(f"更新任务预案字段失败: {exc}")
            return None
        finally:
            conn.close()

    def update_segment_plan(
        self,
        task_id: str,
        segment_index: int,
        updates: Dict,
        expected_plan_version: int = None,
    ) -> Optional[int]:
        """Atomically edit one segment and advance the task plan version."""
        prepared_updates = self._prepare_segment_error_updates(updates)
        fields = {
            key: value for key, value in prepared_updates.items()
            if key in self.SEGMENT_CHECKPOINT_COLUMNS
        }
        if not fields:
            return None
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT plan_version FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            current = int(row["plan_version"] or 0)
            if expected_plan_version is not None and current != int(expected_plan_version):
                return -1
            parts = [f"{key}=?" for key in fields]
            values = list(fields.values())
            values.extend([task_id, segment_index])
            cur.execute(
                f"UPDATE task_segments SET {', '.join(parts)}, "
                "updated_at=datetime('now','localtime') WHERE task_id=? AND segment_index=?",
                values,
            )
            if cur.rowcount <= 0:
                return None
            next_version = current + 1
            cur.execute(
                "UPDATE tasks SET plan_version=?, updated_at=datetime('now','localtime') WHERE task_id=?",
                (next_version, task_id),
            )
            conn.commit()
            return next_version
        except Exception as exc:
            conn.rollback()
            logger.error(f"更新分镜预案失败: {exc}")
            return None
        finally:
            conn.close()

    def replace_plan_segments(
        self,
        task_id: str,
        script_text: str,
        segments: List[Dict],
        expected_plan_version: int = None,
    ) -> Optional[int]:
        """Replace the whole plan in one transaction, removing obsolete tail rows."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT plan_version FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            current = int(row["plan_version"] or 0)
            if expected_plan_version is not None and current != int(expected_plan_version):
                return -1
            cur.execute("DELETE FROM task_segments WHERE task_id=?", (task_id,))
            for segment in segments:
                cur.execute(
                    """INSERT INTO task_segments
                       (task_id, segment_index, text, image_prompt, image_status,
                        audio_status, prompt_status, prompt_manual, prompt_needs_review)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, segment["segment_index"], segment["text"],
                        segment.get("image_prompt", ""),
                        segment.get("image_status", "pending"),
                        segment.get("audio_status", "pending"),
                        segment.get("prompt_status", "pending"),
                        int(bool(segment.get("prompt_manual"))),
                        int(bool(segment.get("prompt_needs_review"))),
                    ),
                )
            next_version = current + 1
            cur.execute(
                """UPDATE tasks SET script_text=?, script_source='user_edited',
                   plan_version=?, workflow_phase='planning',
                   status='interrupted', current_step='image_prompt_generation', error=NULL,
                   updated_at=datetime('now','localtime') WHERE task_id=?""",
                (script_text, next_version, task_id),
            )
            conn.commit()
            return next_version
        except Exception as exc:
            conn.rollback()
            logger.error(f"替换分镜预案失败: {exc}")
            return None
        finally:
            conn.close()

    def set_task_deletion_intent(self, task_id: str, delete_files: bool) -> bool:
        """Persist file cleanup intent so startup can finish deletion safely."""
        return self._update_task_fields(
            task_id, {"delete_files_on_delete": int(bool(delete_files))}
        )

    def save_task_result(self, task_id: str, draft_path: str, segments_count: int,
                         draft_url: str = None, video_url: str = None, total_duration: float = None) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO task_results (task_id, draft_path, draft_url, video_url, segments_count, total_duration)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                   draft_path=excluded.draft_path, draft_url=excluded.draft_url,
                   video_url=excluded.video_url, segments_count=excluded.segments_count,
                   total_duration=excluded.total_duration""",
                (task_id, draft_path, draft_url, video_url, segments_count, total_duration)
            )
            conn.commit()
            conn.close()
            logger.info(f"任务结果保存成功: {task_id}")
            return True
        except Exception as e:
            logger.error(f"保存任务结果失败: {e}")
            return False

    def complete_task_with_result(
        self,
        task_id: str,
        draft_path: str,
        segments_count: int,
        *,
        draft_url: str = None,
        video_url: str = None,
        total_duration: float = None,
        workflow_phase: str = "ready",
    ) -> bool:
        """Atomically publish a validated result and clear any historical error."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """INSERT INTO task_results
                   (task_id, draft_path, draft_url, video_url, segments_count, total_duration)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                   draft_path=excluded.draft_path, draft_url=excluded.draft_url,
                   video_url=excluded.video_url, segments_count=excluded.segments_count,
                   total_duration=excluded.total_duration""",
                (
                    task_id,
                    draft_path,
                    draft_url,
                    video_url,
                    int(segments_count or 0),
                    total_duration,
                ),
            )
            cur.execute(
                """UPDATE tasks
                   SET status='completed', workflow_phase=?, current_step='completed',
                       error=NULL, error_code=NULL, error_meta_json=NULL,
                       exported_at=NULL, last_export_target=NULL,
                       completed_at=datetime('now','localtime'),
                       updated_at=datetime('now','localtime')
                   WHERE task_id=? AND status != 'deleting'""",
                (workflow_phase, task_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            logger.error(f"原子完成任务失败: {exc}")
            return False
        finally:
            conn.close()

    def mark_task_exported(self, task_id: str, target: str) -> bool:
        """Persist that a user-requested export completed successfully."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            with self.get_connection() as conn:
                if not conn:
                    return False
                cur = conn.execute(
                    """UPDATE tasks
                       SET exported_at=datetime('now','localtime'),
                           last_export_target=?, updated_at=datetime('now','localtime')
                       WHERE task_id=? AND status='completed'""",
                    (str(target or "export"), task_id),
                )
                conn.commit()
                return cur.rowcount == 1
        except Exception as exc:
            logger.error("记录任务导出状态失败: %s", exc)
            return False

    def clear_task_result(self, task_id: str) -> bool:
        """Invalidate the draft record while leaving local files recoverable."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM task_results WHERE task_id=?", (task_id,))
            conn.commit()
            return True
        except Exception as exc:
            logger.error(f"清除任务结果记录失败: {exc}")
            return False
        finally:
            conn.close()

    def invalidate_task_result_for_finalization(self, task_id: str) -> bool:
        """Atomically mark a review-first task dirty without deleting old files."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """UPDATE tasks
                   SET status='awaiting_finalization',
                       workflow_phase='awaiting_finalization',
                       current_step='awaiting_finalization',
                       error=NULL, error_code=NULL, error_meta_json=NULL,
                       updated_at=datetime('now','localtime')
                   WHERE task_id=? AND execution_mode='review_first'""",
                (task_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            cur.execute("DELETE FROM task_results WHERE task_id=?", (task_id,))
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            logger.error("使生产草稿失效失败: %s", exc)
            return False
        finally:
            conn.close()

    def update_step(self, task_id: str, step_name: str, status: str,
                    progress: int = None, total: int = None, duration: float = None) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            if status == "processing":
                cur.execute(
                    "UPDATE task_steps SET status=?, started_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE task_id=? AND step_name=?",
                    (status, task_id, step_name)
                )
            elif status == "completed":
                cur.execute(
                    "UPDATE task_steps SET status=?, progress=?, total=?, duration=?, completed_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE task_id=? AND step_name=?",
                    (status, progress, total, duration, task_id, step_name)
                )
            else:
                cur.execute(
                    "UPDATE task_steps SET status=?, progress=?, total=?, updated_at=datetime('now','localtime') WHERE task_id=? AND step_name=?",
                    (status, progress, total, task_id, step_name)
                )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"更新步骤状态失败: {e}")
            return False

    def get_task(self, task_id: str) -> Optional[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            task = cur.fetchone()
            if not task:
                conn.close()
                return None
            task = self._decode_task_error(dict(task))

            cur.execute("SELECT * FROM task_results WHERE task_id=?", (task_id,))
            result = cur.fetchone()
            task["result"] = dict(result) if result else None

            cur.execute("SELECT * FROM task_steps WHERE task_id=? ORDER BY id", (task_id,))
            steps = [dict(r) for r in cur.fetchall()]
            task["steps"] = steps

            conn.close()
            return task
        except Exception as e:
            logger.error(f"获取任务信息失败: {e}")
            return None

    def list_tts_voices(self, provider: str = None, include_disabled: bool = False) -> List[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return []
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            sql = "SELECT * FROM tts_voices WHERE 1=1"
            values = []
            if provider:
                sql += " AND provider=?"
                values.append(provider)
            if not include_disabled:
                sql += " AND is_enabled=1"
            sql += " ORDER BY provider ASC, sort_order DESC, id ASC"
            cur.execute(sql, values)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            for row in rows:
                row["id"] = build_voice_key(row["provider"], row["voice_id"])
                row["is_enabled"] = bool(row["is_enabled"])
                try:
                    row["capabilities"] = json.loads(row.get("capabilities_json") or "{}")
                except (TypeError, ValueError):
                    row["capabilities"] = {}
            return rows
        except Exception as e:
            logger.error(f"获取音色列表失败: {e}")
            return []

    def get_enabled_voices(self) -> List[Dict]:
        return self.list_tts_voices(include_disabled=False)

    def find_tts_voice(self, provider: str, voice_id: str) -> Optional[Dict]:
        rows = self.list_tts_voices(provider=provider, include_disabled=True)
        return next((row for row in rows if row["voice_id"] == voice_id), None)

    def set_voice_availability(self, voice_keys: List[str]) -> int:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return 0
        selections = {
            (selection.provider, selection.voice_id)
            for selection in (parse_voice_key(key) for key in voice_keys)
            if selection.kind == "preset"
        }
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT provider, voice_id FROM tts_voices")
            rows = cur.fetchall()
            for row in rows:
                cur.execute(
                    """UPDATE tts_voices SET is_enabled=?,
                       updated_at=datetime('now','localtime')
                       WHERE provider=? AND voice_id=?""",
                    (1 if (row["provider"], row["voice_id"]) in selections else 0,
                     row["provider"], row["voice_id"]),
                )
            conn.commit()
            conn.close()
            return len(rows)
        except Exception as e:
            logger.error(f"更新音色开放状态失败: {e}")
            return 0

    def update_voice_status(self, voice_id: str, is_enabled: bool) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("UPDATE tts_voices SET is_enabled=? WHERE voice_id=?", (1 if is_enabled else 0, voice_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"更新音色状态失败: {e}")
            return False

    def create_voice_clone(self, record: Dict) -> Dict:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return {}
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO tts_voice_clones
                   (clone_id, provider, name, reference_path, duration, file_size,
                    status, preview_path, error_message, is_enabled, consent_confirmed)
                   VALUES (?, 'mimo', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["clone_id"], record["name"], record["reference_path"],
                    record.get("duration"), record.get("file_size"),
                    record.get("status", "draft"), record.get("preview_path"),
                    sanitize_persisted_error_text(record.get("error_message")),
                    1 if record.get("is_enabled") else 0,
                    1 if record.get("consent_confirmed") else 0,
                ),
            )
            conn.commit()
            conn.close()
            return self.get_voice_clone(record["clone_id"]) or {}
        except Exception as exc:
            logger.error(f"创建克隆音色失败: {exc}")
            return {}

    def get_voice_clone(self, clone_id: str) -> Optional[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tts_voice_clones WHERE clone_id=?", (clone_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_voice_clones(self, include_hidden: bool = False) -> List[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return []
        conn = self._get_conn()
        cur = conn.cursor()
        sql = "SELECT * FROM tts_voice_clones"
        if not include_hidden:
            sql += " WHERE status != 'hidden'"
        sql += " ORDER BY updated_at DESC, id DESC"
        cur.execute(sql)
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return rows

    def update_voice_clone(self, clone_id: str, updates: Dict) -> Optional[Dict]:
        allowed = {
            "name", "reference_path", "duration", "file_size", "status",
            "preview_path", "error_message", "is_enabled", "consent_confirmed",
        }
        fields = {key: value for key, value in updates.items() if key in allowed}
        if "error_message" in fields:
            fields["error_message"] = sanitize_persisted_error_text(
                fields["error_message"]
            )
        if not fields:
            return self.get_voice_clone(clone_id)
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        conn = self._get_conn()
        cur = conn.cursor()
        parts = [f"{key}=?" for key in fields]
        values = [
            1 if key in {"is_enabled", "consent_confirmed"} and value else
            0 if key in {"is_enabled", "consent_confirmed"} else value
            for key, value in fields.items()
        ]
        values.append(clone_id)
        cur.execute(
            f"UPDATE tts_voice_clones SET {', '.join(parts)}, "
            "updated_at=datetime('now','localtime') WHERE clone_id=?",
            values,
        )
        conn.commit()
        conn.close()
        return self.get_voice_clone(clone_id)

    def delete_voice_clone(self, clone_id: str) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM tts_voice_clones WHERE clone_id=?", (clone_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def is_voice_clone_referenced(self, clone_id: str) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        key = f"mimo-clone:{clone_id}"
        conn = self._get_conn()
        cur = conn.cursor()
        checks = (
            ("SELECT 1 FROM tasks WHERE voice_type=? LIMIT 1", (key,)),
            ("SELECT 1 FROM task_segments WHERE audio_voice_type=? LIMIT 1", (key,)),
            ("SELECT 1 FROM task_assets WHERE voice_type=? LIMIT 1", (key,)),
        )
        referenced = False
        for sql, values in checks:
            cur.execute(sql, values)
            if cur.fetchone():
                referenced = True
                break
        conn.close()
        return referenced

    def update_extract_path(self, task_id: str, extract_path: str) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("UPDATE tasks SET extract_path=? WHERE task_id=?", (extract_path, task_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"更新解压路径失败: {e}")
            return False

    def list_tasks(self, status: str = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return []
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            if status:
                cur.execute(
                    """SELECT t.*, r.draft_path, r.draft_url, r.video_url, r.segments_count,
                              (
                                SELECT s.image_url
                                FROM task_segments s
                                WHERE s.task_id = t.task_id
                                  AND s.image_url IS NOT NULL
                                  AND TRIM(s.image_url) != ''
                                ORDER BY s.segment_index ASC
                                LIMIT 1
                              ) AS cover_image_url,
                              (
                                SELECT s.image_path
                                FROM task_segments s
                                WHERE s.task_id = t.task_id
                                  AND s.image_path IS NOT NULL
                                  AND TRIM(s.image_path) != ''
                                ORDER BY s.segment_index ASC
                                LIMIT 1
                              ) AS cover_image_path
                       FROM tasks t LEFT JOIN task_results r ON t.task_id = r.task_id
                       WHERE t.status=? ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
                    (status, limit, offset)
                )
            else:
                cur.execute(
                    """SELECT t.*, r.draft_path, r.draft_url, r.video_url, r.segments_count,
                              (
                                SELECT s.image_url
                                FROM task_segments s
                                WHERE s.task_id = t.task_id
                                  AND s.image_url IS NOT NULL
                                  AND TRIM(s.image_url) != ''
                                ORDER BY s.segment_index ASC
                                LIMIT 1
                              ) AS cover_image_url,
                              (
                                SELECT s.image_path
                                FROM task_segments s
                                WHERE s.task_id = t.task_id
                                  AND s.image_path IS NOT NULL
                                  AND TRIM(s.image_path) != ''
                                ORDER BY s.segment_index ASC
                                LIMIT 1
                              ) AS cover_image_path
                       FROM tasks t LEFT JOIN task_results r ON t.task_id = r.task_id
                       WHERE t.status != ?
                       ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
                    ("deleting", limit, offset)
                )
            rows = [self._decode_task_error(dict(r)) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"获取任务列表失败: {e}")
            return []

    def save_segments(self, task_id: str, segments: List[Dict]) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            for raw_segment in segments:
                seg = self._prepare_segment_error_updates(raw_segment)
                cur.execute(
                    """INSERT INTO task_segments
                       (task_id, segment_index, text, image_prompt, image_path,
                        image_url, image_status, image_error, image_error_code,
                        image_error_meta_json, audio_path, audio_url, audio_status,
                        audio_error, audio_error_code, audio_error_meta_json,
                        duration, audio_voice_type, audio_tts_options_json,
                        prompt_status, prompt_error, prompt_error_code,
                        prompt_error_meta_json, prompt_manual, prompt_needs_review)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(task_id, segment_index) DO UPDATE SET
                       text=excluded.text, image_prompt=excluded.image_prompt,
                       image_path=COALESCE(excluded.image_path, task_segments.image_path),
                       image_url=COALESCE(excluded.image_url, task_segments.image_url),
                       image_status=COALESCE(excluded.image_status, task_segments.image_status),
                       image_error=COALESCE(excluded.image_error, task_segments.image_error),
                       image_error_code=COALESCE(excluded.image_error_code, task_segments.image_error_code),
                       image_error_meta_json=COALESCE(excluded.image_error_meta_json, task_segments.image_error_meta_json),
                       audio_path=COALESCE(excluded.audio_path, task_segments.audio_path),
                       audio_url=COALESCE(excluded.audio_url, task_segments.audio_url),
                       audio_status=COALESCE(excluded.audio_status, task_segments.audio_status),
                       audio_error=COALESCE(excluded.audio_error, task_segments.audio_error),
                       audio_error_code=COALESCE(excluded.audio_error_code, task_segments.audio_error_code),
                       audio_error_meta_json=COALESCE(excluded.audio_error_meta_json, task_segments.audio_error_meta_json),
                       audio_voice_type=COALESCE(excluded.audio_voice_type, task_segments.audio_voice_type),
                       audio_tts_options_json=COALESCE(excluded.audio_tts_options_json, task_segments.audio_tts_options_json),
                       prompt_status=COALESCE(excluded.prompt_status, task_segments.prompt_status),
                       prompt_error=COALESCE(excluded.prompt_error, task_segments.prompt_error),
                       prompt_error_code=COALESCE(excluded.prompt_error_code, task_segments.prompt_error_code),
                       prompt_error_meta_json=COALESCE(excluded.prompt_error_meta_json, task_segments.prompt_error_meta_json),
                       prompt_manual=COALESCE(excluded.prompt_manual, task_segments.prompt_manual),
                       prompt_needs_review=COALESCE(excluded.prompt_needs_review, task_segments.prompt_needs_review),
                       duration=COALESCE(excluded.duration, task_segments.duration),
                       updated_at=datetime('now','localtime')""",
                    (task_id, seg['segment_index'], seg['text'],
                     seg.get('image_prompt'),
                     seg.get('image_path'), seg.get('image_url'),
                     seg.get('image_status'), seg.get('image_error'),
                     seg.get('image_error_code'), seg.get('image_error_meta_json'),
                     seg.get('audio_path'), seg.get('audio_url'),
                     seg.get('audio_status'), seg.get('audio_error'),
                     seg.get('audio_error_code'), seg.get('audio_error_meta_json'),
                     seg.get('duration'), seg.get('audio_voice_type'),
                     seg.get('audio_tts_options_json'),
                     seg.get('prompt_status') or ('completed' if seg.get('image_prompt') else 'pending'),
                     seg.get('prompt_error'), seg.get('prompt_error_code'),
                     seg.get('prompt_error_meta_json'), int(bool(seg.get('prompt_manual'))),
                     int(bool(seg.get('prompt_needs_review'))))
                )
            conn.commit()
            conn.close()
            logger.info(f"任务段落保存成功: {task_id}, 共 {len(segments)} 段")
            return True
        except Exception as e:
            logger.error(f"保存任务段落失败: {e}")
            return False

    def get_segments(self, task_id: str) -> List[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return []
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM task_segments WHERE task_id=? ORDER BY segment_index ASC", (task_id,))
            rows = [self._decode_segment_errors(dict(r)) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"获取任务段落失败: {e}")
            return []

    def update_segment(self, task_id: str, segment_index: int, updates: Dict) -> bool:
        return self._update_segment_fields(task_id, segment_index, updates)

    def update_segment_checkpoint(self, task_id: str, segment_index: int, **updates) -> bool:
        return self._update_segment_fields(task_id, segment_index, updates)

    def _update_segment_fields(self, task_id: str, segment_index: int, updates: Dict) -> bool:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            set_parts = []
            values = []
            prepared_updates = self._prepare_segment_error_updates(updates)
            for key, value in prepared_updates.items():
                if value is None and key not in self.CLEARABLE_SEGMENT_ERROR_COLUMNS:
                    continue
                if key in self.SEGMENT_CHECKPOINT_COLUMNS:
                    set_parts.append(f"{key}=?")
                    values.append(value)
            if not set_parts:
                conn.close()
                return False
            values.extend([task_id, segment_index])
            cur.execute(
                f"UPDATE task_segments SET {', '.join(set_parts)}, "
                "updated_at=datetime('now','localtime') WHERE task_id=? AND segment_index=?",
                values
            )
            updated = cur.rowcount > 0
            conn.commit()
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"更新段落失败: {e}")
            return False

    def save_task_asset(self, task_id: str, asset_type: str, source: str, path: str = None,
                        url: str = None, segment_index: int = None, label: str = None,
                        prompt: str = None, text: str = None, voice_type: str = None,
                        metadata_json: str = None, status: str = "completed",
                        error_message: str = None, operation_id: str = None,
                        origin_asset_id: str = None, snapshot_json: str = None) -> Dict:
        """Persist an immutable asset version.

        Re-reading a legacy path is idempotent, but a newly generated/uploaded
        path always becomes a new row.  Existing rows are never rewritten, so
        history timestamps remain stable when the workspace is refreshed.
        """
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return {}
        try:
            error_message = sanitize_persisted_error_text(error_message)
            conn = self._get_conn()
            cur = conn.cursor()
            if path and source == "legacy":
                cur.execute(
                    """SELECT * FROM task_assets
                       WHERE task_id=? AND asset_type=? AND path=?
                         AND COALESCE(segment_index, -1)=COALESCE(?, -1)
                       ORDER BY CASE WHEN source='legacy' THEN 1 ELSE 0 END, id ASC
                       LIMIT 1""",
                    (task_id, asset_type, path, segment_index)
                )
            elif path:
                cur.execute(
                    """SELECT * FROM task_assets
                       WHERE task_id=? AND asset_type=? AND source=? AND path=?
                         AND COALESCE(segment_index, -1)=COALESCE(?, -1)
                       ORDER BY id DESC LIMIT 1""",
                    (task_id, asset_type, source, path, segment_index)
                )
            else:
                cur.execute(
                    """SELECT * FROM task_assets
                       WHERE task_id=? AND asset_type=? AND source=? AND segment_index=?
                       ORDER BY id DESC LIMIT 1""",
                    (task_id, asset_type, source, segment_index)
                )
            existing = cur.fetchone()
            if existing:
                conn.close()
                return dict(existing)

            asset_id = uuid.uuid4().hex
            cur.execute(
                """INSERT INTO task_assets
                   (asset_id, task_id, segment_index, asset_type, source, path, url,
                    label, prompt, text, voice_type, metadata_json, status,
                    error_message, operation_id, origin_asset_id, snapshot_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, task_id, segment_index, asset_type, source, path, url,
                 label, prompt, text, voice_type, metadata_json, status,
                 error_message, operation_id, origin_asset_id, snapshot_json)
            )
            conn.commit()
            cur.execute("SELECT * FROM task_assets WHERE asset_id=?", (asset_id,))
            row = dict(cur.fetchone())
            conn.close()
            return row
        except Exception as e:
            logger.error(f"保存任务资产失败: {e}")
            return {}

    def list_task_assets(self, task_id: str, asset_type: str = None, segment_index: int = None) -> List[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return []
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            sql = "SELECT * FROM task_assets WHERE task_id=?"
            values = [task_id]
            if asset_type:
                if asset_type == "upload":
                    sql += " AND source='upload'"
                else:
                    sql += " AND asset_type=?"
                    values.append(asset_type)
            if segment_index is not None:
                sql += " AND segment_index=?"
                values.append(segment_index)
            sql += " ORDER BY created_at DESC, id DESC"
            cur.execute(sql, values)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"获取任务资产失败: {e}")
            return []

    def get_task_asset(self, task_id: str, asset_id: str) -> Optional[Dict]:
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return None
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM task_assets WHERE task_id=? AND asset_id=?",
                (task_id, asset_id),
            )
            row = cur.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"获取任务资产失败: {e}")
            return None

    def select_segment_asset(
        self,
        task_id: str,
        segment_index: int,
        asset: Dict,
        asset_type: str,
        confirm_text_mismatch: bool = False,
    ) -> bool:
        """Atomically point one segment at an existing immutable asset."""
        if asset_type not in {"image", "audio"}:
            return False
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return False
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM task_segments WHERE task_id=? AND segment_index=?",
                (task_id, segment_index),
            )
            segment = cur.fetchone()
            if not segment:
                return False
            if asset_type == "image":
                cur.execute(
                    """UPDATE task_segments
                       SET selected_image_asset_id=?, image_path=?, image_url=?,
                           image_prompt=COALESCE(NULLIF(?, ''), image_prompt),
                           image_status='completed', image_error=NULL,
                           image_error_code=NULL, image_error_meta_json=NULL,
                           updated_at=datetime('now','localtime')
                       WHERE task_id=? AND segment_index=?""",
                    (
                        asset.get("asset_id"), asset.get("path"), asset.get("url"),
                        asset.get("prompt"),
                        task_id, segment_index,
                    ),
                )
            else:
                cur.execute(
                    """UPDATE task_segments
                       SET selected_audio_asset_id=?, audio_path=?, audio_url=?,
                           audio_voice_type=?, audio_status='completed',
                           audio_error=NULL, audio_error_code=NULL,
                           audio_error_meta_json=NULL, audio_mismatch_confirmed=?,
                           updated_at=datetime('now','localtime')
                       WHERE task_id=? AND segment_index=?""",
                    (
                        asset.get("asset_id"), asset.get("path"), asset.get("url"),
                        asset.get("voice_type"), int(bool(confirm_text_mismatch)),
                        task_id, segment_index,
                    ),
                )
            conn.commit()
            return cur.rowcount > 0
        except Exception as exc:
            conn.rollback()
            logger.error("选择分镜素材失败: %s", exc)
            return False
        finally:
            conn.close()

    def backfill_selected_asset_ids(self, task_id: str) -> None:
        """Idempotently align each segment pointer with its current media path."""
        if not self._initialized:
            self._init_db()
        if not self._initialized:
            return
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM task_segments WHERE task_id=?", (task_id,)
            ).fetchall()
            for segment in rows:
                for asset_type, path_column, selected_column in (
                    ("image", "image_path", "selected_image_asset_id"),
                    ("audio", "audio_path", "selected_audio_asset_id"),
                ):
                    if not segment[path_column]:
                        continue
                    selected_path = None
                    if segment[selected_column]:
                        selected = cur.execute(
                            "SELECT path FROM task_assets WHERE task_id=? AND asset_id=?",
                            (task_id, segment[selected_column]),
                        ).fetchone()
                        selected_path = selected["path"] if selected else None
                    if selected_path == segment[path_column]:
                        continue
                    asset = cur.execute(
                        """SELECT asset_id FROM task_assets
                           WHERE task_id=? AND asset_type=? AND path=?
                           ORDER BY id DESC LIMIT 1""",
                        (task_id, asset_type, segment[path_column]),
                    ).fetchone()
                    if asset:
                        cur.execute(
                            f"UPDATE task_segments SET {selected_column}=? "
                            "WHERE task_id=? AND segment_index=?",
                            (asset["asset_id"], task_id, segment["segment_index"]),
                        )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _template_row(row) -> Dict:
        if not row:
            return {}
        item = dict(row)
        for source, target in (
            ("tts_options_json", "tts_options"),
            ("subtitle_options_json", "subtitle_options"),
            ("generation_options_json", "generation_options"),
        ):
            try:
                item[target] = json.loads(item.get(source) or "{}")
            except (TypeError, ValueError):
                item[target] = {}
        item["is_default"] = bool(item.get("is_default"))
        return item

    def list_production_templates(self) -> List[Dict]:
        with self.get_connection() as conn:
            if not conn:
                return []
            rows = conn.execute(
                "SELECT * FROM production_templates ORDER BY is_default DESC, updated_at DESC"
            ).fetchall()
            return [self._template_row(row) for row in rows]

    def create_production_template(self, values: Dict) -> Dict:
        template_id = uuid.uuid4().hex
        with self.get_connection() as conn:
            if not conn:
                return {}
            cur = conn.cursor()
            if values.get("is_default"):
                cur.execute("UPDATE production_templates SET is_default=0")
            cur.execute(
                """INSERT INTO production_templates
                   (template_id, name, description, visual_style, text_style, ratio,
                    voice_type, tts_options_json, subtitle_options_json,
                    generation_options_json, is_default)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    template_id, values.get("name") or "未命名模板",
                    values.get("description"), values.get("visual_style") or "电影质感",
                    values.get("text_style") or "知识科普", values.get("ratio") or "16:9",
                    values.get("voice_type"), json.dumps(values.get("tts_options") or {}, ensure_ascii=False),
                    json.dumps(values.get("subtitle_options") or {}, ensure_ascii=False),
                    json.dumps(values.get("generation_options") or {}, ensure_ascii=False),
                    int(bool(values.get("is_default"))),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM production_templates WHERE template_id=?", (template_id,)
            ).fetchone()
            return self._template_row(row)

    def update_production_template(self, template_id: str, values: Dict) -> Dict:
        mapping = {
            "name": "name", "description": "description",
            "visual_style": "visual_style", "text_style": "text_style",
            "ratio": "ratio", "voice_type": "voice_type",
            "tts_options": "tts_options_json",
            "subtitle_options": "subtitle_options_json",
            "generation_options": "generation_options_json",
            "is_default": "is_default",
        }
        with self.get_connection() as conn:
            if not conn:
                return {}
            cur = conn.cursor()
            if values.get("is_default"):
                cur.execute("UPDATE production_templates SET is_default=0")
            sets, params = [], []
            for key, column in mapping.items():
                if key not in values:
                    continue
                value = values[key]
                if key in {"tts_options", "subtitle_options", "generation_options"}:
                    value = json.dumps(value or {}, ensure_ascii=False)
                elif key == "is_default":
                    value = int(bool(value))
                sets.append(f"{column}=?")
                params.append(value)
            if sets:
                params.append(template_id)
                cur.execute(
                    f"UPDATE production_templates SET {', '.join(sets)}, "
                    "updated_at=datetime('now','localtime') WHERE template_id=?",
                    params,
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM production_templates WHERE template_id=?", (template_id,)
            ).fetchone()
            return self._template_row(row)

    def delete_production_template(self, template_id: str) -> bool:
        with self.get_connection() as conn:
            if not conn:
                return False
            cur = conn.execute(
                "DELETE FROM production_templates WHERE template_id=?", (template_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _decode_batch_row(row) -> Dict:
        if not row:
            return {}
        data = dict(row)
        try:
            data["config"] = json.loads(data.pop("config_json", "{}") or "{}")
        except (TypeError, ValueError):
            data["config"] = {}
        data["cancel_requested"] = bool(data.get("cancel_requested"))
        return data

    @classmethod
    def _decode_batch_item_row(cls, row) -> Dict:
        if not row:
            return {}
        data = dict(row)
        raw_meta = data.pop("error_meta_json", None)
        if raw_meta:
            try:
                raw_meta = json.loads(raw_meta)
            except (TypeError, ValueError):
                raw_meta = None
        if data.get("error") or data.get("error_code") or raw_meta:
            code = normalize_error_code(data.get("error_code"), has_error=True)
            data["error_code"] = (code or ErrorCode.UNKNOWN).value
            data["error_meta"] = normalize_error_metadata(
                data["error_code"], raw_meta
            )
            data["error"] = data["error_meta"]["safe_message"]
        else:
            data["error_meta"] = None
        return data

    @staticmethod
    def _refresh_batch_status_cursor(cur, batch_id: str) -> None:
        batch = cur.execute(
            "SELECT cancel_requested FROM task_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if not batch:
            return
        counts = {
            row["status"]: int(row["count"])
            for row in cur.execute(
                """SELECT status, COUNT(*) AS count FROM task_batch_items
                   WHERE batch_id=? GROUP BY status""",
                (batch_id,),
            ).fetchall()
        }
        queued = counts.get("queued", 0)
        running = counts.get("running", 0)
        failed = counts.get("failed", 0)
        awaiting = counts.get("awaiting_confirmation", 0)
        cancelled = counts.get("cancelled", 0)
        total = sum(counts.values())
        cancel_requested = bool(batch["cancel_requested"])
        completed = False
        if running:
            status = "running"
        elif queued:
            status = "queued"
        elif cancel_requested:
            status = "cancelled"
            completed = True
        elif total and awaiting == total:
            status = "completed"
            completed = True
        elif failed or cancelled:
            status = "completed_with_errors"
            completed = True
        else:
            status = "queued"
        cur.execute(
            """UPDATE task_batches SET status=?,
               completed_at=CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END,
               updated_at=datetime('now','localtime') WHERE batch_id=?""",
            (status, int(completed), batch_id),
        )

    def create_batch(
        self,
        batch_id: str,
        items: List[Dict],
        config: Dict,
        concurrency: int,
    ) -> Dict:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                """INSERT INTO task_batches
                   (batch_id, status, concurrency, config_json, total_count)
                   VALUES (?, 'queued', ?, ?, ?)""",
                (
                    batch_id,
                    int(concurrency),
                    json.dumps(config or {}, ensure_ascii=False),
                    len(items),
                ),
            )
            for position, item in enumerate(items):
                cur.execute(
                    """INSERT INTO task_batch_items
                       (item_id, batch_id, position, name, theme, normalized_theme)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        item["item_id"],
                        batch_id,
                        position,
                        item.get("name"),
                        item["theme"],
                        item["normalized_theme"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_batch(batch_id)

    def list_batches(self, limit: int = 50, offset: int = 0) -> List[Dict]:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT b.*,
                          SUM(CASE WHEN i.status='queued' THEN 1 ELSE 0 END) AS queued_count,
                          SUM(CASE WHEN i.status='running' THEN 1 ELSE 0 END) AS running_count,
                          SUM(CASE WHEN i.status='awaiting_confirmation' THEN 1 ELSE 0 END) AS awaiting_confirmation_count,
                          SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END) AS failed_count,
                          SUM(CASE WHEN i.status='cancelled' THEN 1 ELSE 0 END) AS cancelled_count
                   FROM task_batches b
                   LEFT JOIN task_batch_items i ON i.batch_id=b.batch_id
                   GROUP BY b.batch_id
                   ORDER BY b.created_at DESC, b.id DESC LIMIT ? OFFSET ?""",
                (max(1, min(200, int(limit))), max(0, int(offset))),
            ).fetchall()
            return [self._decode_batch_row(row) for row in rows]
        finally:
            conn.close()

    def get_batch(self, batch_id: str) -> Dict:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            batch = self._decode_batch_row(row)
            if not batch:
                return {}
            item_rows = conn.execute(
                """SELECT * FROM task_batch_items WHERE batch_id=?
                   ORDER BY position ASC, id ASC""",
                (batch_id,),
            ).fetchall()
            batch["items"] = [self._decode_batch_item_row(item) for item in item_rows]
            counts = {status: 0 for status in (
                "queued", "running", "awaiting_confirmation", "failed", "cancelled"
            )}
            for item in batch["items"]:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
            batch["counts"] = counts
            return batch
        finally:
            conn.close()

    def get_batch_item(self, item_id: str) -> Dict:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT i.*, b.cancel_requested, b.config_json, b.concurrency
                   FROM task_batch_items i JOIN task_batches b ON b.batch_id=i.batch_id
                   WHERE i.item_id=?""",
                (item_id,),
            ).fetchone()
            data = self._decode_batch_item_row(row)
            if data:
                try:
                    data["config"] = json.loads(data.pop("config_json", "{}") or "{}")
                except (TypeError, ValueError):
                    data["config"] = {}
                data["cancel_requested"] = bool(data.get("cancel_requested"))
            return data
        finally:
            conn.close()

    def claim_next_batch_item(self, *, global_concurrency: int = 3) -> Dict:
        if not self._initialized:
            self._init_db()
        global_concurrency = max(1, min(3, int(global_concurrency)))
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                """SELECT i.item_id, i.batch_id
                   FROM task_batch_items i
                   JOIN task_batches b ON b.batch_id=i.batch_id
                   WHERE i.status='queued' AND b.cancel_requested=0
                     AND (SELECT COUNT(*) FROM task_batch_items global_active
                          WHERE global_active.status='running') < ?
                     AND (SELECT COUNT(*) FROM task_batch_items active
                          WHERE active.batch_id=i.batch_id AND active.status='running') < b.concurrency
                   ORDER BY b.created_at ASC, b.id ASC, i.position ASC, i.id ASC
                   LIMIT 1""",
                (global_concurrency,),
            ).fetchone()
            if not row:
                conn.commit()
                return {}
            cur.execute(
                """UPDATE task_batch_items SET status='running', attempt=attempt+1,
                   started_at=datetime('now','localtime'), completed_at=NULL,
                   updated_at=datetime('now','localtime')
                   WHERE item_id=? AND status='queued'""",
                (row["item_id"],),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {}
            self._refresh_batch_status_cursor(cur, row["batch_id"])
            conn.commit()
            return self.get_batch_item(row["item_id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_batch_item_task(self, item_id: str, task_id: str) -> bool:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """UPDATE task_batch_items SET task_id=?, updated_at=datetime('now','localtime')
                   WHERE item_id=? AND status='running'""",
                (task_id, item_id),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def reserve_batch_item_task(self, item_id: str, proposed_task_id: str) -> Optional[str]:
        """Persist one task identity before task creation so crash recovery is idempotent."""
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT status, task_id FROM task_batch_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if not row or row["status"] != "running":
                conn.rollback()
                return None
            task_id = row["task_id"] or str(proposed_task_id)
            if not row["task_id"]:
                cur.execute(
                    """UPDATE task_batch_items SET task_id=?,
                       updated_at=datetime('now','localtime')
                       WHERE item_id=? AND status='running' AND task_id IS NULL""",
                    (task_id, item_id),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
            conn.commit()
            return task_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def try_acquire_batch_scheduler_lock(self, owner_id: str) -> bool:
        """Hold one scheduler owner per local database until process exit or release."""
        owner_id = str(owner_id)
        with self._batch_scheduler_lock_guard:
            if owner_id in self._batch_scheduler_lock_handles:
                return True
            lock_path = DB_PATH.with_name(f".{DB_PATH.name}.batch-scheduler.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    backend = "fcntl"
                elif msvcrt is not None:  # pragma: no cover - Windows fallback
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    backend = "msvcrt"
                else:  # pragma: no cover - unsupported platform
                    logger.error("当前平台不支持批量调度器进程锁")
                    handle.close()
                    return False
            except (BlockingIOError, OSError):
                handle.close()
                return False
            self._batch_scheduler_lock_handles[owner_id] = (backend, handle)
            return True

    def release_batch_scheduler_lock(self, owner_id: str) -> bool:
        """Release a lock explicitly; process termination also releases it safely."""
        with self._batch_scheduler_lock_guard:
            entry = self._batch_scheduler_lock_handles.pop(str(owner_id), None)
            if entry is None:
                return False
            backend, handle = entry
            try:
                if backend == "fcntl":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows fallback
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()
            return True

    @contextmanager
    def batch_launch_guard(self):
        """Serialize batch cancellation with provider launch across API processes."""
        with self._batch_launch_thread_lock:
            depth = getattr(self._batch_launch_local, "depth", 0)
            if depth:
                self._batch_launch_local.depth = depth + 1
                try:
                    yield
                finally:
                    self._batch_launch_local.depth = depth
                return

            lock_path = DB_PATH.with_name(f".{DB_PATH.name}.batch-launch.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    backend = "fcntl"
                elif msvcrt is not None:  # pragma: no cover - Windows fallback
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    backend = "msvcrt"
                else:  # pragma: no cover - unsupported platform
                    raise RuntimeError("当前平台不支持批量启动进程锁")
                self._batch_launch_local.depth = 1
                try:
                    yield
                finally:
                    self._batch_launch_local.depth = 0
                    if backend == "fcntl":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    else:  # pragma: no cover - Windows fallback
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()

    def update_batch_item_status(
        self,
        item_id: str,
        status: str,
        *,
        error: str = None,
        error_code: str = None,
        error_meta: Dict = None,
    ) -> bool:
        allowed = {"queued", "running", "awaiting_confirmation", "failed", "cancelled"}
        if status not in allowed:
            raise ValueError("批次项目状态无效")
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT batch_id FROM task_batch_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            safe_error, encoded_code, encoded_meta = self._encode_error_record(
                error, error_code, error_meta
            )
            terminal = status in {"awaiting_confirmation", "failed", "cancelled"}
            cur.execute(
                """UPDATE task_batch_items SET status=?, error=?, error_code=?,
                   error_meta_json=?,
                   completed_at=CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END,
                   updated_at=datetime('now','localtime') WHERE item_id=?""",
                (
                    status,
                    safe_error if safe_error is not None else error,
                    encoded_code,
                    encoded_meta,
                    int(terminal),
                    item_id,
                ),
            )
            self._refresh_batch_status_cursor(cur, row["batch_id"])
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_batch(self, batch_id: str) -> Dict:
        with self.batch_launch_guard():
            return self._cancel_batch_locked(batch_id)

    def _cancel_batch_locked(self, batch_id: str) -> Dict:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            batch = cur.execute(
                "SELECT status FROM task_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                conn.rollback()
                return {}
            if batch["status"] in {"completed", "completed_with_errors", "cancelled"}:
                conn.commit()
                return {"batch_id": batch_id, "running": []}
            cur.execute(
                """UPDATE task_batches SET cancel_requested=1,
                   updated_at=datetime('now','localtime') WHERE batch_id=?""",
                (batch_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {}
            running = [
                dict(row) for row in cur.execute(
                    """SELECT item_id, task_id FROM task_batch_items
                       WHERE batch_id=? AND status='running'""",
                    (batch_id,),
                ).fetchall()
            ]
            cur.execute(
                """UPDATE task_batch_items SET status='cancelled',
                   completed_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime')
                   WHERE batch_id=? AND status='queued'""",
                (batch_id,),
            )
            self._refresh_batch_status_cursor(cur, batch_id)
            conn.commit()
            return {"batch_id": batch_id, "running": running}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def retry_failed_batch_items(self, batch_id: str) -> int:
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            batch = cur.execute(
                "SELECT cancel_requested FROM task_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not batch:
                conn.rollback()
                return -1
            if batch["cancel_requested"]:
                conn.rollback()
                return -2
            cur.execute(
                """UPDATE task_batch_items SET status='queued', error=NULL,
                   error_code=NULL, error_meta_json=NULL, completed_at=NULL,
                   updated_at=datetime('now','localtime')
                   WHERE batch_id=? AND status='failed'""",
                (batch_id,),
            )
            count = cur.rowcount
            self._refresh_batch_status_cursor(cur, batch_id)
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def recover_batch_items(self) -> int:
        """Requeue persisted running items; their existing task_id is retained."""
        if not self._initialized:
            self._init_db()
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            batches = [
                row["batch_id"] for row in cur.execute(
                    "SELECT DISTINCT batch_id FROM task_batch_items WHERE status='running'"
                ).fetchall()
            ]
            cur.execute(
                """UPDATE task_batch_items SET status=CASE
                       WHEN batch_id IN (SELECT batch_id FROM task_batches WHERE cancel_requested=1)
                       THEN 'cancelled' ELSE 'queued' END,
                   completed_at=CASE
                       WHEN batch_id IN (SELECT batch_id FROM task_batches WHERE cancel_requested=1)
                       THEN datetime('now','localtime') ELSE NULL END,
                   updated_at=datetime('now','localtime') WHERE status='running'"""
            )
            count = cur.rowcount
            for batch_id in batches:
                self._refresh_batch_status_cursor(cur, batch_id)
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_task_activity(self, limit: int = 10) -> List[Dict]:
        """Return every incomplete task, then export-ready tasks completed in the last 30 days."""
        with self.get_connection() as conn:
            if not conn:
                return []
            incomplete_count = int(conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE status NOT IN ('completed', 'cancelled', 'deleting')"""
            ).fetchone()[0])
            effective_limit = max(int(limit or 0), incomplete_count)
            rows = conn.execute(
                """SELECT t.*,
                          COUNT(s.id) AS segments_total,
                          SUM(CASE WHEN TRIM(COALESCE(s.image_prompt, '')) != '' THEN 1 ELSE 0 END) AS prompts_ready,
                          SUM(CASE WHEN s.image_status IN ('completed','stale') THEN 1 ELSE 0 END) AS images_ready,
                          SUM(CASE WHEN s.audio_status IN ('completed','stale') THEN 1 ELSE 0 END) AS audio_ready,
                          o.operation_id, o.kind AS operation_kind, o.state AS operation_state,
                          o.completed_count, o.failed_count, o.targets_json
                   FROM tasks t
                   LEFT JOIN task_segments s ON s.task_id=t.task_id
                   LEFT JOIN task_operations o ON o.id=(
                       SELECT oo.id FROM task_operations oo
                       WHERE oo.task_id=t.task_id AND oo.state IN ('pending','running')
                       ORDER BY oo.id DESC LIMIT 1
                   )
                   WHERE t.status NOT IN ('cancelled', 'deleting')
                     AND (
                         t.status != 'completed'
                         OR (
                             t.exported_at IS NULL
                             AND t.completed_at IS NOT NULL
                             AND t.completed_at >= datetime('now','localtime','-30 days')
                         )
                     )
                   GROUP BY t.task_id
                   ORDER BY CASE WHEN t.status='completed' THEN 1 ELSE 0 END,
                            CASE WHEN t.status!='completed' THEN t.updated_at END DESC,
                            CASE WHEN t.status='completed' THEN t.completed_at END DESC,
                            t.id DESC
                   LIMIT ?""",
                (effective_limit,),
            ).fetchall()
            return [self._decode_task_error(dict(row)) for row in rows]


# 全局 SQLite 客户端实例
sqlite_client = SQLiteClient()

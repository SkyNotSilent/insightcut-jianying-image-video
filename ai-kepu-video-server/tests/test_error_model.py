import asyncio
import errno
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.error_model import ErrorCode, classify_exception, make_safe_error, sanitize_text
from src.api import routes
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


class FakeResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class FakeProviderError(Exception):
    def __init__(self, message, *, status_code=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = FakeResponse(status_code, headers=headers, text=message)


@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    [
        (FakeProviderError("unauthorized", status_code=401), ErrorCode.AUTH, False),
        (FakeProviderError("forbidden", status_code=403), ErrorCode.AUTH, False),
        (FakeProviderError("conflict", status_code=409), ErrorCode.CONFLICT, False),
        (FakeProviderError("bad gateway", status_code=502), ErrorCode.PROVIDER_ERROR, True),
        (TimeoutError("request timed out"), ErrorCode.TIMEOUT, True),
        (ConnectionError("connection refused"), ErrorCode.NETWORK, True),
        (OSError(errno.ENOSPC, "disk full"), ErrorCode.DISK, False),
        (ValueError("LLM API Key 未配置"), ErrorCode.CONFIG_MISSING, False),
        (asyncio.CancelledError(), ErrorCode.CANCELLED, False),
        (RuntimeError("unexpected"), ErrorCode.UNKNOWN, False),
    ],
)
def test_classifies_stable_error_codes(error, expected_code, retryable):
    classified = classify_exception(error, provider="test-provider")

    assert classified.code is expected_code
    assert classified.retryable is retryable
    assert classified.safe_message
    assert classified.provider == "test-provider"


def test_rate_limit_extracts_retry_after_and_safe_request_metadata():
    error = FakeProviderError(
        "rate limited",
        status_code=429,
        headers={"Retry-After": "17", "x-request-id": "req_abc-123"},
    )

    classified = classify_exception(error, provider="agnes")

    assert classified.code is ErrorCode.RATE_LIMIT
    assert classified.retryable is True
    assert classified.retry_after_seconds == 17
    assert classified.http_status == 429
    assert classified.request_id == "req_abc-123"
    assert classified.to_record() == {
        "error_code": "rate_limit",
        "error_meta": {
            "retryable": True,
            "retry_after_seconds": 17,
            "safe_message": classified.safe_message,
            "provider": "agnes",
            "http_status": 429,
            "request_id": "req_abc-123",
        },
    }


def test_content_policy_error_is_actionable_and_not_retryable():
    safe = make_safe_error(
        ErrorCode.CONTENT_POLICY,
        provider="agnes",
        http_status=400,
        request_id="req-policy-safe",
    )

    assert safe.code is ErrorCode.CONTENT_POLICY
    assert safe.retryable is False
    assert "提示词" in safe.safe_message
    assert safe.metadata()["request_id"] == "req-policy-safe"


def test_export_job_transmits_structured_error_without_request_payload(
    monkeypatch,
):
    secret = "sk-export-secret-0123456789"
    data_url = "data:audio/wav;base64,UklGRmZha2U="
    routes.EXPORT_JOBS.clear()
    job = routes._create_export_job(
        "export-safe",
        "mp4",
        {"api_key": secret, "audio": data_url, "use_preview": True},
    )
    fake_task = SimpleNamespace(
        task_id="export-safe",
        name="安全导出",
        theme="安全导出",
        result=SimpleNamespace(draft_path="/tmp/draft"),
    )
    monkeypatch.setattr(routes.task_manager, "get_task", lambda _task_id: fake_task)
    monkeypatch.setattr(routes.mysql_client, "get_segments", lambda _task_id: [{"text": "一段"}])

    def fail_export(*_args, **_kwargs):
        raise FakeProviderError(
            f"Authorization: Bearer {secret}; body={data_url}",
            status_code=401,
            headers={"x-request-id": "req-export-safe"},
        )

    monkeypatch.setattr(routes, "_export_mp4", fail_export)

    routes._run_export_job(job["job_id"], "mp4", True, {"api_key": secret})

    stored = routes._export_job_snapshot(job["job_id"])
    serialized = json.dumps(stored, ensure_ascii=False)
    assert stored["status"] == "failed"
    assert stored["error_code"] == "auth"
    assert stored["error"] == stored["error_meta"]["safe_message"]
    assert stored["error_meta"]["request_id"] == "req-export-safe"
    assert secret not in serialized
    assert data_url not in serialized
    assert "api_key" not in stored["params"]


def test_workspace_completed_local_asset_exposes_storage_warning_not_failure(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    warning = classify_exception(OSError(errno.ENOSPC, "disk full"), provider="local_storage")
    request = Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("localhost", 2002),
        "path": "/workspace",
        "root_path": "",
        "query_string": b"",
        "headers": [],
    })

    payload = routes._workspace_segment_payload({
        "id": 1,
        "task_id": "storage-warning",
        "segment_index": 0,
        "text": "第一段",
        "image_prompt": "prompt",
        "prompt_status": "completed",
        "image_path": str(image),
        "image_url": "https://example.invalid/image.png",
        "image_status": "completed",
        "image_error": warning.safe_message,
        "image_error_code": warning.code.value,
        "image_error_meta": warning.metadata(),
        "audio_status": "pending",
    }, request)

    assert payload["image_status"] == "completed"
    assert payload["image_error"] is None
    assert payload["image_storage_warning"]["safe_message"] == warning.safe_message
    assert payload["image_storage_warning"]["provider"] == "local_storage"


def test_http_409_response_keeps_detail_and_adds_conflict_structure():
    from api_server import structured_http_error

    response = asyncio.run(
        structured_http_error(
            None,
            HTTPException(status_code=409, detail="预案已发生变化，请刷新后重试"),
        )
    )
    payload = json.loads(response.body)

    assert response.status_code == 409
    assert payload["detail"] == "预案已发生变化，请刷新后重试"
    assert payload["error_code"] == "conflict"
    assert payload["error_meta"]["retryable"] is False


def test_http_structured_detail_is_preserved_and_credential_fields_are_removed():
    from api_server import structured_http_error

    secret = "sk-http-detail-0123456789"
    data_url = "data:audio/wav;base64,UklGRmZha2U="
    response = asyncio.run(
        structured_http_error(
            None,
            HTTPException(
                status_code=409,
                detail={
                    "code": "operation_running",
                    "message": "当前已有素材操作正在执行",
                    "operation_id": "op-safe-1",
                    "recovery": {
                        "mode": "needs_prompt",
                        "targets": [{"segment_index": 2, "asset_type": "image"}],
                    },
                    "api_key": secret,
                    "debug": f"Authorization: Bearer {secret}; body={data_url}",
                },
                headers={
                    "X-Error-Code": "conflict",
                    "Authorization": f"Bearer {secret}",
                    "Retry-After": "3",
                },
            ),
        )
    )
    payload = json.loads(response.body)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["detail"]["code"] == "operation_running"
    assert payload["detail"]["operation_id"] == "op-safe-1"
    assert payload["detail"]["recovery"]["mode"] == "needs_prompt"
    assert payload["detail"]["recovery"]["targets"] == [
        {"segment_index": 2, "asset_type": "image"}
    ]
    assert "api_key" not in payload["detail"]
    assert response.headers["retry-after"] == "3"
    assert "authorization" not in response.headers
    assert secret not in serialized
    assert data_url not in serialized
    assert "Authorization" not in serialized
    assert "Bearer " not in serialized


def test_classifier_never_echoes_raw_exception_or_sensitive_payloads():
    secret = "sk-test-0123456789abcdefghijklmnopqrstuvwxyz"
    data_url = "data:audio/wav;base64,UklGRmZha2UtYXVkaW8="
    message = (
        f"Authorization: Bearer {secret}; api_key={secret}; "
        f"body={{'voice': '{data_url}'}}"
    )
    error = FakeProviderError(
        message,
        status_code=401,
        headers={
            "Authorization": f"Bearer {secret}",
            "x-request-id": "req-safe",
        },
    )

    classified = classify_exception(error, provider="openai")
    serialized = json.dumps(classified.to_record(), ensure_ascii=False)

    assert secret not in serialized
    assert data_url not in serialized
    assert message not in serialized
    assert "Authorization" not in serialized
    assert "Bearer" not in serialized
    assert sanitize_text(message).count("[REDACTED]") >= 2


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


def test_structured_errors_round_trip_for_task_segment_and_operation(temp_db):
    temp_db.create_task("error-task", "主题", "知识科普|电影质感", 100)
    temp_db.save_segments(
        "error-task",
        [{"segment_index": 0, "text": "第一段"}],
    )
    safe_error = classify_exception(
        FakeProviderError(
            "provider raw body must not survive",
            status_code=429,
            headers={"Retry-After": "9", "x-request-id": "req-roundtrip"},
        ),
        provider="agnes",
    )

    assert temp_db.update_task_status(
        "error-task",
        "interrupted",
        "image_generation",
        error=safe_error.safe_message,
        error_code=safe_error.code.value,
        error_meta=safe_error.metadata(),
    )
    assert temp_db.update_segment_checkpoint(
        "error-task",
        0,
        image_status="failed",
        image_error=safe_error.safe_message,
        image_error_code=safe_error.code.value,
        image_error_meta=safe_error.metadata(),
    )
    operation = temp_db.create_task_operation(
        "error-task",
        "retry_assets",
        "error-task:retry:0:image",
        "snapshot-1",
        [{"segment_index": 0, "asset_type": "image"}],
    )["operation"]
    assert temp_db.update_task_operation(
        operation["operation_id"],
        state="failed",
        error=safe_error.safe_message,
        error_code=safe_error.code.value,
        error_meta=safe_error.metadata(),
    )

    task = temp_db.get_task("error-task")
    segment = temp_db.get_segments("error-task")[0]
    operation = temp_db.get_task_operation(operation["operation_id"])

    assert task["error_code"] == "rate_limit"
    assert task["error_meta"] == safe_error.metadata()
    assert json.loads(task["error_meta_json"]) == safe_error.metadata()
    assert segment["image_error_code"] == "rate_limit"
    assert segment["image_error_meta"] == safe_error.metadata()
    assert json.loads(segment["image_error_meta_json"]) == safe_error.metadata()
    assert operation["error_code"] == "rate_limit"
    assert operation["error_meta"] == safe_error.metadata()
    assert json.loads(operation["error_meta_json"]) == safe_error.metadata()


def test_legacy_errors_migrate_once_and_read_as_unknown(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-errors.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE tasks (
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
            extract_path TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            completed_at TEXT
        );
        CREATE TABLE task_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            segment_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            image_prompt TEXT,
            image_path TEXT,
            image_url TEXT,
            image_status TEXT DEFAULT 'completed',
            image_error TEXT,
            audio_path TEXT,
            audio_url TEXT,
            audio_status TEXT DEFAULT 'completed',
            audio_error TEXT,
            duration REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(task_id, segment_index)
        );
        CREATE TABLE task_operations (
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
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO tasks (task_id, theme, style, length, error)
        VALUES ('legacy-error', '旧任务', '电影质感', 100, '旧错误原文');
        INSERT INTO task_segments
            (task_id, segment_index, text, image_error)
        VALUES ('legacy-error', 0, '第一段', '旧图片错误');
        INSERT INTO task_operations
            (operation_id, task_id, kind, state, idempotency_key, error)
        VALUES ('legacy-op', 'legacy-error', 'retry_assets', 'failed', 'legacy-key', '旧操作错误');
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", db_path)

    first_client = SQLiteClient()
    task = first_client.get_task("legacy-error")
    segment = first_client.get_segments("legacy-error")[0]
    operation = first_client.get_task_operation("legacy-op")
    second_client = SQLiteClient()
    second_client.get_task("legacy-error")

    assert task["error_code"] == "unknown"
    assert task["error_meta"]["safe_message"]
    assert segment["image_error_code"] == "unknown"
    assert segment["image_error_meta"]["safe_message"]
    assert operation["error_code"] == "unknown"
    assert operation["error_meta"]["safe_message"]

    with sqlite3.connect(db_path) as migrated:
        versions = migrated.execute(
            """SELECT version, COUNT(*) FROM schema_migrations
               WHERE version LIKE '20260819_structured_errors_%'
               GROUP BY version ORDER BY version"""
        ).fetchall()
        task_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        segment_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(task_segments)")
        }
        operation_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(task_operations)")
        }

    assert versions == [
        ("20260819_structured_errors_operations", 1),
        ("20260819_structured_errors_segments", 1),
        ("20260819_structured_errors_tasks", 1),
    ]
    assert {"error_code", "error_meta_json"} <= task_columns
    assert {
        "prompt_error_code", "prompt_error_meta_json",
        "image_error_code", "image_error_meta_json",
        "audio_error_code", "audio_error_meta_json",
    } <= segment_columns
    assert {"error_code", "error_meta_json"} <= operation_columns


def test_legacy_error_sanitization_removes_secrets_from_all_durable_error_fields(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "legacy-sensitive-errors.db"
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", db_path)
    bootstrap = SQLiteClient()
    bootstrap.create_task("legacy-sensitive", "旧任务", "电影质感", 100)
    bootstrap.save_segments(
        "legacy-sensitive",
        [{"segment_index": 0, "text": "第一段"}],
    )
    operation = bootstrap.create_task_operation(
        "legacy-sensitive",
        "retry_assets",
        "legacy-sensitive-key",
        "snapshot-safe",
        [{"segment_index": 0, "asset_type": "image"}],
    )["operation"]

    secret = "sk-legacy-sensitive-0123456789"
    data_url = "data:audio/wav;base64,UklGRmxlZ2FjeQ=="
    raw = f"Authorization: Bearer {secret}; api_key={secret}; audio={data_url}"
    malicious_meta = json.dumps(
        {
            "safe_message": raw,
            "retryable": True,
            "provider": f"Authorization-{secret}",
            "request_id": f"Bearer-{secret}",
        },
        ensure_ascii=False,
    )
    malicious_targets = json.dumps(
        [{
            "segment_index": 0,
            "asset_type": "image",
            "status": "failed",
            "error": raw,
            "error_code": "auth",
            "error_meta": {"safe_message": raw, "retryable": False},
        }],
        ensure_ascii=False,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE tasks SET error=?, error_code=NULL, error_meta_json=? WHERE task_id=?",
            (raw, malicious_meta, "legacy-sensitive"),
        )
        connection.execute(
            """UPDATE task_segments
               SET prompt_error=?, prompt_error_code=NULL, prompt_error_meta_json=?,
                   image_error=?, image_error_code='auth', image_error_meta_json=?,
                   audio_error=?, audio_error_code=NULL, audio_error_meta_json=NULL
               WHERE task_id=? AND segment_index=0""",
            (
                raw,
                malicious_meta,
                raw,
                malicious_meta,
                raw,
                "legacy-sensitive",
            ),
        )
        connection.execute(
            """UPDATE task_operations
               SET state='failed', error=?, error_code='auth', error_meta_json=?,
                   targets_json=? WHERE operation_id=?""",
            (raw, malicious_meta, malicious_targets, operation["operation_id"]),
        )
        connection.execute(
            """INSERT INTO task_assets
               (asset_id, task_id, asset_type, source, status, error_message)
               VALUES ('legacy-asset', 'legacy-sensitive', 'image', 'generated', 'failed', ?)""",
            (raw,),
        )
        connection.execute(
            """INSERT INTO tts_voice_clones
               (clone_id, name, reference_path, status, error_message)
               VALUES ('legacy-clone', '旧音色', '/tmp/reference.wav', 'failed', ?)""",
            (raw,),
        )
        connection.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            ("20260819_sanitize_legacy_error_payloads_v1",),
        )
        connection.commit()

    migrated = SQLiteClient()
    task = migrated.get_task("legacy-sensitive")
    segment = migrated.get_segments("legacy-sensitive")[0]
    migrated_operation = migrated.get_task_operation(operation["operation_id"])
    assert task["error_code"] == "unknown"
    assert segment["prompt_error_code"] == "unknown"
    assert segment["image_error_code"] == "auth"
    assert segment["audio_error_code"] == "unknown"
    assert migrated_operation["error_code"] == "auth"
    assert migrated_operation["targets"][0]["error_code"] == "auth"

    # Starting a second client proves the cleanup marker is idempotent.
    SQLiteClient().get_task("legacy-sensitive")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        logical_dump = []
        for table_name in table_names:
            logical_dump.extend(
                dict(row) for row in connection.execute(f'SELECT * FROM "{table_name}"')
            )
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=?",
            ("20260819_sanitize_legacy_error_payloads_v1",),
        ).fetchone()[0]
    serialized = json.dumps(logical_dump, ensure_ascii=False)
    assert migration_count == 1
    assert secret not in serialized
    assert data_url not in serialized
    assert "Authorization:" not in serialized
    assert "Bearer " not in serialized

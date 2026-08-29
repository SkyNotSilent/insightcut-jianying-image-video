import asyncio
import threading
from datetime import datetime, timedelta

import pytest

from src.api import task_manager as task_manager_module
from src.api.models import TaskStatus
from src.api.task_manager import TaskManager
from src.api.task_runtime import TaskRuntimeRegistry
from src.api.task_sweeper import (
    DEFAULT_SWEEP_INTERVAL_SECONDS,
    SWEEP_INTERVAL_ENV,
    SweepResult,
    TaskSweeper,
)
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


@pytest.fixture
def sweeper_context(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    database = SQLiteClient()
    runtime = TaskRuntimeRegistry()
    monkeypatch.setattr(task_manager_module, "db_client", database)
    monkeypatch.setattr(task_manager_module, "task_runtime", runtime)
    manager = TaskManager()
    sweeper = TaskSweeper(
        manager=manager,
        database=database,
        runtime=runtime,
        interval_seconds=300,
    )
    return database, manager, runtime, sweeper


def _create_task(database, task_id, *, phase="planning", status="processing"):
    assert database.create_task(
        task_id,
        "主题",
        "知识科普|电影质感",
        100,
        execution_mode="review_first",
    )
    assert database.update_task_workflow(
        task_id,
        phase,
        status=status,
        current_step="text_generation",
    )


def _make_task_stale(database, task_id):
    stale_at = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE tasks SET updated_at=? WHERE task_id=?",
            (stale_at, task_id),
        )
        connection.commit()


def test_sweeper_interval_defaults_to_300_seconds_and_supports_environment(monkeypatch):
    monkeypatch.delenv(SWEEP_INTERVAL_ENV, raising=False)
    assert TaskSweeper().interval_seconds == DEFAULT_SWEEP_INTERVAL_SECONDS

    monkeypatch.setenv(SWEEP_INTERVAL_ENV, "12.5")
    assert TaskSweeper().interval_seconds == 12.5


def test_run_once_interrupts_stale_task_without_polling_api(
    sweeper_context, monkeypatch
):
    database, _manager, _runtime, sweeper = sweeper_context
    monkeypatch.setenv("TASK_STALE_TIMEOUT_SECONDS", "60")
    _create_task(database, "stale-task")
    _make_task_stale(database, "stale-task")

    first = sweeper.run_once()
    second = sweeper.run_once()

    task = database.get_task("stale-task")
    assert first.interrupted_tasks == 1
    assert first.interrupted_operations == 0
    assert second.interrupted_tasks == 0
    assert task["status"] == TaskStatus.INTERRUPTED.value
    assert "可继续生成" in task["error"]


@pytest.mark.parametrize(
    "phase",
    ["awaiting_confirmation", "awaiting_finalization"],
)
def test_waiting_for_user_phases_never_time_out(
    sweeper_context, monkeypatch, phase
):
    database, manager, _runtime, sweeper = sweeper_context
    monkeypatch.setenv("TASK_STALE_TIMEOUT_SECONDS", "60")
    task_id = f"waiting-{phase}"
    _create_task(database, task_id, phase=phase)
    _make_task_stale(database, task_id)

    result = sweeper.run_once()

    assert result.interrupted_tasks == 0
    assert database.get_task(task_id)["status"] == TaskStatus.PROCESSING.value
    assert manager.mark_orphaned_tasks_interrupted() == 0
    assert database.get_task(task_id)["workflow_phase"] == phase


def test_orphan_operation_is_interrupted_once_and_completed_target_is_untouched(
    sweeper_context, tmp_path
):
    database, _manager, _runtime, sweeper = sweeper_context
    task_id = "orphan-operation"
    _create_task(database, task_id, phase="repairing_assets")
    completed_image = tmp_path / "completed.png"
    completed_image.write_bytes(b"png")
    database.save_segments(
        task_id,
        [
            {
                "segment_index": 0,
                "text": "已完成",
                "image_prompt": "prompt-0",
                "image_path": str(completed_image),
                "image_status": "completed",
                "audio_status": "completed",
            },
            {
                "segment_index": 1,
                "text": "运行中",
                "image_prompt": "prompt-1",
                "image_status": "processing",
                "audio_status": "completed",
            },
        ],
    )
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE task_segments SET updated_at='2001-01-01 00:00:00' "
            "WHERE task_id=? AND segment_index=0",
            (task_id,),
        )
        connection.commit()
    targets = [
        {
            "segment_index": 0,
            "asset_type": "image",
            "status": "completed",
            "mode": "retry",
        },
        {
            "segment_index": 1,
            "asset_type": "image",
            "status": "processing",
            "mode": "retry",
        },
    ]
    created = database.create_task_operation(
        task_id,
        "retry_assets",
        "orphan-operation-key",
        "snapshot",
        targets,
    )
    operation_id = created["operation"]["operation_id"]
    assert database.update_task_operation(operation_id, state="running")

    first = sweeper.run_once()
    second = sweeper.run_once()

    operation = database.get_task_operation(operation_id)
    segments = database.get_segments(task_id)
    assert first.interrupted_operations == 1
    assert second.interrupted_operations == 0
    assert operation["state"] == "interrupted"
    assert operation["targets"][0]["status"] == "completed"
    assert operation["targets"][1]["status"] == "failed"
    assert segments[0]["image_status"] == "completed"
    assert segments[0]["image_path"] == str(completed_image)
    assert segments[0]["updated_at"] == "2001-01-01 00:00:00"
    assert segments[1]["image_status"] == "failed"
    task = database.get_task(task_id)
    assert task["status"] == TaskStatus.INTERRUPTED.value
    assert task["current_step"] == "asset_repair"


def test_registered_runtime_operation_is_not_treated_as_orphan(sweeper_context):
    database, _manager, runtime, sweeper = sweeper_context
    task_id = "live-operation"
    _create_task(database, task_id, phase="repairing_assets")
    created = database.create_task_operation(
        task_id,
        "retry_assets",
        "live-operation-key",
        "snapshot",
        [
            {
                "segment_index": 0,
                "asset_type": "image",
                "status": "processing",
            }
        ],
    )
    operation_id = created["operation"]["operation_id"]
    assert database.update_task_operation(operation_id, state="running")
    token = runtime.begin(task_id)

    result = sweeper.run_once()

    assert result.interrupted_operations == 0
    assert database.get_task_operation(operation_id)["state"] == "running"
    assert database.get_task(task_id)["status"] == TaskStatus.PROCESSING.value
    runtime.finish(task_id, token)


def test_periodic_scan_runs_on_one_daemon_thread_and_stops_explicitly():
    sweeper = TaskSweeper(interval_seconds=0.01)
    scanned = threading.Event()
    scan_threads = []

    def run_once():
        scan_threads.append(threading.current_thread())
        scanned.set()
        return SweepResult()

    sweeper.run_once = run_once

    assert sweeper.start() is True
    assert sweeper.start() is False
    assert scanned.wait(timeout=1)
    assert scan_threads[0] is not threading.main_thread()
    assert scan_threads[0].daemon is True

    sweeper.stop()
    assert sweeper.join(timeout=1) is True
    assert sweeper.is_running is False


def test_fastapi_lifespan_owns_sweeper_start_stop_and_join(monkeypatch):
    import api_server

    calls = []

    class FakeSweeper:
        def start(self):
            calls.append("start")
            return True

        def stop(self):
            calls.append("stop")

        def join(self, timeout):
            calls.append(("join", timeout, threading.current_thread().name))
            return True

    monkeypatch.setattr(api_server, "task_sweeper", FakeSweeper())
    monkeypatch.setattr(
        api_server.task_manager,
        "complete_deleting_tasks",
        lambda: calls.append("delete") or 0,
    )
    monkeypatch.setattr(
        api_server.task_manager,
        "mark_orphaned_tasks_interrupted",
        lambda: calls.append("interrupt") or 0,
    )
    monkeypatch.setattr(
        api_server.task_manager,
        "reconcile_completed_tasks",
        lambda: calls.append("reconcile") or 0,
    )

    async def exercise_lifespan():
        async with api_server.lifespan(api_server.app):
            calls.append("serving")

    asyncio.run(exercise_lifespan())

    labels = [item if isinstance(item, str) else item[0] for item in calls]
    assert labels == [
        "delete", "interrupt", "reconcile", "start", "serving", "stop", "join"
    ]
    join_call = next(item for item in calls if isinstance(item, tuple))
    assert join_call[1] == 30.0
    assert join_call[2] != threading.main_thread().name

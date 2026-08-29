import time

import pytest

from src.api import task_manager as task_manager_module
from src.api.batch_manager import BatchScheduler
from src.api.task_manager import TaskManager
from src.database import sqlite_client as sqlite_client_module
from src.database.sqlite_client import SQLiteClient


@pytest.fixture
def batch_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_client_module, "DB_PATH", tmp_path / "local.db")
    return SQLiteClient()


def _items(prefix, count):
    return [
        {
            "item_id": f"{prefix}-item-{index}",
            "theme": f"主题 {prefix} {index}",
            "normalized_theme": f"主题 {prefix} {index}".casefold(),
        }
        for index in range(count)
    ]


def _config():
    return {
        "style": "知识科普|电影质感",
        "ratio": "16:9",
        "length": 100,
        "voice_type": "doubao:test",
        "tts_options": {"speed_level": "normal", "volume_ratio": 1},
    }


def test_batch_creation_is_transactional_and_rejects_normalized_duplicates(batch_db):
    duplicate = _items("duplicate", 2)
    duplicate[1]["normalized_theme"] = duplicate[0]["normalized_theme"]
    with pytest.raises(Exception):
        batch_db.create_batch("batch-duplicate", duplicate, _config(), 1)
    assert batch_db.get_batch("batch-duplicate") == {}


def test_fifo_claim_honors_per_batch_concurrency(batch_db):
    batch_db.create_batch("batch-a", _items("a", 2), _config(), 1)
    batch_db.create_batch("batch-b", _items("b", 2), _config(), 1)

    first = batch_db.claim_next_batch_item()
    second = batch_db.claim_next_batch_item()
    third = batch_db.claim_next_batch_item()

    assert first["item_id"] == "a-item-0"
    assert second["item_id"] == "b-item-0"
    assert third == {}
    batch_db.update_batch_item_status(first["item_id"], "awaiting_confirmation")
    assert batch_db.claim_next_batch_item()["item_id"] == "a-item-1"


def test_cancel_keeps_running_checkpoint_and_cancels_unstarted_items(batch_db):
    batch_db.create_batch("batch-cancel", _items("cancel", 2), _config(), 1)
    running = batch_db.claim_next_batch_item()
    batch_db.set_batch_item_task(running["item_id"], "existing-task")

    result = batch_db.cancel_batch("batch-cancel")

    assert result["running"] == [{"item_id": "cancel-item-0", "task_id": "existing-task"}]
    batch = batch_db.get_batch("batch-cancel")
    assert batch["items"][0]["status"] == "running"
    assert batch["items"][0]["task_id"] == "existing-task"
    assert batch["items"][1]["status"] == "cancelled"
    batch_db.update_batch_item_status("cancel-item-0", "cancelled")
    assert batch_db.get_batch("batch-cancel")["status"] == "cancelled"


def test_restart_requeues_running_items_without_replacing_their_task(batch_db):
    batch_db.create_batch("batch-restart", _items("restart", 2), _config(), 1)
    running = batch_db.claim_next_batch_item()
    batch_db.set_batch_item_task(running["item_id"], "checkpoint-task")

    assert batch_db.recover_batch_items() == 1

    batch = batch_db.get_batch("batch-restart")
    assert batch["status"] == "queued"
    assert batch["items"][0]["status"] == "queued"
    assert batch["items"][0]["task_id"] == "checkpoint-task"


class _ConcurrencyDatabase:
    def __init__(self, count=4):
        self.items = [
            {
                "item_id": f"global-item-{index}",
                "theme": f"全局并发主题 {index}",
                "task_id": f"global-task-{index}",
                "cancel_requested": False,
                "config": {},
            }
            for index in range(count)
        ]
        self.claimed = []

    def claim_next_batch_item(self):
        if not self.items:
            return {}
        item = self.items.pop(0)
        self.claimed.append(item["item_id"])
        return item

    def get_batch_item(self, item_id):
        return {
            "item_id": item_id,
            "theme": item_id,
            "task_id": item_id.replace("item", "task"),
            "cancel_requested": False,
            "config": {},
        }

    def get_task(self, task_id):
        return {
            "task_id": task_id,
            "theme": task_id,
            "status": "pending",
            "length": 100,
            "ratio": "16:9",
        }

    def update_batch_item_status(self, *_args, **_kwargs):
        return True


class _StartingExecutor:
    def execute_task(self, **_kwargs):
        return True


def test_scheduler_never_dispatches_more_than_three_global_workers():
    database = _ConcurrencyDatabase(count=4)
    scheduler = BatchScheduler(
        database=database,
        manager=object(),
        executor=_StartingExecutor(),
        runtime=_Runtime(),
        global_concurrency=99,
        poll_interval=0.02,
    )

    scheduler._dispatch()
    try:
        assert scheduler.global_concurrency == 3
        assert database.claimed == [
            "global-item-0",
            "global-item-1",
            "global-item-2",
        ]
        assert len(scheduler._workers) == 3
    finally:
        scheduler.stop()
        for worker in list(scheduler._workers.values()):
            worker.join(1)


class _Runtime:
    def is_running(self, _task_id):
        return False

    def request_cancel(self, _task_id):
        return True


class _Executor:
    def __init__(self, database):
        self.database = database
        self.created = []
        self.resumed = []
        self.fail_first = True

    def execute_task(self, **kwargs):
        task_id = kwargs["task_id"]
        self.created.append(task_id)
        if self.fail_first:
            self.database.update_task_workflow(
                task_id, "planning", status="failed", current_step="text_generation"
            )
            self.fail_first = False
        else:
            self.database.update_task_workflow(
                task_id,
                "awaiting_confirmation",
                status="awaiting_confirmation",
                current_step="awaiting_confirmation",
            )
        return True

    def resume_task(self, task_id):
        self.resumed.append(task_id)
        self.database.update_task_workflow(
            task_id,
            "awaiting_confirmation",
            status="awaiting_confirmation",
            current_step="awaiting_confirmation",
        )
        return "started"

    def cancel_task(self, _task_id, _timeout):
        return True


def _wait_for(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_retry_reuses_original_task_and_checkpoint(batch_db, monkeypatch):
    monkeypatch.setattr(task_manager_module, "db_client", batch_db)
    manager = TaskManager()
    executor = _Executor(batch_db)
    scheduler = BatchScheduler(
        database=batch_db,
        manager=manager,
        executor=executor,
        runtime=_Runtime(),
        poll_interval=0.02,
    )
    batch_db.create_batch("batch-retry", _items("retry", 2), _config(), 1)
    scheduler.start()
    try:
        _wait_for(lambda: batch_db.get_batch("batch-retry")["counts"]["failed"] == 1)
        failed_item = batch_db.get_batch("batch-retry")["items"][0]
        original_task = failed_item["task_id"]
        assert scheduler.retry_failed("batch-retry") == 1
        _wait_for(lambda: batch_db.get_batch("batch-retry")["status"] == "completed")
    finally:
        scheduler.stop()
        scheduler.join(1)

    batch = batch_db.get_batch("batch-retry")
    assert batch["items"][0]["task_id"] == original_task
    assert executor.resumed == [original_task]
    assert len(executor.created) == 2
    assert all(item["status"] == "awaiting_confirmation" for item in batch["items"])

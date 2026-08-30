import asyncio
import json
import threading
import time

import httpx
import pytest
from pydantic import ValidationError

from src.api import task_manager as task_manager_module
from src.api.batch_manager import BatchScheduler
from src.api.models import CreateBatchRequest
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

    def claim_next_batch_item(self, **_kwargs):
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


def test_batch_request_accepts_exact_boundaries_and_rejects_out_of_range_values():
    for count in (2, 50):
        request = CreateBatchRequest(
            items=[{"theme": f"边界主题 {index}"} for index in range(count)],
            concurrency=3,
        )
        assert len(request.items) == count

    for count in (0, 1, 51):
        with pytest.raises(ValidationError):
            CreateBatchRequest(
                items=[{"theme": f"非法主题 {index}"} for index in range(count)]
            )
    for concurrency in (0, 4):
        with pytest.raises(ValidationError):
            CreateBatchRequest(
                items=[{"theme": "主题一"}, {"theme": "主题二"}],
                concurrency=concurrency,
            )
    with pytest.raises(ValidationError):
        CreateBatchRequest(items=[{"theme": "a" * 101}, {"theme": "主题二"}])


def test_batch_theme_folding_is_locale_independent_and_matches_the_web_contract():
    from src.api.routes import _normalize_batch_theme

    equivalent_pairs = [
        ("ＡＩ   助手", "ai 助手"),
        ("ASCII\tSPACE", "ascii space"),
    ]
    for left, right in equivalent_pairs:
        assert _normalize_batch_theme(left) == _normalize_batch_theme(right)
    distinct_pairs = [
        ("Straße", "STRASSE"),
        ("ς", "Σ"),
        ("ı", "I"),
        ("A B", "A\u0085B"),
        ("A B", "A\ufeffB"),
    ]
    for left, right in distinct_pairs:
        assert _normalize_batch_theme(left) != _normalize_batch_theme(right)


def test_persists_twenty_full_batches_and_keeps_global_claims_at_three(batch_db):
    for batch_index in range(20):
        batch_db.create_batch(
            f"batch-stress-{batch_index:02d}",
            _items(f"stress-{batch_index:02d}", 50),
            _config(),
            3,
        )

    assert len(batch_db.list_batches(limit=20)) == 20
    claimed = [batch_db.claim_next_batch_item() for _ in range(4)]
    assert all(claimed[index] for index in range(3))
    assert claimed[3] == {}


def test_terminal_batch_cancellation_is_an_idempotent_noop(batch_db):
    batch_db.create_batch("batch-terminal", _items("terminal", 2), _config(), 1)
    for _ in range(2):
        claimed = batch_db.claim_next_batch_item()
        batch_db.update_batch_item_status(
            claimed["item_id"], "awaiting_confirmation"
        )

    before = batch_db.get_batch("batch-terminal")
    result = batch_db.cancel_batch("batch-terminal")
    after = batch_db.get_batch("batch-terminal")

    assert result == {"batch_id": "batch-terminal", "running": []}
    assert before["status"] == after["status"] == "completed"
    assert after["cancel_requested"] is False
    assert after["counts"]["awaiting_confirmation"] == 2


def test_cancelling_completed_with_errors_preserves_retryability(batch_db):
    batch_db.create_batch("batch-retryable-terminal", _items("retryable", 2), _config(), 1)
    failed = batch_db.claim_next_batch_item()
    batch_db.update_batch_item_status(failed["item_id"], "failed", error="failed")
    successful = batch_db.claim_next_batch_item()
    batch_db.update_batch_item_status(successful["item_id"], "awaiting_confirmation")

    assert batch_db.get_batch("batch-retryable-terminal")["status"] == "completed_with_errors"
    batch_db.cancel_batch("batch-retryable-terminal")
    after = batch_db.get_batch("batch-retryable-terminal")

    assert after["status"] == "completed_with_errors"
    assert after["cancel_requested"] is False
    assert batch_db.retry_failed_batch_items("batch-retryable-terminal") == 1


class _RecordingExecutor:
    def __init__(self):
        self.executed = []
        self.cancelled = []

    def execute_task(self, **kwargs):
        self.executed.append(kwargs["task_id"])
        return True

    def resume_task(self, task_id):
        self.executed.append(task_id)
        return "started"

    def cancel_task(self, task_id, _timeout):
        self.cancelled.append(task_id)
        return True


class _CancelDuringCreateManager:
    def __init__(self, database, batch_id):
        self.database = database
        self.batch_id = batch_id

    def create_task(self, **kwargs):
        task_id = kwargs.get("task_id") or "cancel-race-task"
        self.database.create_task(
            task_id,
            kwargs["theme"],
            kwargs["style"],
            kwargs["length"],
            kwargs.get("name"),
            kwargs.get("ratio") or "16:9",
            kwargs.get("voice_type"),
            tts_options=kwargs.get("tts_options"),
            execution_mode=kwargs.get("execution_mode") or "review_first",
        )
        self.database.cancel_batch(self.batch_id)
        return task_id


def test_cancel_during_task_creation_never_starts_generation(batch_db):
    batch_db.create_batch("batch-cancel-race", _items("cancel-race", 2), _config(), 1)
    item = batch_db.claim_next_batch_item()
    executor = _RecordingExecutor()
    scheduler = BatchScheduler(
        database=batch_db,
        manager=_CancelDuringCreateManager(batch_db, "batch-cancel-race"),
        executor=executor,
        runtime=_Runtime(),
        poll_interval=0.02,
    )

    scheduler._run_item(item)

    assert executor.executed == []
    stored = batch_db.get_batch_item(item["item_id"])
    assert stored["status"] == "cancelled"
    assert stored["task_id"]


class _CrashAfterPersistManager:
    def __init__(self, database):
        self.database = database
        self.calls = 0

    def create_task(self, **kwargs):
        self.calls += 1
        task_id = kwargs.get("task_id") or f"orphan-task-{self.calls}"
        self.database.create_task(
            task_id,
            kwargs["theme"],
            kwargs["style"],
            kwargs["length"],
            kwargs.get("name"),
            kwargs.get("ratio") or "16:9",
            kwargs.get("voice_type"),
            execution_mode=kwargs.get("execution_mode") or "review_first",
        )
        if self.calls == 1:
            raise RuntimeError("simulated crash after task persistence")
        return task_id


def test_crash_after_task_persistence_reuses_reserved_task_without_orphan(batch_db):
    batch_db.create_batch("batch-create-crash", _items("create-crash", 2), _config(), 1)
    item = batch_db.claim_next_batch_item()
    manager = _CrashAfterPersistManager(batch_db)
    scheduler = BatchScheduler(
        database=batch_db,
        manager=manager,
        executor=_RecordingExecutor(),
        runtime=_Runtime(),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        scheduler._create_task(item)
    checkpoint = batch_db.get_batch_item(item["item_id"])
    reserved_task_id = checkpoint["task_id"]
    assert reserved_task_id

    assert scheduler._create_task(checkpoint) == reserved_task_id
    assert manager.calls == 1
    persisted = batch_db.list_tasks(limit=10)
    assert [task["task_id"] for task in persisted] == [reserved_task_id]


def test_only_one_scheduler_owner_can_hold_the_process_lock(batch_db):
    contender = SQLiteClient()
    try:
        assert batch_db.try_acquire_batch_scheduler_lock("owner-a")
        assert not contender.try_acquire_batch_scheduler_lock("owner-b")
        assert batch_db.try_acquire_batch_scheduler_lock("owner-a")
        assert batch_db.release_batch_scheduler_lock("owner-a")
        assert contender.try_acquire_batch_scheduler_lock("owner-b")
    finally:
        batch_db.release_batch_scheduler_lock("owner-a")
        contender.release_batch_scheduler_lock("owner-b")


class _FaultOnceSchedulerDatabase:
    def __init__(self):
        self.claim_calls = 0
        self.released = []

    def try_acquire_batch_scheduler_lock(self, _owner_id):
        return True

    def release_batch_scheduler_lock(self, owner_id):
        self.released.append(owner_id)
        return True

    def recover_batch_items(self):
        return 0

    def claim_next_batch_item(self, **_kwargs):
        self.claim_calls += 1
        if self.claim_calls == 1:
            raise RuntimeError("transient claim fault")
        return {}


class _FaultOnceRecoveryDatabase(_FaultOnceSchedulerDatabase):
    def __init__(self):
        super().__init__()
        self.recovery_calls = 0
        self.recovered = False

    def recover_batch_items(self):
        self.recovery_calls += 1
        if self.recovery_calls == 1:
            raise RuntimeError("transient recovery fault")
        self.recovered = True
        return 3

    def claim_next_batch_item(self, **_kwargs):
        assert self.recovered
        self.claim_calls += 1
        return {}


def test_scheduler_survives_transient_fault_and_releases_lock_when_stopped():
    database = _FaultOnceSchedulerDatabase()
    scheduler = BatchScheduler(
        database=database,
        manager=object(),
        executor=object(),
        runtime=_Runtime(),
        poll_interval=0.02,
        owner_id="fault-test-owner",
    )

    scheduler.start()
    try:
        _wait_for(lambda: database.claim_calls >= 2)
        assert scheduler.is_running
    finally:
        scheduler.stop()
        scheduler.join(1)

    assert database.released == ["fault-test-owner"]


def test_scheduler_retries_recovery_before_claiming_any_batch_item():
    database = _FaultOnceRecoveryDatabase()
    scheduler = BatchScheduler(
        database=database,
        manager=object(),
        executor=object(),
        runtime=_Runtime(),
        poll_interval=0.02,
        owner_id="recovery-test-owner",
    )

    scheduler.start()
    try:
        _wait_for(lambda: database.claim_calls >= 1)
        assert scheduler.is_running
        assert database.recovery_calls == 2
    finally:
        scheduler.stop()
        scheduler.join(1)

    assert database.released == ["recovery-test-owner"]


def test_batch_launch_guard_serializes_cancel_across_database_clients(batch_db):
    batch_db.create_batch("batch-launch-guard", _items("launch-guard", 2), _config(), 1)
    contender = SQLiteClient()
    cancel_started = threading.Event()
    cancel_finished = threading.Event()

    def cancel_from_another_client():
        cancel_started.set()
        contender.cancel_batch("batch-launch-guard")
        cancel_finished.set()

    with batch_db.batch_launch_guard():
        thread = threading.Thread(target=cancel_from_another_client)
        thread.start()
        assert cancel_started.wait(1)
        assert not cancel_finished.wait(0.1)
    thread.join(1)

    assert cancel_finished.is_set()
    assert batch_db.get_batch("batch-launch-guard")["status"] == "cancelled"


class _PersistReservedTaskManager:
    def __init__(self, database):
        self.database = database
        self.calls = []

    def create_task(self, **kwargs):
        task_id = kwargs["task_id"]
        self.calls.append(task_id)
        assert self.database.create_task(
            task_id,
            kwargs["theme"],
            kwargs["style"],
            kwargs["length"],
            kwargs.get("name"),
            kwargs.get("ratio") or "16:9",
            kwargs.get("voice_type"),
            execution_mode=kwargs.get("execution_mode") or "review_first",
        )
        return task_id


class _FinishPlanningExecutor(_RecordingExecutor):
    def __init__(self, database):
        super().__init__()
        self.database = database

    def execute_task(self, **kwargs):
        super().execute_task(**kwargs)
        self.database.update_task_workflow(
            kwargs["task_id"],
            "awaiting_confirmation",
            status="awaiting_confirmation",
            current_step="awaiting_confirmation",
        )
        return True


def test_restart_materializes_a_reserved_task_id_that_has_no_task_row(batch_db):
    batch_db.create_batch("batch-reserved-only", _items("reserved-only", 2), _config(), 1)
    item = batch_db.claim_next_batch_item()
    reserved_task_id = batch_db.reserve_batch_item_task(
        item["item_id"], "reserved-task-without-row"
    )
    assert not batch_db.get_task(reserved_task_id)
    manager = _PersistReservedTaskManager(batch_db)
    executor = _FinishPlanningExecutor(batch_db)
    scheduler = BatchScheduler(
        database=batch_db,
        manager=manager,
        executor=executor,
        runtime=_Runtime(),
    )

    scheduler._run_item(batch_db.get_batch_item(item["item_id"]))

    assert manager.calls == [reserved_task_id]
    assert executor.executed == [reserved_task_id]
    assert batch_db.get_task(reserved_task_id)
    assert batch_db.get_batch_item(item["item_id"])["status"] == "awaiting_confirmation"


def test_awaiting_finalization_is_a_successful_batch_checkpoint(batch_db):
    batch_db.create_batch("batch-finalization", _items("finalization", 2), _config(), 1)
    item = batch_db.claim_next_batch_item()
    batch_db.create_task(
        "finalization-task",
        item["theme"],
        "知识科普",
        100,
        execution_mode="review_first",
    )
    batch_db.set_batch_item_task(item["item_id"], "finalization-task")
    batch_db.update_task_workflow(
        "finalization-task",
        "awaiting_finalization",
        status="awaiting_finalization",
        current_step="awaiting_finalization",
    )
    scheduler = BatchScheduler(
        database=batch_db,
        manager=object(),
        executor=_RecordingExecutor(),
        runtime=_Runtime(),
    )

    scheduler._run_item(batch_db.get_batch_item(item["item_id"]))

    assert batch_db.get_batch_item(item["item_id"])["status"] == "awaiting_confirmation"


def test_batch_validation_errors_are_structured_and_never_echo_credentials():
    from api_server import app

    secret = "sk-" + "A" * 120

    async def request_invalid_batch():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/ai/native/video/kepu/batches",
                json={"items": [{"theme": secret}, {"theme": "合法主题"}]},
            )

    response = asyncio.run(request_invalid_batch())
    payload = response.json()

    assert response.status_code == 422
    assert payload["error_code"] == "unknown"
    assert payload["error_meta"]["safe_message"]
    assert secret not in response.text


def test_cli_returns_nonzero_contract_for_partial_batch_failure(tmp_path, monkeypatch):
    import main as cli

    topics_file = tmp_path / "topics.txt"
    topics_file.write_text("主题一\n主题二\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_api_request",
        lambda *_args, **_kwargs: {
            "batch_id": "batch-cli-failed",
            "status": "completed_with_errors",
            "total_count": 2,
            "counts": {
                "running": 0,
                "awaiting_confirmation": 1,
                "failed": 1,
                "cancelled": 0,
            },
        },
    )

    with pytest.raises(RuntimeError, match="失败"):
        cli._run_batch_cli(["--file", str(topics_file)])

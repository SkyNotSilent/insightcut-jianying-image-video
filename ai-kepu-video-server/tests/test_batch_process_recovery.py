import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx


SERVER_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(port: int, environment: dict) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=SERVER_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    health_url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"isolated server exited with {process.returncode}")
        try:
            if httpx.get(health_url, timeout=1).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    process.kill()
    process.wait(timeout=5)
    raise AssertionError("isolated server did not become healthy")


def _stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_strong_restart_reuses_persisted_task_ids_and_finishes_batch(tmp_path):
    port = _free_port()
    database_path = tmp_path / "data" / "restart.db"
    environment = {
        **os.environ,
        "INSIGHTCUT_FAKE_PROVIDERS": "1",
        "INSIGHTCUT_DATA_ROOT": str(tmp_path),
        "INSIGHTCUT_DB_PATH": str(database_path),
        "TASK_SWEEPER_INTERVAL_SECONDS": "0.1",
    }
    api_base = f"http://127.0.0.1:{port}/ai/native/video/kepu"
    first_process = _start_server(port, environment)
    second_process = None
    try:
        response = httpx.post(
            f"{api_base}/batches",
            json={
                "items": [
                    {"theme": f"强杀恢复主题 {index:02d}"}
                    for index in range(20)
                ],
                "concurrency": 3,
                "style": "知识科普|电影质感",
                "ratio": "16:9",
                "length": 80,
            },
            timeout=10,
        )
        assert response.status_code == 201
        batch_id = response.json()["batch_id"]

        checkpoint_task_id = None
        deadline = time.time() + 10
        while time.time() < deadline:
            batch = httpx.get(f"{api_base}/batches/{batch_id}", timeout=5).json()
            running = [item for item in batch["items"] if item["status"] == "running"]
            checkpoint_task_id = next(
                (item["task_id"] for item in running if item.get("task_id")),
                None,
            )
            if checkpoint_task_id and batch["counts"]["queued"]:
                break
            time.sleep(0.02)
        assert checkpoint_task_id

        first_process.kill()
        first_process.wait(timeout=5)
        second_process = _start_server(port, environment)

        deadline = time.time() + 90
        while time.time() < deadline:
            batch = httpx.get(f"{api_base}/batches/{batch_id}", timeout=5).json()
            if batch["status"] in {"completed", "completed_with_errors", "cancelled"}:
                break
            time.sleep(0.1)
        assert batch["status"] == "completed"
        assert batch["counts"]["awaiting_confirmation"] == 20
        task_ids = [item["task_id"] for item in batch["items"]]
        assert checkpoint_task_id in task_ids
        assert len(set(task_ids)) == 20
    finally:
        _stop_server(first_process)
        if second_process is not None:
            _stop_server(second_process)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 20
        assert connection.execute(
            "SELECT COUNT(DISTINCT task_id) FROM task_batch_items WHERE batch_id=?",
            (batch_id,),
        ).fetchone()[0] == 20

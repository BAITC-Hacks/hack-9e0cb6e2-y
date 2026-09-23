import time
from concurrent.futures import ThreadPoolExecutor

from autoprotocol import jobs
from autoprotocol.config import Settings
from autoprotocol.storage import initialize
from autoprotocol.worker import process_job


def seed(path, meeting_id="meeting"):
    jobs.enqueue(
        path,
        {
            "id": meeting_id,
            "request_key": meeting_id,
            "title": "Test",
            "occurred_on": None,
            "timezone": "Asia/Almaty",
            "now": time.time(),
            "duration_seconds": 1,
            "source_path": "unused",
            "audio_sha256": "synthetic",
        },
    )


def test_concurrent_workers_claim_only_one_job(tmp_path):
    path = tmp_path / "queue.sqlite3"
    initialize(path)
    seed(path, "one")
    seed(path, "two")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: jobs.claim(path), range(2)))
    assert sum(item is not None for item in results) == 1


def test_expired_lease_is_retried_and_old_worker_cannot_publish(tmp_path):
    path = tmp_path / "queue.sqlite3"
    initialize(path)
    seed(path)
    now = time.time()
    first = jobs.claim(path, now=now)
    second = jobs.claim(path, now=now + 61)
    assert second["attempt"] == 2
    assert second["owner"] != first["owner"]
    assert not jobs.update(path, first, "transcript_ready", status="ready")
    assert not jobs.heartbeat(path, first, now=now + 62)
    assert jobs.claim(path, now=now + 122) is None
    assert jobs.get(path, "meeting")["status"] == "failed"


def test_heartbeat_prevents_duplicate_recovery(tmp_path):
    path = tmp_path / "queue.sqlite3"
    initialize(path)
    seed(path)
    now = time.time()
    job = jobs.claim(path, now=now)
    assert jobs.heartbeat(path, job, now=now + 50)
    assert jobs.claim(path, now=now + 70) is None


def test_missing_models_fail_honestly_and_survive_restart(tmp_path):
    config = Settings(data_dir=tmp_path, models_dir=tmp_path / "missing", _env_file=None)
    initialize(config.database_path)
    seed(config.database_path)
    job = jobs.claim(config.database_path)
    process_job(config, job)
    initialize(config.database_path)
    row = jobs.get(config.database_path, "meeting")
    assert row["status"] == "failed"
    assert "модели не найдены" in row["error"]
    assert row["result_path"] is None


def test_completed_job_does_not_run_again_after_restart(tmp_path):
    path = tmp_path / "queue.sqlite3"
    initialize(path)
    seed(path)
    job = jobs.claim(path)
    assert jobs.update(
        path, job, "transcript_ready", status="ready", result="result.json", audio="audio.wav"
    )
    initialize(path)
    assert jobs.claim(path) is None
    assert jobs.get(path, "meeting")["result_path"] == "result.json"

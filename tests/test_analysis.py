import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoprotocol import analysis, jobs, review
from autoprotocol.config import Settings
from autoprotocol.main import create_app
from autoprotocol.storage import connect, initialize


@pytest.fixture
def ready(tmp_path):
    config = Settings(data_dir=tmp_path, models_dir=tmp_path / "models", _env_file=None)
    initialize(config.database_path)
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/extraction_ru.json").read_text(encoding="utf-8")
    )
    for segment in fixture["segments"]:
        segment.update(review_flags=[], words=[])
    path = tmp_path / "result.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    jobs.enqueue(
        config.database_path,
        {
            "id": "meeting",
            "request_key": "key",
            "title": "Test",
            "occurred_on": fixture["occurred_on"],
            "timezone": fixture["timezone"],
            "now": time.time(),
            "duration_seconds": 7,
            "source_path": "unused",
            "audio_sha256": "fake",
        },
    )
    with connect(config.database_path) as db:
        db.execute("UPDATE meetings SET status='ready',result_path=?", (str(path),))
    current = review.get_result(config.database_path, "meeting")
    payload = {"expected_revision": 0, "source_digest": current["review"]["source_digest"]}
    return config, payload


def queue(config, payload):
    return analysis.enqueue(config.database_path, "meeting", analysis.AnalysisRequest(**payload))


def test_api_missing_model_origin_and_duplicate_requests(ready):
    config, payload = ready
    url = "/api/meetings/meeting/analysis"
    with TestClient(create_app(config), base_url="http://localhost") as client:
        assert client.get(url).json()["status"] == "not_started"
        assert client.post(url, json=payload).status_code == 503
        for path in (config.llm_model, config.llm_server):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test-only")
        assert (
            client.post(
                url, json=payload, headers={"Origin": "https://elsewhere.example"}
            ).status_code
            == 403
        )
        first = client.post(url, json=payload)
        assert first.status_code == 202
        assert client.post(url, json=payload).json()["id"] == first.json()["id"]
        assert client.get(url).json()["status"] == "queued"
        assert client.post(url, json={**payload, "expected_revision": 1}).status_code == 409


def test_snapshot_survives_edits_and_output_is_marked_stale(ready):
    config, payload = ready
    queue(config, payload)
    job = analysis.claim(config.database_path)
    snapshot = json.loads(job["input_json"])
    assert snapshot["occurred_on"] == "2026-09-23"
    review.save(
        config.database_path,
        "meeting",
        review.ReviewPatch(
            **payload,
            segments=[
                {
                    "id": "seg_1",
                    "text": "Изменённая реплика",
                    "speaker_id": "SPEAKER_00",
                    "reviewed": True,
                }
            ],
        ),
    )
    assert "Изменённая" not in job["input_json"]
    assert analysis.finish(config.database_path, job, result={"actions": [], "summary": []})
    initialize(config.database_path)
    result = analysis.latest(config.database_path, "meeting")
    assert result["stale"] is True
    assert result["revision"] == 0
    assert result["status"] == "ready"


def test_concurrent_claim_and_stale_owner_fencing(ready):
    config, payload = ready
    queue(config, payload)
    now = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: analysis.claim(config.database_path, now), range(2)))
    first = next(job for job in claimed if job)
    assert sum(job is not None for job in claimed) == 1
    second = analysis.claim(config.database_path, now + 61)
    assert second["attempt"] == 2
    assert not analysis.heartbeat(config.database_path, first, now + 62)
    assert not analysis.finish(config.database_path, first, result={})
    assert analysis.claim(config.database_path, now + 122) is None
    assert analysis.latest(config.database_path, "meeting")["status"] == "failed"
    assert queue(config, payload) == first["id"]  # Explicit retry after failure.
    assert analysis.latest(config.database_path, "meeting")["status"] == "queued"


def test_speech_and_analysis_cannot_run_together(ready):
    config, payload = ready
    queue(config, payload)
    with connect(config.database_path) as db:
        db.execute("UPDATE meetings SET status='queued'")
    speech = jobs.claim(config.database_path)
    assert analysis.claim(config.database_path) is None
    assert jobs.update(config.database_path, speech, "done", status="ready", result="unused")
    llm = analysis.claim(config.database_path)
    with connect(config.database_path) as db:
        db.execute("UPDATE meetings SET status='queued'")
    assert jobs.claim(config.database_path) is None
    assert analysis.heartbeat(config.database_path, llm)
    assert analysis.finish(config.database_path, llm, result={})
    assert jobs.claim(config.database_path) is not None


def test_restored_source_selects_its_own_analysis(ready):
    config, payload = ready
    path = config.data_dir / "result.json"
    original = path.read_bytes()
    first_id = queue(config, payload)
    first = analysis.claim(config.database_path)
    assert analysis.finish(config.database_path, first, result={"actions": [], "summary": []})
    changed = json.loads(original)
    changed["segments"][0]["text"] = "Другой результат распознавания"
    path.write_text(json.dumps(changed), encoding="utf-8")
    current = review.get_result(config.database_path, "meeting")
    queue(config, {"expected_revision": 0, "source_digest": current["review"]["source_digest"]})
    second = analysis.claim(config.database_path)
    assert analysis.finish(config.database_path, second, result={"actions": [], "summary": []})
    path.write_bytes(original)
    restored = analysis.latest(config.database_path, "meeting")
    assert restored["id"] == first_id
    assert not restored["stale"]

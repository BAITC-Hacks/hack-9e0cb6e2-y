import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from autoprotocol import jobs, review
from autoprotocol.config import Settings
from autoprotocol.main import create_app
from autoprotocol.storage import connect, initialize


@pytest.fixture
def meeting(tmp_path):
    config = Settings(data_dir=tmp_path, _env_file=None)
    initialize(config.database_path)
    original = {
        "segments": [
            {
                "id": "aligned_0001",
                "text": "Пусть Ерлан подготовит отчёт.",
                "speaker_id": "SPEAKER_00",
                "start_ms": 0,
                "end_ms": 1000,
                "review_flags": [],
                "words": [],
            },
            {
                "id": "aligned_0002",
                "text": "Сделаю завтра.",
                "speaker_id": None,
                "start_ms": 1000,
                "end_ms": 2000,
                "review_flags": ["overlapping_speech"],
                "words": [
                    {
                        "text": "Сделаю",
                        "speaker_id": None,
                        "candidates": [{"speaker_id": "SPEAKER_01", "overlap_ms": 100}],
                    }
                ],
            },
        ],
        "statistics": {"segments_requiring_review": 1},
        "source_segments": [{"id": "source_1", "text": "Исходное распознавание"}],
        "speaker_names_confirmed": False,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    for meeting_id in ("meeting-a", "meeting-b"):
        jobs.enqueue(
            config.database_path,
            {
                "id": meeting_id,
                "request_key": meeting_id,
                "title": "Синтетическая встреча",
                "occurred_on": None,
                "timezone": "Asia/Almaty",
                "now": time.time(),
                "duration_seconds": 2,
                "source_path": "synthetic.wav",
                "audio_sha256": "test",
            },
        )
    with connect(config.database_path) as db:
        db.execute(
            "UPDATE meetings SET status='ready',result_path=? WHERE id='meeting-a'", (str(path),)
        )
    return config, path, original


def patch_for(result, **updates):
    return {
        "source_digest": result["review"]["source_digest"],
        "expected_revision": result["review"]["revision"],
        **updates,
    }


def test_edit_survives_restart_and_preserves_source_and_history(meeting):
    config, path, original = meeting
    raw = path.read_bytes()
    url = "/api/meetings/meeting-a/result"
    with TestClient(create_app(config), base_url="http://localhost") as client:
        current = client.get(url).json()
        assert current["review"]["revision"] == 0
        patch = patch_for(
            current,
            participants=[
                {"speaker_id": "SPEAKER_01", "display_name": " Ерлан ", "confirmed": True}
            ],
            segments=[
                {
                    "id": "aligned_0002",
                    "text": "Сделаю в пятницу.",
                    "speaker_id": "SPEAKER_01",
                    "reviewed": True,
                }
            ],
        )
        saved = client.patch(url, json=patch)
        assert saved.status_code == 200
        assert saved.json()["review"]["revision"] == 1
        assert saved.json()["review"]["unreviewed_flagged_segments"] == 0
        assert saved.json()["segments"][1]["review_flags"] == ["overlapping_speech"]
        assert saved.json()["participants"][1]["display_name"] == "Ерлан"
        assert client.get(url + "?original=true").json() == original
    with TestClient(create_app(config), base_url="http://localhost") as client:
        result = client.get(url).json()
        edited = result["segments"][1]
        assert edited["text"] == "Сделаю в пятницу."
        assert edited["original"]["text"] == "Сделаю завтра."
        assert edited["original"]["speaker_id"] is None
        assert edited["words"] == original["segments"][1]["words"]
        assert edited["manual_fields"] == ["text", "speaker_id"]
        history = client.get("/api/meetings/meeting-a/revisions").json()
        assert len(history) == 1
        assert history[0]["change_source"] == "manual"
        text_change = next(c for c in history[0]["changes"] if c["field"] == "text")
        assert text_change["old"] == "Сделаю завтра."
        assert text_change["new"] == "Сделаю в пятницу."
        # Submitting the identical state does not create spurious revisions.
        patch["expected_revision"] = 1
        assert client.patch(url, json=patch).json()["review"]["revision"] == 1
        assert client.get("/api/meetings/meeting-b/revisions").json() == []
    assert path.read_bytes() == raw


def test_concurrent_saves_only_one_wins(meeting):
    config, _, _ = meeting
    current = review.get_result(config.database_path, "meeting-a")

    def save_name(name):
        patch = review.ReviewPatch.model_validate(
            patch_for(
                current,
                participants=[
                    {"speaker_id": "SPEAKER_00", "display_name": name, "confirmed": True}
                ],
            )
        )
        try:
            review.save(config.database_path, "meeting-a", patch)
            return 200
        except review.ReviewError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save_name, ["Алия", "Бекзат"])) == [200, 409]
    assert len(review.history(config.database_path, "meeting-a")) == 1


def test_changed_model_output_does_not_receive_old_edits(meeting):
    config, path, original = meeting
    current = review.get_result(config.database_path, "meeting-a")
    patch = review.ReviewPatch.model_validate(
        patch_for(
            current,
            participants=[{"speaker_id": "SPEAKER_00", "display_name": "Алия", "confirmed": True}],
        )
    )
    review.save(config.database_path, "meeting-a", patch)
    original["segments"][0]["text"] = "Повторное распознавание"
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    changed = review.get_result(config.database_path, "meeting-a")
    assert changed["review"]["revision"] == 0
    assert changed["review"]["previous_source_reviews"] is True
    assert changed["participants"][0]["display_name"] is None
    with pytest.raises(review.ReviewError) as error:
        review.save(config.database_path, "meeting-a", patch)
    assert error.value.status == 409
    assert len(review.history(config.database_path, "meeting-a")) == 1
    with connect(config.database_path) as db:
        snapshot = db.execute("SELECT source_json FROM review_states").fetchone()[0]
        assert json.loads(snapshot)["segments"][0]["text"] == "Пусть Ерлан подготовит отчёт."


@pytest.mark.parametrize(
    "updates",
    [
        {"participants": [{"speaker_id": "missing", "display_name": "Алия", "confirmed": True}]},
        {
            "participants": [
                {"speaker_id": "SPEAKER_00", "display_name": "Алия", "confirmed": False}
            ]
        },
        {"segments": [{"id": "absent", "text": "Текст", "speaker_id": None, "reviewed": True}]},
        {"segments": [{"id": "aligned_0001", "text": " ", "speaker_id": None, "reviewed": True}]},
        {
            "segments": [
                {"id": "aligned_0001", "text": "Текст", "speaker_id": "foreign", "reviewed": True}
            ]
        },
        {
            "segments": [
                {
                    "id": "aligned_0001",
                    "text": "Текст",
                    "speaker_id": None,
                    "reviewed": True,
                    "start_ms": 9,
                }
            ]
        },
        {"expected_revision": True},
        {
            "participants": [
                {"speaker_id": "SPEAKER_00", "display_name": "Алия", "confirmed": True},
                {"speaker_id": "SPEAKER_00", "display_name": "Бекзат", "confirmed": True},
            ]
        },
    ],
)
def test_invalid_edits_leave_no_partial_changes(meeting, updates):
    config, _, _ = meeting
    with TestClient(create_app(config), base_url="http://localhost") as client:
        url = "/api/meetings/meeting-a/result"
        before = client.get(url).json()
        patch = patch_for(
            before,
            participants=[{"speaker_id": "SPEAKER_00", "display_name": "Алия", "confirmed": True}],
        )
        patch.update(updates)
        assert client.patch(url, json=patch).status_code == 422
        assert client.get(url).json() == before
        assert client.get("/api/meetings/meeting-a/revisions").json() == []


def test_origin_not_ready_and_missing_meetings(meeting):
    config, _, _ = meeting
    with TestClient(create_app(config), base_url="http://localhost") as client:
        url = "/api/meetings/meeting-a/result"
        patch = patch_for(client.get(url).json())
        assert (
            client.patch(url, json=patch, headers={"Origin": "https://example.com"}).status_code
            == 403
        )
        assert (
            client.patch(url, json=patch, headers={"Origin": "http://localhost"}).status_code == 200
        )
        assert client.patch("/api/meetings/meeting-b/result", json=patch).status_code == 409
        assert client.patch("/api/meetings/absent/result", json=patch).status_code == 404
        assert client.get("/api/meetings/absent/revisions").status_code == 404


def test_clear_name_and_restore_original_are_audited(meeting):
    config, _, _ = meeting
    current = review.get_result(config.database_path, "meeting-a")
    saved = review.save(
        config.database_path,
        "meeting-a",
        review.ReviewPatch.model_validate(
            patch_for(
                current,
                participants=[
                    {"speaker_id": "SPEAKER_00", "display_name": "Алия", "confirmed": True}
                ],
                segments=[
                    {
                        "id": "aligned_0001",
                        "text": "Другой текст",
                        "speaker_id": None,
                        "reviewed": True,
                    }
                ],
            )
        ),
    )
    restored = review.save(
        config.database_path,
        "meeting-a",
        review.ReviewPatch.model_validate(
            patch_for(
                saved,
                participants=[
                    {"speaker_id": "SPEAKER_00", "display_name": None, "confirmed": False}
                ],
                segments=[
                    {
                        "id": "aligned_0001",
                        "text": current["segments"][0]["text"],
                        "speaker_id": "SPEAKER_00",
                        "reviewed": False,
                    }
                ],
            )
        ),
    )
    assert restored["review"]["revision"] == 2
    assert restored["participants"][0]["mapping_source"] is None
    assert restored["segments"][0]["manual_fields"] == []
    assert len(review.history(config.database_path, "meeting-a")) == 2


def test_v2_upgrade_keeps_ready_meetings_and_isolates_identical_recordings(meeting):
    config, path, original = meeting
    with connect(config.database_path) as db:
        # A legacy database has meetings/jobs but no review tables.
        for table in (
            "analysis_revisions",
            "analysis_reviews",
            "analyses",
            "revisions",
            "segment_edits",
            "participants",
            "review_states",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM schema_version WHERE version>=3")
        db.execute(
            "UPDATE meetings SET status='ready',result_path=? WHERE id='meeting-b'", (str(path),)
        )
    initialize(config.database_path)
    initialize(config.database_path)
    current = review.get_result(config.database_path, "meeting-a")
    review.save(
        config.database_path,
        "meeting-a",
        review.ReviewPatch.model_validate(
            patch_for(
                current,
                participants=[
                    {"speaker_id": "SPEAKER_00", "display_name": "Алия", "confirmed": True}
                ],
            )
        ),
    )
    other = review.get_result(config.database_path, "meeting-b")
    assert other["review"]["revision"] == 0
    assert other["participants"][0]["display_name"] is None
    assert other["segments"][0]["text"] == original["segments"][0]["text"]
    with connect(config.database_path) as db:
        assert [
            r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")
        ] == [1, 2, 3, 4, 5, 6]
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2

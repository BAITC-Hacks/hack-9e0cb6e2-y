import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_analysis import queue
from test_analysis import ready as prepared_meeting  # noqa: F401
from test_extract import output

from autoprotocol import analysis, analysis_review, review
from autoprotocol.main import create_app
from autoprotocol.pipeline.extract import validate_output
from autoprotocol.storage import connect, initialize


@pytest.fixture
def extracted(prepared_meeting):  # noqa: F811
    config, request = prepared_meeting
    identifier = queue(config, request)
    job = analysis.claim(config.database_path)
    raw = output()
    raw["summary"] = [
        {
            "text": "Подготовить отчёт",
            "kind": "action",
            "evidence": copy.deepcopy(raw["actions"][0]["evidence"]),
        }
    ]
    result = validate_output(json.dumps(raw), json.loads(job["input_json"]))
    assert analysis.finish(config.database_path, job, result=result)
    return config, identifier, result, request


def patch_for(config):
    current = analysis.latest(config.database_path, "meeting")["result"]
    return {
        "expected_revision": current["review"]["revision"],
        "actions": [
            {key: item[key] for key in analysis_review.ActionEdit.model_fields}
            for item in current["actions"]
        ],
        "summary": [
            {key: item[key] for key in analysis_review.SummaryEdit.model_fields}
            for item in current["summary"]
        ],
    }


def save(config, identifier, payload):
    return analysis_review.save(
        config.database_path,
        "meeting",
        identifier,
        analysis_review.AnalysisPatch.model_validate(payload),
    )


def test_edits_restart_original_evidence_and_history(extracted):
    config, identifier, original, _ = extracted
    payload = patch_for(config)
    payload["actions"][0].update(
        task="Исправленный отчёт", assignee_text="Әлия", due_text="2026-10-15", reviewed=True
    )
    payload["summary"][0].update(text="Қорытынды: отчёт", excluded=True, reviewed=True)
    url = f"/api/meetings/meeting/analysis/{identifier}"
    with TestClient(create_app(config), base_url="http://localhost") as client:
        response = client.patch(url + "/review", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["review"]["revision"] == 1
        assert result["actions"][0]["id"] == original["actions"][0]["id"]
        assert result["actions"][0]["due_normalized"]["date"] == "2026-10-15"
        assert result["actions"][0]["requires_review"] is False
        assert result["actions"][0]["original"]["assignee_text"] == "Ерлан"
        assert result["actions"][0]["evidence"] == original["actions"][0]["evidence"]
        assert result["summary"][0]["excluded"] is True
        assert (
            client.get("/api/meetings/meeting/analysis?original=true").json()["result"] == original
        )
    initialize(config.database_path)
    with TestClient(create_app(config), base_url="http://localhost") as client:
        assert client.get("/api/meetings/meeting/analysis").json()["result"] == result
        revisions = client.get(url + "/revisions").json()
        assert len(revisions) == 1
        assert {
            "id": original["actions"][0]["id"],
            "field": "assignee_text",
            "old": "Ерлан",
            "new": "Әлия",
        } in revisions[0]["changes"]
        # No-op save does not create history; restore remains an audited edit.
        assert (
            client.patch(url + "/review", json=patch_for(config)).json()["review"]["revision"] == 1
        )
        restore = patch_for(config)
        restore["summary"][0]["excluded"] = False
        assert client.patch(url + "/review", json=restore).json()["review"]["revision"] == 2


def test_concurrent_edit_cannot_overwrite_and_transactions_are_atomic(extracted):
    config, identifier, _, _ = extracted
    payload = patch_for(config)
    payload["actions"][0]["task"] = "Первая правка"

    def attempt(_):
        try:
            save(config, identifier, payload)
            return 200
        except review.ReviewError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 409]
    invalid = patch_for(config)
    invalid["actions"][0]["task"] = "Не должно сохраниться"
    invalid["summary"][0]["id"] = "foreign"
    with pytest.raises(review.ReviewError, match="не принадлежит"):
        save(config, identifier, invalid)
    result = analysis.latest(config.database_path, "meeting")["result"]
    assert result["actions"][0]["task"] == "Первая правка"
    assert result["review"]["revision"] == 1


def test_transcript_change_blocks_old_edits_and_new_analysis_preserves_history(extracted):
    config, identifier, original, request = extracted
    payload = patch_for(config)
    payload["actions"][0]["assignee_text"] = "Алия"
    save(config, identifier, payload)
    current = review.get_result(config.database_path, "meeting")
    segment = current["segments"][0]
    updated = review.save(
        config.database_path,
        "meeting",
        review.ReviewPatch(
            **request,
            segments=[
                {
                    "id": segment["id"],
                    "text": segment["text"],
                    "speaker_id": segment["speaker_id"],
                    "reviewed": True,
                }
            ],
        ),
    )
    with pytest.raises(review.ReviewError, match="Транскрипт изменился"):
        save(config, identifier, patch_for(config))
    assert analysis.latest(config.database_path, "meeting")["stale"]
    new_id = queue(
        config,
        {
            "expected_revision": updated["review"]["revision"],
            "source_digest": updated["review"]["source_digest"],
        },
    )
    job = analysis.claim(config.database_path)
    analysis.finish(config.database_path, job, result=original)
    latest = analysis.latest(config.database_path, "meeting")
    assert latest["id"] == new_id != identifier
    assert latest["result"]["review"]["revision"] == 0
    assert latest["result"]["actions"][0]["assignee_text"] == "Ерлан"
    assert len(analysis_review.history(config.database_path, "meeting", identifier)) == 1
    with connect(config.database_path) as db:
        assert (
            json.loads(
                db.execute(
                    "SELECT edits_json FROM analysis_reviews WHERE analysis_id=?", (identifier,)
                ).fetchone()[0]
            )["action_0001"]["assignee_text"]
            == "Алия"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"evidence": []},
        {"task": "   "},
        {"reviewed": "true"},
        {"due_text": None, "due_kind": "date"},
        {"due_text": "завтра", "due_kind": "unspecified"},
        {"due_kind": "bogus"},
        {"status": "complete"},
        {"task": "x" * 1001},
    ],
)
def test_invalid_patch_rejected_without_writes(extracted, change):
    config, identifier, _, _ = extracted
    payload = patch_for(config)
    payload["actions"][0].update(change)
    with TestClient(create_app(config), base_url="http://localhost") as client:
        assert (
            client.patch(
                f"/api/meetings/meeting/analysis/{identifier}/review", json=payload
            ).status_code
            == 422
        )
    assert analysis_review.history(config.database_path, "meeting", identifier) == []


def test_origin_duplicate_ids_wrong_meeting_and_not_ready(extracted):
    config, identifier, _, _ = extracted
    payload = patch_for(config)
    url = f"/api/meetings/meeting/analysis/{identifier}/review"
    with TestClient(create_app(config), base_url="http://localhost") as client:
        assert (
            client.patch(
                url, json=payload, headers={"Origin": "https://elsewhere.example"}
            ).status_code
            == 403
        )
        assert client.patch(url.replace("/meeting/", "/other/"), json=payload).status_code == 404
        assert (
            client.get(
                url.replace("/meeting/", "/other/").replace("/review", "/revisions")
            ).status_code
            == 404
        )
        duplicate = copy.deepcopy(payload)
        duplicate["actions"] *= 2
        assert client.patch(url, json=duplicate).status_code == 422
        with connect(config.database_path) as db:
            db.execute("UPDATE analyses SET status='processing' WHERE id=?", (identifier,))
        assert client.patch(url, json=payload).status_code == 409


def test_clear_deadline_clears_computed_date_and_restores_unknown_assignee(extracted):
    config, identifier, _, _ = extracted
    payload = patch_for(config)
    payload["actions"][0].update(due_text=None, due_kind="unspecified", assignee_text="  ")
    item = save(config, identifier, payload)["actions"][0]
    assert item["due_text"] is None
    assert not any(item["due_normalized"].values())
    assert item["assignee_text"] is None
    assert "unknown_assignee" in item["review_flags"]

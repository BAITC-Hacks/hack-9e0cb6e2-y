"""Durable extraction requests bound to a saved transcript revision."""

import json
import time
import uuid

from pydantic import Field

from autoprotocol import analysis_review, review
from autoprotocol.jobs import LEASE_SECONDS, MAX_ATTEMPTS
from autoprotocol.pipeline.extract import PROMPT_VERSION
from autoprotocol.storage import connect


class AnalysisRequest(review.StrictModel):
    expected_revision: int = Field(ge=0)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def enqueue(database, meeting_id, request):
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        original, digest = review.source(db, meeting_id)
        current = review.view(db, meeting_id, original, digest)
        revision = current["review"]["revision"]
        if revision != request.expected_revision or digest != request.source_digest:
            raise review.ReviewError(409, "Транскрипт изменился. Загрузите сохранённую версию.")
        meeting = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        snapshot = {
            "title": meeting["title"],
            "occurred_on": meeting["occurred_on"],
            "timezone": meeting["timezone"],
            "participants": current["participants"],
            "segments": [
                {k: s[k] for k in ("id", "text", "speaker_id", "start_ms", "end_ms")}
                for s in current["segments"]
            ],
        }
        payload = json.dumps(snapshot, ensure_ascii=False)
        if len(payload) > 40000:
            raise review.ReviewError(422, "Транскрипт слишком большой для текущего профиля LLM.")
        existing = db.execute(
            "SELECT * FROM analyses WHERE meeting_id=? AND source_digest=? AND revision=? "
            "AND prompt_version=?",
            (meeting_id, digest, revision, PROMPT_VERSION),
        ).fetchone()
        if existing:
            if existing["status"] == "failed":
                db.execute(
                    """UPDATE analyses SET status='queued',attempt=0,owner=NULL,
                    lease_until=NULL,error=NULL,result_json=NULL,created_at=? WHERE id=?""",
                    (time.time(), existing["id"]),
                )
            return existing["id"]
        identifier = uuid.uuid4().hex
        db.execute(
            """INSERT INTO analyses
            (id,meeting_id,source_digest,revision,input_json,status,created_at,prompt_version)
            VALUES (?,?,?,?,?,'queued',?,?)""",
            (identifier, meeting_id, digest, revision, payload, time.time(), PROMPT_VERSION),
        )
        return identifier


def latest(database, meeting_id, *, original=False):
    with connect(database) as db:
        db.execute("BEGIN")
        transcript, digest = review.source(db, meeting_id)
        current = review.view(db, meeting_id, transcript, digest)
        row = db.execute(
            "SELECT * FROM analyses WHERE meeting_id=? "
            "ORDER BY (source_digest=? AND revision=?) DESC,created_at DESC LIMIT 1",
            (meeting_id, digest, current["review"]["revision"]),
        ).fetchone()
        if row is None:
            return {"status": "not_started", "stale": False, "result": None}
        return {
            "id": row["id"],
            "status": row["status"],
            "error": row["error"],
            "revision": row["revision"],
            "source_digest": row["source_digest"],
            "upgrade_available": row["prompt_version"] != PROMPT_VERSION,
            "stale": row["source_digest"] != digest
            or row["revision"] != current["review"]["revision"],
            "result": (
                (json.loads(row["result_json"]) if original else analysis_review.view(db, row))
                if row["result_json"]
                else None
            ),
        }


def claim(database, now=None):
    now = time.time() if now is None else now
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute(
            "SELECT * FROM analyses WHERE status='processing' AND lease_until<=?", (now,)
        ).fetchall():
            failed = row["attempt"] >= MAX_ATTEMPTS
            db.execute(
                "UPDATE analyses SET status=?,owner=NULL,lease_until=NULL,error=? WHERE id=?",
                (
                    "failed" if failed else "queued",
                    "Обработчик анализа прервался; попытки исчерпаны." if failed else None,
                    row["id"],
                ),
            )
        if db.execute("SELECT 1 FROM meetings WHERE status='processing'").fetchone():
            return None
        if db.execute("SELECT 1 FROM analyses WHERE status='processing'").fetchone():
            return None
        row = db.execute(
            "SELECT * FROM analyses WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        owner = uuid.uuid4().hex
        db.execute(
            "UPDATE analyses SET status='processing',owner=?,lease_until=?,attempt=attempt+1 "
            "WHERE id=?",
            (owner, now + LEASE_SECONDS, row["id"]),
        )
        return {**dict(row), "owner": owner, "attempt": row["attempt"] + 1}


def heartbeat(database, job, now=None):
    now = time.time() if now is None else now
    with connect(database) as db:
        return (
            db.execute(
                """UPDATE analyses SET lease_until=? WHERE id=? AND owner=?
            AND status='processing' AND lease_until>?""",
                (now + LEASE_SECONDS, job["id"], job["owner"], now),
            ).rowcount
            == 1
        )


def finish(database, job, *, result=None, error=None):
    with connect(database) as db:
        return (
            db.execute(
                """UPDATE analyses SET status=?,result_json=?,error=?,lease_until=NULL
            WHERE id=? AND owner=? AND status='processing' AND lease_until>?""",
                (
                    "failed" if error else "ready",
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    job["id"],
                    job["owner"],
                    time.time(),
                ),
            ).rowcount
            == 1
        )

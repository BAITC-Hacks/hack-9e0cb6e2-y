"""Durable single-active-job queue with fenced leases and bounded crash recovery."""

import time
import uuid

from autoprotocol.storage import connect

LEASE_SECONDS = 60
MAX_ATTEMPTS = 2


def public(row):
    keys = (
        "id",
        "title",
        "occurred_on",
        "timezone",
        "created_at",
        "duration_seconds",
        "status",
        "stage",
        "error",
    )
    return {key: row[key] for key in keys}


def enqueue(database, meeting):
    with connect(database) as db:
        db.execute(
            """INSERT INTO meetings
            (id,request_key,title,occurred_on,timezone,consent_at,created_at,
             duration_seconds,source_path,audio_sha256,status,stage)
            VALUES (:id,:request_key,:title,:occurred_on,:timezone,:now,:now,
             :duration_seconds,:source_path,:audio_sha256,'queued','queued')""",
            meeting,
        )
        db.execute("INSERT INTO jobs(meeting_id) VALUES (?)", (meeting["id"],))


def get(database, meeting_id):
    with connect(database) as db:
        row = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return dict(row) if row else None


def by_key(database, key):
    with connect(database) as db:
        row = db.execute("SELECT * FROM meetings WHERE request_key=?", (key,)).fetchone()
        return dict(row) if row else None


def recent(database):
    with connect(database) as db:
        return [
            public(row)
            for row in db.execute("SELECT * FROM meetings ORDER BY created_at DESC LIMIT 100")
        ]


def claim(database, now=None):
    now = time.time() if now is None else now
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        stale = db.execute(
            """SELECT m.id,j.attempt FROM meetings m JOIN jobs j
            ON m.id=j.meeting_id WHERE m.status='processing' AND j.lease_until <= ?""",
            (now,),
        ).fetchall()
        for row in stale:
            status = "failed" if row["attempt"] >= MAX_ATTEMPTS else "queued"
            error = (
                "Обработчик прервался. Повторные попытки исчерпаны." if status == "failed" else None
            )
            db.execute(
                "UPDATE meetings SET status=?,stage=?,error=? WHERE id=?",
                (status, status, error, row["id"]),
            )
            db.execute(
                "UPDATE jobs SET owner=NULL,lease_until=NULL WHERE meeting_id=?", (row["id"],)
            )
        # Serialize GPU/RAM-heavy jobs even when two workers are accidentally started.
        if db.execute("SELECT 1 FROM meetings WHERE status='processing'").fetchone():
            return None
        row = db.execute("""SELECT m.*,j.attempt FROM meetings m JOIN jobs j ON m.id=j.meeting_id
            WHERE m.status='queued' ORDER BY m.created_at LIMIT 1""").fetchone()
        if row is None:
            return None
        owner = uuid.uuid4().hex
        db.execute(
            """UPDATE jobs SET attempt=attempt+1,owner=?,lease_until=?,heartbeat=?
            WHERE meeting_id=?""",
            (owner, now + LEASE_SECONDS, now, row["id"]),
        )
        db.execute(
            "UPDATE meetings SET status='processing',stage='preparing',error=NULL WHERE id=?",
            (row["id"],),
        )
        return {**dict(row), "owner": owner, "attempt": row["attempt"] + 1}


def heartbeat(database, job, now=None):
    now = time.time() if now is None else now
    with connect(database) as db:
        cursor = db.execute(
            """UPDATE jobs SET heartbeat=?,lease_until=?
            WHERE meeting_id=? AND owner=? AND lease_until>?""",
            (now, now + LEASE_SECONDS, job["id"], job["owner"], now),
        )
        return cursor.rowcount == 1


def update(database, job, stage, *, status="processing", error=None, result=None, audio=None):
    with connect(database) as db:
        cursor = db.execute(
            """UPDATE meetings SET stage=?,status=?,error=?,result_path=?,audio_path=?
            WHERE id=? AND status='processing' AND EXISTS (
              SELECT 1 FROM jobs WHERE meeting_id=? AND owner=? AND lease_until>?)""",
            (stage, status, error, result, audio, job["id"], job["id"], job["owner"], time.time()),
        )
        return cursor.rowcount == 1

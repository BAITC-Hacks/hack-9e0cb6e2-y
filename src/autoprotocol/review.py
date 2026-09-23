"""Manual review overlays, bound to immutable model output and an optimistic revision."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from autoprotocol.storage import connect


class ReviewError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ParticipantEdit(StrictModel):
    speaker_id: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    confirmed: bool

    @model_validator(mode="after")
    def confirmed_name(self):
        if self.display_name is not None:
            self.display_name = self.display_name.strip() or None
        if bool(self.display_name) != self.confirmed:
            raise ValueError("Имя должно быть подтверждено вручную; пустое имя не подтверждается.")
        return self


class SegmentEdit(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20000)
    speaker_id: str | None = Field(max_length=100)
    reviewed: bool

    @model_validator(mode="after")
    def nonempty_text(self):
        if not self.text.strip():
            raise ValueError("Текст фрагмента не может состоять из пробелов.")
        return self


class ReviewPatch(StrictModel):
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    participants: list[ParticipantEdit] = Field(default_factory=list, max_length=256)
    segments: list[SegmentEdit] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def unique_targets(self):
        for values in ([p.speaker_id for p in self.participants], [s.id for s in self.segments]):
            if len(values) != len(set(values)):
                raise ValueError("Один объект нельзя изменить дважды в одном запросе.")
        return self


def source(db, meeting_id):
    row = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if row is None:
        raise ReviewError(404, "Встреча не найдена.")
    if row["status"] != "ready":
        raise ReviewError(409, "Результат ещё не готов.")
    try:
        raw = Path(row["result_path"]).read_bytes()
        result = json.loads(raw)
    except (OSError, ValueError, TypeError):
        raise ReviewError(503, "Файл результата недоступен. Проверьте локальные данные.") from None
    return result, hashlib.sha256(raw).hexdigest()


def view(db, meeting_id, original, digest):
    # json copy keeps all source words/flags intact, including when the text is corrected.
    result = json.loads(json.dumps(original, ensure_ascii=False))
    key = (meeting_id, digest)
    state = db.execute(
        "SELECT revision FROM review_states WHERE meeting_id=? AND source_digest=?", key
    ).fetchone()
    speakers = {s["speaker_id"] for s in original["segments"] if s["speaker_id"] is not None}
    speakers.update(
        candidate["speaker_id"]
        for s in original["segments"]
        for w in s.get("words", [])
        for candidate in w.get("candidates", [])
    )
    names = {
        row["speaker_id"]: row["display_name"]
        for row in db.execute(
            "SELECT * FROM participants WHERE meeting_id=? AND source_digest=?", key
        )
    }
    result["participants"] = [
        {
            "speaker_id": speaker,
            "display_name": names.get(speaker),
            "confirmed": speaker in names,
            "mapping_source": "manual" if speaker in names else None,
        }
        for speaker in sorted(speakers)
    ]
    edits = {
        row["segment_id"]: row
        for row in db.execute(
            "SELECT * FROM segment_edits WHERE meeting_id=? AND source_digest=?", key
        )
    }
    for segment in result["segments"]:
        segment["original"] = {"text": segment["text"], "speaker_id": segment["speaker_id"]}
        edit = edits.get(segment["id"])
        segment["reviewed"] = bool(edit and edit["reviewed"])
        if edit:
            segment.update(text=edit["text"], speaker_id=edit["speaker_id"])
        segment["manual_fields"] = [
            field
            for field in ("text", "speaker_id")
            if segment[field] != segment["original"][field]
        ]
        segment["word_alignment_source"] = "model"
    result["review"] = {
        "revision": state["revision"] if state else 0,
        "source_digest": digest,
        "reviewed_segments": sum(s["reviewed"] for s in result["segments"]),
        "unreviewed_flagged_segments": sum(
            bool(s["review_flags"]) and not s["reviewed"] for s in result["segments"]
        ),
        "previous_source_reviews": bool(
            db.execute(
                "SELECT 1 FROM revisions WHERE meeting_id=? AND source_digest!=? LIMIT 1", key
            ).fetchone()
        ),
    }
    result["speaker_names_confirmed"] = bool(speakers) and speakers.issubset(names)
    return result


def get_result(database, meeting_id, *, original=False):
    with connect(database) as db:
        db.execute("BEGIN")
        result, digest = source(db, meeting_id)
        return result if original else view(db, meeting_id, result, digest)


def save(database, meeting_id, patch: ReviewPatch):
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        original, digest = source(db, meeting_id)
        current = view(db, meeting_id, original, digest)
        revision = current["review"]["revision"]
        if digest != patch.source_digest or revision != patch.expected_revision:
            raise ReviewError(
                409, "Сохранённая версия изменилась. Загрузите её перед новой правкой."
            )
        participants = {p["speaker_id"]: p for p in current["participants"]}
        segments = {s["id"]: s for s in current["segments"]}
        changes = []
        for edit in patch.participants:
            if edit.speaker_id not in participants:
                raise ReviewError(422, "Говорящий не принадлежит этой записи.")
            old = participants[edit.speaker_id]["display_name"]
            if old != edit.display_name:
                changes.append(
                    {
                        "entity": "participant",
                        "id": edit.speaker_id,
                        "field": "display_name",
                        "old": old,
                        "new": edit.display_name,
                    }
                )
        for edit in patch.segments:
            if edit.id not in segments:
                raise ReviewError(422, "Фрагмент не принадлежит этой записи.")
            if edit.speaker_id is not None and edit.speaker_id not in participants:
                raise ReviewError(422, "Неизвестный говорящий. Выберите голос из этой записи.")
            for field in ("text", "speaker_id", "reviewed"):
                old, new = segments[edit.id][field], getattr(edit, field)
                if old != new:
                    changes.append(
                        {"entity": "segment", "id": edit.id, "field": field, "old": old, "new": new}
                    )
        if not changes:
            return current
        key = (meeting_id, digest)
        db.execute(
            "INSERT OR IGNORE INTO review_states(meeting_id,source_digest,source_json) "
            "VALUES (?,?,?)",
            (*key, json.dumps(original, ensure_ascii=False)),
        )
        for edit in patch.participants:
            db.execute(
                "DELETE FROM participants WHERE meeting_id=? AND source_digest=? AND speaker_id=?",
                (*key, edit.speaker_id),
            )
            if edit.display_name:
                db.execute(
                    "INSERT INTO participants VALUES (?,?,?,?)",
                    (*key, edit.speaker_id, edit.display_name),
                )
        for edit in patch.segments:
            db.execute(
                """INSERT INTO segment_edits VALUES (?,?,?,?,?,?)
                ON CONFLICT(meeting_id,source_digest,segment_id) DO UPDATE SET
                text=excluded.text,speaker_id=excluded.speaker_id,reviewed=excluded.reviewed""",
                (*key, edit.id, edit.text, edit.speaker_id, int(edit.reviewed)),
            )
        db.execute(
            "UPDATE review_states SET revision=? WHERE meeting_id=? AND source_digest=?",
            (revision + 1, *key),
        )
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?)",
            (
                *key,
                revision + 1,
                datetime.now(UTC).isoformat(),
                json.dumps(changes, ensure_ascii=False),
            ),
        )
        return view(db, meeting_id, original, digest)


def history(database, meeting_id):
    with connect(database) as db:
        if not db.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone():
            raise ReviewError(404, "Встреча не найдена.")
        return [
            {
                "revision": row["revision"],
                "source_digest": row["source_digest"],
                "changed_at": row["changed_at"],
                "change_source": "manual",
                "changes": json.loads(row["changes_json"]),
            }
            for row in db.execute(
                "SELECT * FROM revisions WHERE meeting_id=? ORDER BY changed_at DESC LIMIT 100",
                (meeting_id,),
            )
        ]

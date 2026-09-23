"""Human corrections scoped to one immutable extraction, with atomic revision checks."""

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from autoprotocol import review
from autoprotocol.pipeline.extract import normalize_due
from autoprotocol.storage import connect


class ActionEdit(review.StrictModel):
    id: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1, max_length=1000)
    assignee_text: str | None = Field(max_length=200)
    due_text: str | None = Field(max_length=300)
    due_kind: Literal["date", "interval", "condition", "unspecified"]
    status: Literal["proposed", "agreed", "changed", "cancelled"]
    reviewed: bool
    excluded: bool

    @model_validator(mode="after")
    def valid_fields(self):
        self.task = self.task.strip()
        if not self.task:
            raise ValueError("Укажите текст поручения.")
        self.assignee_text = (self.assignee_text or "").strip() or None
        self.due_text = (self.due_text or "").strip() or None
        if (self.due_text is None) != (self.due_kind == "unspecified"):
            raise ValueError("Для отсутствующего срока выберите «Не указан» и очистите текст.")
        return self


class SummaryEdit(review.StrictModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["fact", "decision", "action", "question"]
    reviewed: bool
    excluded: bool

    @model_validator(mode="after")
    def nonempty(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("Укажите текст итога.")
        return self


class ReportEdit(review.StrictModel):
    id: str = Field(min_length=1, max_length=100)
    direction: str = Field(min_length=1, max_length=200)
    indicator: str | None = Field(max_length=1000)
    problem: str | None = Field(max_length=1000)
    reviewed: bool
    excluded: bool

    @model_validator(mode="after")
    def nonempty(self):
        self.direction = self.direction.strip()
        if not self.direction:
            raise ValueError("Укажите направление доклада.")
        self.indicator = (self.indicator or "").strip() or None
        self.problem = (self.problem or "").strip() or None
        return self


GROUPS = (("actions", ActionEdit), ("summary", SummaryEdit), ("reports", ReportEdit))


class AnalysisPatch(review.StrictModel):
    expected_revision: int = Field(ge=0)
    actions: list[ActionEdit] = Field(default_factory=list, max_length=100)
    summary: list[SummaryEdit] = Field(default_factory=list, max_length=100)
    reports: list[ReportEdit] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        for items in (self.actions, self.summary, self.reports):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Один пункт нельзя изменить дважды.")
        return self


def view(db, row):
    result = json.loads(row["result_json"])
    saved = db.execute(
        "SELECT * FROM analysis_reviews WHERE analysis_id=?", (row["id"],)
    ).fetchone()
    edits = json.loads(saved["edits_json"]) if saved else {}
    occurred_on = json.loads(row["input_json"])["occurred_on"]
    for group, schema in GROUPS:
        result.setdefault(group, [])
        for index, item in enumerate(result[group]):
            identifier = item.get("id", f"{group}_{index + 1}")
            fields = [
                key for key in schema.model_fields if key not in ("id", "reviewed", "excluded")
            ]
            original = {key: item[key] for key in fields}
            item.update(id=identifier, original=original, reviewed=False, excluded=False)
            item.update(edits.get(identifier, {}))
            item["manual_fields"] = [key for key in fields if item[key] != original[key]]
            item["requires_review"] = not item["reviewed"]
            if group == "actions":
                item["due_normalized"] = normalize_due(
                    item["due_text"], item["due_kind"], occurred_on
                )
                item["review_flags"] = [] if item["reviewed"] else ["human_review_required"]
                if not item["assignee_text"]:
                    item["review_flags"].append("unknown_assignee")
                if item["due_text"] and not any(item["due_normalized"].values()):
                    item["review_flags"].append("unresolved_deadline")
    result["review"] = {"revision": saved["revision"] if saved else 0}
    return result


def get_analysis(db, meeting_id, analysis_id):
    row = db.execute(
        "SELECT * FROM analyses WHERE id=? AND meeting_id=?", (analysis_id, meeting_id)
    ).fetchone()
    if row is None:
        raise review.ReviewError(404, "Анализ не найден для этой встречи.")
    return row


def save(database, meeting_id, analysis_id, patch: AnalysisPatch):
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        row = get_analysis(db, meeting_id, analysis_id)
        if row["status"] != "ready" or not row["result_json"]:
            raise review.ReviewError(409, "Анализ ещё не готов.")
        original, digest = review.source(db, meeting_id)
        transcript = review.view(db, meeting_id, original, digest)
        if row["source_digest"] != digest or row["revision"] != transcript["review"]["revision"]:
            raise review.ReviewError(409, "Транскрипт изменился. Сначала выполните новый анализ.")
        current = view(db, row)
        revision = current["review"]["revision"]
        if patch.expected_revision != revision:
            raise review.ReviewError(
                409, "Поручения изменены в другой вкладке. Загрузите сохранённую версию."
            )
        changes = []
        for group, _ in GROUPS:
            items = {item["id"]: item for item in current[group]}
            for edit in getattr(patch, group):
                if edit.id not in items:
                    raise review.ReviewError(422, "Пункт не принадлежит этому анализу.")
                item = items[edit.id]
                for field, value in edit.model_dump(exclude={"id"}).items():
                    if value != item[field]:
                        changes.append(
                            {"id": edit.id, "field": field, "old": item[field], "new": value}
                        )
                item.update(edit.model_dump())
        if not changes:
            return current
        # Store only editable fields; evidence, identifiers and model output remain immutable.
        edits = {
            item["id"]: {key: item[key] for key in schema.model_fields if key != "id"}
            for group, schema in GROUPS
            for item in current[group]
        }
        db.execute(
            "INSERT INTO analysis_reviews VALUES (?,?,?) ON CONFLICT(analysis_id) "
            "DO UPDATE SET revision=excluded.revision,edits_json=excluded.edits_json",
            (analysis_id, revision + 1, json.dumps(edits, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO analysis_revisions VALUES (?,?,?,?)",
            (
                analysis_id,
                revision + 1,
                datetime.now(UTC).isoformat(),
                json.dumps(changes, ensure_ascii=False),
            ),
        )
        return view(db, row)


def history(database, meeting_id, analysis_id):
    with connect(database) as db:
        get_analysis(db, meeting_id, analysis_id)
        return [
            {
                "revision": row["revision"],
                "changed_at": row["changed_at"],
                "change_source": "manual",
                "changes": json.loads(row["changes_json"]),
            }
            for row in db.execute(
                "SELECT * FROM analysis_revisions WHERE analysis_id=? "
                "ORDER BY revision DESC LIMIT 100",
                (analysis_id,),
            )
        ]

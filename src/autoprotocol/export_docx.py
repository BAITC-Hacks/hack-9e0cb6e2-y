"""DOCX from a consistent saved snapshot, using retained reference components."""

import io
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from lxml import etree
from pydantic import Field, model_validator

from autoprotocol import analysis_review, review
from autoprotocol.storage import connect

TEMPLATES = Path(__file__).parent / "templates"
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class Topic(review.StrictModel):
    title: str = Field(min_length=1, max_length=200)
    start_segment_id: str = Field(min_length=1, max_length=100)


class Role(review.StrictModel):
    speaker_id: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=200)


class ExportRequest(review.StrictModel):
    template: Literal["1", "2"] = "1"
    organization: str = Field(default="", max_length=200)
    topics: list[Topic] = Field(default_factory=list, max_length=30)
    roles: list[Role] = Field(default_factory=list, max_length=256)
    analysis_id: str = Field(min_length=1, max_length=100)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_revision: int = Field(ge=0)
    analysis_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def distinct(self):
        if any(not topic.title.strip() for topic in self.topics):
            raise ValueError("Название темы не может быть пустым.")
        if len({role.speaker_id for role in self.roles}) != len(self.roles):
            raise ValueError("Должность голоса указана дважды.")
        if any(not role.role.strip() for role in self.roles):
            raise ValueError("Должность не может состоять из пробелов.")
        return self


def snapshot(database, meeting_id, request):
    with connect(database) as db:
        db.execute("BEGIN")
        original, digest = review.source(db, meeting_id)
        transcript = review.view(db, meeting_id, original, digest)
        row = analysis_review.get_analysis(db, meeting_id, request.analysis_id)
        if row["status"] != "ready" or not row["result_json"]:
            raise review.ReviewError(409, "Дождитесь завершения анализа перед экспортом.")
        if (
            request.source_digest != digest
            or request.transcript_revision != transcript["review"]["revision"]
            or row["source_digest"] != digest
            or row["revision"] != request.transcript_revision
        ):
            raise review.ReviewError(
                409, "Транскрипт изменился. Обновите страницу и выполните актуальный анализ."
            )
        result = analysis_review.view(db, row)
        if result["review"]["revision"] != request.analysis_revision:
            raise review.ReviewError(
                409, "Пункты протокола изменились. Загрузите сохранённую версию."
            )
        if request.template == "2" and "reports" not in json.loads(row["result_json"]):
            raise review.ReviewError(
                409, "Для шаблона №2 запустите новый анализ с показателями и проблемами."
            )
        meeting = dict(db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone())
    people = {p["speaker_id"] for p in transcript["participants"] if p["confirmed"]}
    if any(role.speaker_id not in people for role in request.roles):
        raise review.ReviewError(422, "Должность можно указать только подтверждённому участнику.")
    indices = {s["id"]: i for i, s in enumerate(transcript["segments"])}
    starts = [indices.get(topic.start_segment_id, -1) for topic in request.topics]
    if starts and (starts[0] != 0 or starts != sorted(set(starts)) or -1 in starts):
        raise review.ReviewError(
            422, "Темы должны начинаться с первой реплики и идти по порядку без повторов."
        )
    return {"meeting": meeting, "transcript": transcript, "analysis": result}


def xml_text(value):
    # XML 1.0 cannot encode control characters occasionally present in imported transcripts.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "�", str(value))


def set_text(paragraph, value):
    """Clone formatting but replace all source runs, never leaving reference prose behind."""
    first = paragraph.find(qn("w:r"))
    properties = (
        deepcopy(first.find(qn("w:rPr")))
        if first is not None and first.find(qn("w:rPr")) is not None
        else None
    )
    for child in list(paragraph):
        if child.tag != qn("w:pPr"):
            paragraph.remove(child)
    run = OxmlElement("w:r")
    if properties is not None:
        run.append(properties)
    for index, line in enumerate(xml_text(value).split("\n")):
        if index:
            run.append(OxmlElement("w:br"))
        text = OxmlElement("w:t")
        text.set(qn("xml:space"), "preserve")
        text.text = line
        run.append(text)
    paragraph.append(run)
    return paragraph


def timecode(ms):
    seconds = max(0, int(ms)) // 1000
    return f"{seconds // 60:02}:{seconds % 60:02}"


def render(data, request):
    template = TEMPLATES / f"protocol-{request.template}.docx"
    with ZipFile(template) as package:
        root = parse_xml(package.read("word/document.xml"))
        body = root.find(qn("w:body"))
        prototypes = {}
        for child in body:
            texts = list(child.iter(qn("w:t")))
            if texts and texts[0].text and texts[0].text.startswith("{{"):
                prototypes[texts[0].text.strip("{}")] = deepcopy(child)
        section = deepcopy(body.find(qn("w:sectPr")))
        for child in list(body):
            body.remove(child)

        def paragraph(kind, text, *, keep=False):
            element = set_text(deepcopy(prototypes[kind]), text)
            if keep:
                properties = element.get_or_add_pPr()
                properties.append(OxmlElement("w:keepNext"))
            body.append(element)
            return element

        def table(kind, rows):
            element = deepcopy(prototypes[kind])
            header, sample = element.findall(qn("w:tr"))
            labels = (
                ["Поручение", "Ответственный", "Срок"]
                if kind == "actions"
                else ["Направление / доклад", "Показатель", "Проблема"]
            )
            for cell, label in zip(header.findall(qn("w:tc")), labels, strict=True):
                set_text(cell.find(qn("w:p")), label)
            element.remove(sample)
            for values in rows:
                row = deepcopy(sample)
                for cell, value in zip(row.findall(qn("w:tc")), values, strict=True):
                    set_text(cell.find(qn("w:p")), value)
                element.append(row)
            body.append(element)

        meeting, transcript, result = data["meeting"], data["transcript"], data["analysis"]
        segments = transcript["segments"]
        people = {
            p["speaker_id"]: p["display_name"] for p in transcript["participants"] if p["confirmed"]
        }
        roles = {r.speaker_id: r.role for r in request.roles}
        paragraph("title", "Протокол совещания")
        if request.organization.strip():
            paragraph("organization", request.organization.strip())
        paragraph("subject", "Тема: " + meeting["title"])
        paragraph(
            "body",
            f"Дата встречи: {meeting['occurred_on'] or 'не указана'}. "
            f"Часовой пояс: {meeting['timezone']}.",
        )
        paragraph(
            "body",
            "Подтверждённые участники: " + (", ".join(people.values()) or "имена не подтверждены"),
        )

        topics = request.topics or (
            [Topic(title=meeting["title"], start_segment_id=segments[0]["id"])] if segments else []
        )
        indices = {s["id"]: i for i, s in enumerate(segments)}
        starts = [indices[t.start_segment_id] for t in topics]
        ends = starts[1:] + [len(segments)] if starts else []
        for number, (topic, start, end) in enumerate(zip(topics, starts, ends, strict=True), 1):
            if request.template == "1":
                if number > 1:
                    paragraph("divider", "")
                paragraph("heading", f"Часть {number}. {topic.title}", keep=True)
            elif number == 1:
                paragraph("heading", "Текст совещания", keep=True)
            for segment in segments[start:end]:
                name = people.get(
                    segment["speaker_id"], segment["speaker_id"] or "Говорящий неизвестен"
                )
                speaker = paragraph("speaker", name, keep=True)
                role = (
                    f" ({roles[segment['speaker_id']]})" if segment["speaker_id"] in roles else ""
                )
                run = OxmlElement("w:r")
                props = OxmlElement("w:rPr")
                props.append(OxmlElement("w:i"))
                color = OxmlElement("w:color")
                color.set(qn("w:val"), "555555")
                props.append(color)
                run.append(props)
                text = OxmlElement("w:t")
                text.set(qn("xml:space"), "preserve")
                text.text = xml_text(
                    f"{role}  [{timecode(segment['start_ms'])}–{timecode(segment['end_ms'])}]"
                )
                run.append(text)
                speaker.append(run)
                paragraph("body", segment["text"])
        if not segments:
            paragraph("body", "Речь не обнаружена.")
        paragraph("summary_heading", "Саммари по ключевым пунктам", keep=True)
        evidence = []

        def reference(item):
            evidence.append(item)
            return f"[{len(evidence)}]"

        def mark(item):
            return "Проверено по записи" if item["reviewed"] else "Требует проверки"

        def action_rows(items):
            statuses = {
                "agreed": "Согласовано",
                "proposed": "Предложено",
                "changed": "Изменено",
                "cancelled": "Отменено",
            }
            rows = []
            for item in items:
                due = item["due_normalized"]
                normalized = due["date"] or (
                    f"{due['interval_start']} — {due['interval_end']}"
                    if due["interval_start"]
                    else ""
                )
                deadline = item["due_text"] or "Не указан"
                if normalized and normalized != deadline:
                    deadline += "\n" + normalized
                rows.append(
                    (
                        f"{item['task']} {reference(item)}\n"
                        f"{statuses[item['status']]} · {mark(item)}",
                        item["assignee_text"] or "Требует уточнения",
                        deadline,
                    )
                )
            return rows or [("Поручения не выделены или исключены при проверке", "—", "—")]

        active = {
            group: [item for item in result.get(group, []) if not item["excluded"]]
            for group in ("actions", "summary", "reports")
        }
        kinds = {
            "fact": "Факт",
            "decision": "Решение",
            "action": "Поручение",
            "question": "Открытый вопрос",
        }

        def summaries(items):
            for item in items:
                paragraph(
                    "body",
                    f"{kinds[item['kind']]}: {item['text']} {reference(item)} · {mark(item)}",
                )
            if not items:
                paragraph("body", "Пункты саммари не выделены или исключены при проверке.")

        if request.template == "1":
            if not topics:
                summaries(active["summary"])
                table("actions", action_rows(active["actions"]))

            def in_topic(item, start, end):
                return start <= min(indices[e["segment_id"]] for e in item["evidence"]) < end

            for number, (topic, start, end) in enumerate(zip(topics, starts, ends, strict=True), 1):
                paragraph("subheading", f"Тема {number} — {topic.title}", keep=True)
                summaries([i for i in active["summary"] if in_topic(i, start, end)])
                for item in active["reports"]:
                    if in_topic(item, start, end):
                        paragraph(
                            "body",
                            f"{item['direction']}: {item['indicator'] or 'показатель не указан'}. "
                            f"Проблема: {item['problem'] or 'не указана'}. "
                            f"{reference(item)} · {mark(item)}",
                        )
                table(
                    "actions",
                    action_rows([i for i in active["actions"] if in_topic(i, start, end)]),
                )
        else:
            summaries(active["summary"])
            rows = [
                (
                    f"{i['direction']} {reference(i)}\n{mark(i)}",
                    i["indicator"] or "Не указан",
                    i["problem"] or "Не указана",
                )
                for i in active["reports"]
            ]
            table(
                "reports",
                rows
                or [("Доклады не выделены или исключены при проверке", "Не указан", "Не указана")],
            )
            paragraph("subheading", "Поручения", keep=True)
            table("actions", action_rows(active["actions"]))

        paragraph("heading", "Источники и отметки проверки", keep=True)
        paragraph(
            "body",
            f"Версия транскрипта: {request.transcript_revision}. "
            f"Версия правок анализа: {request.analysis_revision}. "
            f"Анализ: {request.analysis_id}. Источник: {request.source_digest}.",
        )
        unreviewed = sum(not i["reviewed"] for items in active.values() for i in items)
        paragraph(
            "body",
            f"Непроверенных пунктов: {unreviewed}. Непроверенных реплик: "
            f"{sum(not s['reviewed'] for s in segments)}. "
            "Исключённые пункты не включены в таблицы и саммари.",
        )
        for number, item in enumerate(evidence, 1):
            manual = " · Есть ручные исправления" if item["manual_fields"] else ""
            paragraph("speaker", f"[{number}] {mark(item)}{manual}", keep=True)
            for source in item["evidence"]:
                speaker = people.get(
                    source["speaker_id"], source["speaker_id"] or "Говорящий неизвестен"
                )
                paragraph(
                    "body",
                    f"{timecode(source['start_ms'])}–{timecode(source['end_ms'])} · "
                    f"{speaker}: «{source['quote']}»",
                )
        body.append(section)
        stream = io.BytesIO()
        with ZipFile(stream, "w") as output:
            for member in package.infolist():
                content = (
                    etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                    if member.filename == "word/document.xml"
                    else package.read(member.filename)
                )
                output.writestr(member, content)
        return stream.getvalue()


def export(database, meeting_id, request):
    return render(snapshot(database, meeting_id, request), request)

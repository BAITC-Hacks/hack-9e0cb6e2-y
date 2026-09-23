"""Generate labelled synthetic multi-page DOCX samples through the production exporter."""

from pathlib import Path

from autoprotocol.export_docx import ExportRequest, Role, Topic, render


def main():
    output = Path(__file__).resolve().parents[1] / "data/docx-qa"
    output.mkdir(parents=True, exist_ok=True)
    segments = [
        {
            "id": f"seg_{i}",
            "speaker_id": f"SPEAKER_0{i % 2}",
            "start_ms": i * 10000,
            "end_ms": (i + 1) * 10000,
            "text": text,
            "reviewed": i == 0,
        }
        for i, text in enumerate(
            [
                "Синтетический пример: выпуск составил 94% от плана. Поставщик задержал сырьё.",
                "Әлия, Қазақстан бойынша есепті дайындаңыз. Әә Ғғ Ққ Ңң Өө Ұұ Үү Һһ Іі.",
                "Проверить показатели обучения. Дата и ответственный пока не определены.",
                "Предыдущее поручение отменено. Это проверка вёрстки, а не реальное совещание.",
            ]
        )
    ]

    def evidence(i):
        segment = segments[i % len(segments)]
        return [
            {
                **{k: segment[k] for k in ("speaker_id", "start_ms", "end_ms")},
                "segment_id": segment["id"],
                "quote": segment["text"],
                "role": "initial",
            }
        ]

    actions = [
        {
            "task": f"Синтетическое поручение №{i + 1}. "
            + (
                "Сверить данные подразделений, проверить источники и подготовить пояснения. "
                * (11 if i == 0 else 2)
            ),
            "assignee_text": "Әлия" if i % 2 else None,
            "due_text": "после подтверждения результатов" if i % 2 else None,
            "due_normalized": {"date": None, "interval_start": None, "interval_end": None},
            "status": "cancelled" if i == 3 else "agreed",
            "evidence": evidence(i),
            "excluded": False,
            "reviewed": i == 0,
            "manual_fields": ["task"] if i == 0 else [],
        }
        for i in range(8)
    ]
    reports = [
        {
            "direction": f"Синтетическое направление №{i + 1}",
            "indicator": "94% от плана" if i % 2 else None,
            "problem": "Поставщик задержал сырьё. " * 3 if i % 2 else None,
            "evidence": evidence(0),
            "excluded": False,
            "reviewed": False,
            "manual_fields": [],
        }
        for i in range(8)
    ]
    data = {
        "meeting": {
            "title": "Проверка длинных таблиц / Қазақстан",
            "occurred_on": None,
            "timezone": "Asia/Qyzylorda",
        },
        "transcript": {
            "segments": segments,
            "participants": [
                {"speaker_id": "SPEAKER_01", "display_name": "Әлия", "confirmed": True}
            ],
        },
        "analysis": {"actions": actions, "summary": [], "reports": reports},
    }
    for number in ("1", "2"):
        request = ExportRequest(
            template=number,
            organization="СИНТЕТИЧЕСКАЯ ПРОВЕРКА ВЁРСТКИ",
            analysis_id="layout-check",
            source_digest="a" * 64,
            transcript_revision=2,
            analysis_revision=3,
            roles=[Role(speaker_id="SPEAKER_01", role="Руководитель направления")],
            topics=[
                Topic(title="Производство", start_segment_id="seg_0"),
                Topic(title="Обучение", start_segment_id="seg_2"),
            ],
        )
        path = output / f"long-{number}.docx"
        path.write_bytes(render(data, request))
        print(path)


if __name__ == "__main__":
    main()

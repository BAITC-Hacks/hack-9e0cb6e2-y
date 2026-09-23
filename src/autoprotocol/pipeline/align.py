"""Align local STT words to regular diarization intervals without inventing identities."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def interval(item: dict, duration_ms: int, *, allow_point: bool = False) -> tuple[int, int]:
    start, end = item["start_ms"], item["end_ms"]
    if type(start) is not int or type(end) is not int:
        raise ValueError("Timestamps must be integer milliseconds")
    if not (0 <= start <= end <= duration_ms) or (start == end and not allow_point):
        raise ValueError("Invalid interval or interval outside recording")
    return start, end


def merged(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def assign(start: int, end: int, turns: list[dict]) -> dict:
    if start == end:
        return {"speaker_id": None, "review_flags": ["zero_duration"], "candidates": []}
    spans = defaultdict(list)
    for turn in turns:
        left, right = max(start, turn["start_ms"]), min(end, turn["end_ms"])
        if right > left:
            spans[turn["speaker_id"]].append((left, right))
    spans = {speaker: merged(values) for speaker, values in spans.items()}
    candidates = sorted(
        [
            {"speaker_id": speaker, "overlap_ms": sum(b - a for a, b in values)}
            for speaker, values in spans.items()
        ],
        key=lambda item: (-item["overlap_ms"], item["speaker_id"]),
    )
    if not candidates:
        return {"speaker_id": None, "review_flags": ["no_speaker_evidence"], "candidates": []}
    speakers = list(spans)
    simultaneous = any(
        min(b, d) > max(a, c)
        for i, speaker in enumerate(speakers)
        for other in speakers[i + 1 :]
        for a, b in spans[speaker]
        for c, d in spans[other]
    )
    flags = []
    if simultaneous:
        flags.append("overlapping_speech")
    elif len(candidates) > 1:
        flags.append("speaker_boundary")
    if candidates[0]["overlap_ms"] / (end - start) < 0.6:
        flags.append("insufficient_coverage")
    return {
        "speaker_id": None if flags else candidates[0]["speaker_id"],
        "review_flags": flags,
        "candidates": candidates,
    }


def align(stt: dict, diarization: dict) -> dict:
    duration = stt["duration_seconds"]
    other_duration = diarization["duration_seconds"]
    if not all(
        isinstance(d, (int, float)) and math.isfinite(d) and d > 0
        for d in (duration, other_duration)
    ):
        raise ValueError("Recording durations must be finite and positive")
    if abs(duration - other_duration) > 0.05:
        raise ValueError("Recording durations differ; use outputs from the same complete audio")
    duration_ms = round(duration * 1000)
    # Exclusive output hides overlapping voices and is intentionally not used here.
    turns = diarization["turns"]
    for turn in turns:
        interval(turn, duration_ms)
        if not isinstance(turn["speaker_id"], str) or not turn["speaker_id"]:
            raise ValueError("Diarization requires nonempty speaker IDs")
    output = []
    source_ids = set()
    last_start = -1
    for segment in stt["segments"]:
        left, right = interval(segment, duration_ms)
        source_id = segment["id"]
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise ValueError("Source segment IDs must be nonempty and unique")
        if left < last_start:
            raise ValueError("Source segments must be chronologically ordered")
        last_start = left
        source_ids.add(source_id)
        if not isinstance(segment["text"], str):
            raise ValueError("Segment text must be a string")
        words = segment.get("words") or []
        if not words:
            output.append(
                {
                    "source_segment_id": source_id,
                    "start_ms": left,
                    "end_ms": right,
                    "text": segment["text"],
                    "speaker_id": None,
                    "review_flags": ["missing_word_timestamps"],
                    "words": [],
                }
            )
            continue
        normalized = lambda text: " ".join(text.split())  # noqa: E731
        joined = "".join(word["text"] for word in words)
        mismatch = normalized(joined) != normalized(segment["text"])
        previous = left
        group = None
        for index, word in enumerate(words):
            start, end = interval(word, duration_ms, allow_point=True)
            if start < left or end > right or start < previous:
                raise ValueError("Word timestamps must be ordered and inside their source segment")
            previous = start
            attribution = assign(start, end, turns)
            if mismatch:
                attribution["review_flags"].append("word_text_mismatch")
            annotated = {**word, **attribution, "source_word_index": index}
            key = (attribution["speaker_id"], attribution["review_flags"])
            if group is None or key != (group["speaker_id"], group["review_flags"]):
                group = {
                    "source_segment_id": source_id,
                    "start_ms": start,
                    "end_ms": end,
                    "text": "",
                    "speaker_id": attribution["speaker_id"],
                    "review_flags": attribution["review_flags"].copy(),
                    "words": [],
                }
                output.append(group)
            group["end_ms"] = max(group["end_ms"], end)
            group["text"] += word["text"]
            group["words"].append(annotated)
    for index, segment in enumerate(output):
        segment["id"] = f"aligned_{index + 1:04d}"
        segment["text"] = segment["text"].strip()
    return {
        "schema_version": 1,
        "duration_seconds": duration,
        "alignment_policy": "conservative-word-overlap-v1",
        "minimum_coverage": 0.6,
        "speaker_names_confirmed": False,
        "source_segments": stt["segments"],
        "segments": output,
        "statistics": {
            "source_segments": len(stt["segments"]),
            "aligned_segments": len(output),
            "words": sum(len(s["words"]) for s in output),
            "unassigned_words": sum(w["speaker_id"] is None for s in output for w in s["words"]),
            "segments_requiring_review": sum(bool(s["review_flags"]) for s in output),
        },
    }


def render_text(result: dict) -> str:
    def timestamp(value: int) -> str:
        seconds, millis = divmod(value, 1000)
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes:02d}:{seconds:02d}.{millis:03d}"

    lines = ["Черновой транскрипт. Имена и правильность говорящих не подтверждены.", ""]
    for segment in result["segments"]:
        speaker = segment["speaker_id"] or "Говорящий не определён"
        flags = ", ".join(segment["review_flags"])
        label = f" | Требует проверки: {flags}" if flags else ""
        lines.append(
            f"[{timestamp(segment['start_ms'])}–{timestamp(segment['end_ms'])}] "
            f"{speaker} | {segment['id']} | источник {segment['source_segment_id']}{label}"
        )
        lines.extend([segment["text"], ""])
    mismatched_ids = {
        s["source_segment_id"]
        for s in result["segments"]
        if "word_text_mismatch" in s["review_flags"]
    }
    for source in result["source_segments"]:
        if source["id"] in mismatched_ids:
            lines.extend(
                [f"Исходный текст {source['id']} (расхождение со словами):", source["text"], ""]
            )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stt", type=Path)
    parser.add_argument("diarization", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--text-output", type=Path)
    args = parser.parse_args()
    if args.output.resolve() in (args.stt.resolve(), args.diarization.resolve()):
        parser.error("Output must not overwrite input artifacts")
    if args.text_output and args.text_output.resolve() in (
        args.stt.resolve(),
        args.diarization.resolve(),
        args.output.resolve(),
    ):
        parser.error("Text output must differ from JSON output and input artifacts")
    try:
        result = align(
            json.loads(args.stt.read_text(encoding="utf-8")),
            json.loads(args.diarization.read_text(encoding="utf-8")),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Cannot align inputs: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.text_output:
        args.text_output.parent.mkdir(parents=True, exist_ok=True)
        args.text_output.write_text(render_text(result), encoding="utf-8")
    print(json.dumps(result["statistics"]))


if __name__ == "__main__":
    main()

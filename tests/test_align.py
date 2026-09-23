from copy import deepcopy

import pytest

from autoprotocol.pipeline.align import align, assign, render_text


def turn(start, end, speaker="A"):
    return {"start_ms": start, "end_ms": end, "speaker_id": speaker}


def sample():
    return {
        "duration_seconds": 2,
        "segments": [
            {
                "id": "seg_1",
                "start_ms": 0,
                "end_ms": 2000,
                "text": "Добрый день.",
                "words": [
                    {"start_ms": 0, "end_ms": 900, "text": "Добрый"},
                    {"start_ms": 1000, "end_ms": 2000, "text": " день."},
                ],
            }
        ],
    }


def test_speaker_change_splits_source_and_preserves_text_and_indices():
    source = sample()
    original = deepcopy(source)
    result = align(source, {"duration_seconds": 2, "turns": [turn(0, 1000), turn(1000, 2000, "B")]})
    assert [s["speaker_id"] for s in result["segments"]] == ["A", "B"]
    assert [s["source_segment_id"] for s in result["segments"]] == ["seg_1", "seg_1"]
    assert " ".join(s["text"] for s in result["segments"]) == "Добрый день."
    assert [w["source_word_index"] for s in result["segments"] for w in s["words"]] == [0, 1]
    assert source == original


@pytest.mark.parametrize(
    ("turns", "flag"),
    [
        ([turn(0, 1000), turn(500, 1000, "B")], "overlapping_speech"),
        ([turn(0, 500), turn(500, 1000, "B")], "speaker_boundary"),
        ([], "no_speaker_evidence"),
        ([turn(0, 300)], "insufficient_coverage"),
    ],
)
def test_ambiguous_words_are_not_assigned(turns, flag):
    result = assign(0, 1000, turns)
    assert result["speaker_id"] is None
    assert flag in result["review_flags"]


def test_duplicate_same_speaker_intervals_do_not_inflate_coverage():
    result = assign(0, 1000, [turn(0, 400), turn(0, 400)])
    assert result["speaker_id"] is None
    assert result["candidates"][0]["overlap_ms"] == 400


def test_point_word_has_no_invented_timestamp_or_identity():
    assert assign(500, 500, [turn(0, 1000)])["review_flags"] == ["zero_duration"]


def test_exclusive_turns_do_not_hide_overlap():
    d = {
        "duration_seconds": 2,
        "turns": [turn(0, 2000), turn(0, 2000, "B")],
        "exclusive_turns": [turn(0, 2000)],
    }
    assert all(s["speaker_id"] is None for s in align(sample(), d)["segments"])


def test_missing_words_keeps_original_text_without_assigning_whole_segment():
    source = sample()
    source["segments"][0]["words"] = []
    result = align(source, {"duration_seconds": 2, "turns": [turn(0, 2000)]})
    assert result["segments"][0]["text"] == "Добрый день."
    assert result["segments"][0]["speaker_id"] is None


def test_mismatched_word_text_retains_original_and_flags_review():
    source = sample()
    source["segments"][0]["text"] = "Иной исходный текст"
    result = align(source, {"duration_seconds": 2, "turns": [turn(0, 2000)]})
    assert "word_text_mismatch" in result["segments"][0]["review_flags"]
    assert result["source_segments"][0]["text"] == "Иной исходный текст"


def test_recording_duration_mismatch_is_rejected():
    with pytest.raises(ValueError, match="durations differ"):
        align(sample(), {"duration_seconds": 60, "turns": []})


def test_word_outside_parent_is_rejected():
    source = sample()
    source["segments"][0]["words"][0]["end_ms"] = 2100
    with pytest.raises(ValueError, match="outside recording"):
        align(source, {"duration_seconds": 2, "turns": []})


def test_readable_transcript_exposes_unknown_speaker_and_preserves_source():
    source = sample()
    source["segments"][0]["text"] = "Исходный текст"
    result = align(source, {"duration_seconds": 2, "turns": []})
    text = render_text(result)
    assert "Говорящий не определён" in text
    assert "Требует проверки" in text
    assert "Исходный текст" in text
    assert "00:00.000" in text
    assert "источник seg_1" in text

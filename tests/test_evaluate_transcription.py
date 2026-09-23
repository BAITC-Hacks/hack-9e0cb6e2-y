import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from autoprotocol.evaluation import (
    aggregate,
    edit_distance,
    evaluate_manifest,
    normalize,
    read_input,
    score_text,
    validate_manifest,
)


def reference_distance(left, right):
    """Independent small dynamic-programming oracle for the bit-vector algorithm."""
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        following = [i]
        for j, b in enumerate(right, 1):
            following.append(min(row[j] + 1, following[j - 1] + 1, row[j - 1] + (a != b)))
        row = following
    return row[-1]


def write_manifest(tmp_path, samples):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "samples": samples}), encoding="utf-8")
    return path


def sample(sample_id="ru", language="ru"):
    return {
        "id": sample_id,
        "language": language,
        "dataset_kind": "synthetic",
        "reference": {"format": "text", "path": "reference.txt"},
        "hypothesis": {"format": "segments_json", "path": "hypothesis.json"},
    }


def test_edit_distance_matches_independent_oracle_including_wide_bit_vectors():
    rng = random.Random(1299)
    pairs = [("", ""), ("a", ""), ("", "abc"), ("kitten", "sitting")]
    pairs += [
        (
            "".join(rng.choices("abcәөұ", k=rng.randrange(130))),
            "".join(rng.choices("abcәөұ", k=rng.randrange(130))),
        )
        for _ in range(120)
    ]
    for left, right in pairs:
        expected = reference_distance(left, right)
        assert edit_distance(left, right) == expected
        assert edit_distance(right, left) == expected
    assert edit_distance(["бір", "екі", "үш"], ["бір", "үш"]) == 1


def test_normalization_preserves_kazakh_and_documents_punctuation_policy():
    assert normalize("  ОТЧЁТ — ӘҒҚҢӨҰҮҺІ! 94%\nплана") == "отчет әғқңөұүһі 94 плана"
    assert normalize("е\u0308 Ａ") == "е a"
    assert score_text("Құжат дайын.", "қужат дайын")["word_errors"] == 1
    assert score_text("Отчёт: 94%.", "отчет 94")["wer"] == 0


def test_counts_and_empty_hypothesis_are_not_confused_with_missing_reference():
    result = score_text("abc", "axcy")
    assert result["wer"] == 1
    assert result["character_errors"] == 2
    assert result["cer"] == pytest.approx(2 / 3)
    assert score_text("один два три", "")["wer"] == 1
    assert score_text("а", "а б в г")["wer"] == 3
    for reference in ("", "...!? ", " \n"):
        result = score_text(reference, "")
        assert result == {
            "status": "not_evaluated",
            "reason": "empty_reference",
            "wer": None,
            "cer": None,
        }


def test_manifest_reports_language_gaps_without_zero_error_claims(tmp_path):
    (tmp_path / "reference.txt").write_text("Отчёт готов.", encoding="utf-8")
    (tmp_path / "hypothesis.json").write_text(
        json.dumps({"segments": [{"text": "отчет"}, {"text": "готов"}]}), encoding="utf-8"
    )
    cases = [sample()]
    cases += [{**sample("kk", "kk"), "status": "not_evaluated", "reason": "No labelled audio"}]
    cases += [{**sample("mixed", "mixed"), "hypothesis": None}]
    result = evaluate_manifest(write_manifest(tmp_path, cases))
    assert result["overall"]["status"] == "partial"
    assert result["overall"]["evaluated"] == 1
    assert result["overall"]["wer"] == 0
    assert result["overall"]["dataset_kinds_evaluated"] == ["synthetic"]
    assert result["by_language"]["kk"]["status"] == "not_evaluated"
    assert result["by_language"]["mixed"]["status"] == "missing"
    assert result["by_language"]["kk"]["wer"] is None
    assert result["by_language"]["mixed"]["cer"] is None
    assert not result["all_languages_evaluated"]
    assert result["samples"][0]["hypothesis"]["field"] == "segments"
    assert len(result["samples"][0]["reference"]["sha256"]) == 64
    # Reports contain hashes/counts, never transcript content.
    assert "Отчёт готов" not in json.dumps(result, ensure_ascii=False)


def test_aggregate_is_micro_average_and_empty_corpus_is_unknown():
    cases = [
        {**score_text("a", "b"), "dataset_kind": "synthetic"},
        {**score_text("a a a a a a a a a", "a a a a a a a a a"), "dataset_kind": "synthetic"},
    ]
    result = aggregate(cases)
    assert result["wer"] == 0.1
    assert result["cer"] == 0.1
    assert aggregate([])["wer"] is None
    assert aggregate([])["status"] == "missing"
    assert not aggregate([])["coverage_complete"]


def test_segment_source_is_explicit_and_empty_array_is_valid_hypothesis(tmp_path):
    path = tmp_path / "aligned.json"
    path.write_text(
        json.dumps({"segments": [], "source_segments": [{"text": "исходный текст"}]}),
        encoding="utf-8-sig",
    )
    spec = {"format": "segments_json", "path": str(path)}
    assert read_input(spec, tmp_path)[0] == ""
    assert read_input({**spec, "field": "source_segments"}, tmp_path)[0] == "исходный текст"
    with pytest.raises(ValueError, match="Segment field"):
        read_input({**spec, "field": "unrelated"}, tmp_path)


def test_missing_and_malformed_files_do_not_become_perfect_predictions(tmp_path):
    path = write_manifest(tmp_path, [sample()])
    assert evaluate_manifest(path)["samples"][0]["reason"] == "missing_reference_file"
    (tmp_path / "reference.txt").write_text("слово", encoding="utf-8")
    assert evaluate_manifest(path)["samples"][0]["reason"] == "missing_hypothesis_file"
    (tmp_path / "hypothesis.json").write_text('{"segments":[{"text":null}]}', encoding="utf-8")
    result = evaluate_manifest(path)["samples"][0]
    assert result["status"] == "not_evaluated"
    assert result["reason"] == "invalid_hypothesis"
    assert result["wer"] is None


@pytest.mark.parametrize(
    "cases",
    [
        [sample(), sample()],
        [{**sample(), "language": "unknown"}],
        [{**sample(), "dataset_kind": "unknown"}],
        [{**sample(), "status": "not_evaluated"}],
    ],
)
def test_manifest_rejects_ambiguous_identifiers_and_coverage(cases):
    with pytest.raises(ValueError):
        validate_manifest({"schema_version": 1, "samples": cases})


def test_cli_preserves_inputs_and_can_enforce_incomplete_coverage(tmp_path):
    manifest = write_manifest(tmp_path, [])
    script = Path(__file__).resolve().parents[1] / "scripts/evaluate_transcription.py"
    output = tmp_path / "result.json"
    command = [sys.executable, str(script), "--manifest", str(manifest)]
    result = subprocess.run(
        [*command, "--output", str(output), "--require-complete"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(output.read_text())["overall"]["wer"] is None
    original = manifest.read_bytes()
    result = subprocess.run(
        [*command, "--output", str(manifest)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert manifest.read_bytes() == original

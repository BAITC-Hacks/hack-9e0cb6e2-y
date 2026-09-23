"""Reproducible offline WER/CER; this module never runs or downloads a model.

JSON manifests refer to UTF-8 text files or JSON objects with a segment array.
Paths are relative to the manifest. For align.py output, choose ``source_segments``
to measure original ASR text, or ``segments`` to measure displayed aligned text.
Speaker attribution and diarization are deliberately outside these metrics.
"""

import hashlib
import json
import unicodedata
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

LANGUAGES = ("ru", "kk", "mixed")
NORMALIZATION = {
    "id": "unicode-word-v1",
    "steps": [
        "Unicode NFKC; casefold; Russian ё becomes е",
        "Keep Unicode letters, combining marks and numbers; replace other characters by spaces",
        "Collapse whitespace; preserve Kazakh letters; no transliteration or number expansion",
        "WER uses whitespace-separated tokens; CER excludes all whitespace",
    ],
    "distance": "exact Levenshtein; insertion/deletion/substitution each cost 1",
    "aggregation": "micro-average: sum edit distances / sum reference units",
    "empty_reference": "not_evaluated; null rates, including when hypothesis is also empty",
    "empty_hypothesis": "valid: every reference unit is a deletion",
    "rate_units": "ratio, not percent; rates can exceed 1 when insertions exceed reference length",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return " ".join(
        "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in text).split()
    )


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Exact bit-vector Levenshtein distance, using Python's arbitrary-width integers.

    The shorter sequence is the bit pattern. No heuristic matching or clipping is
    used, so long transcripts retain exact CER without a quadratic Python matrix.
    """
    if len(reference) > len(hypothesis):
        reference, hypothesis = hypothesis, reference
    length = len(reference)
    if not length:
        return len(hypothesis)
    masks = {}
    for index, token in enumerate(reference):
        masks[token] = masks.get(token, 0) | (1 << index)
    limit = (1 << length) - 1
    positive, negative = limit, 0
    score, last = length, 1 << (length - 1)
    for token in hypothesis:
        equal = masks.get(token, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        plus = negative | ~(horizontal | positive)
        minus = positive & horizontal
        if plus & last:
            score += 1
        if minus & last:
            score -= 1
        plus = (plus << 1) | 1
        minus <<= 1
        positive = (minus | ~(vertical | plus)) & limit
        negative = (plus & vertical) & limit
    return score


def score_text(reference: str, hypothesis: str) -> dict:
    """Return aggregate-safe counts; an absent/empty reference is not a perfect score."""
    expected, actual = normalize(reference), normalize(hypothesis)
    words, predicted_words = expected.split(), actual.split()
    characters, predicted_characters = expected.replace(" ", ""), actual.replace(" ", "")
    if not words:
        return {"status": "not_evaluated", "reason": "empty_reference", "wer": None, "cer": None}
    word_errors = edit_distance(words, predicted_words)
    character_errors = edit_distance(characters, predicted_characters)
    return {
        "status": "evaluated",
        "reference_words": len(words),
        "hypothesis_words": len(predicted_words),
        "word_errors": word_errors,
        "wer": word_errors / len(words),
        "reference_characters": len(characters),
        "hypothesis_characters": len(predicted_characters),
        "character_errors": character_errors,
        "cer": character_errors / len(characters),
    }


def input_path(spec: dict, base: Path) -> Path:
    if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not spec["path"]:
        raise ValueError("Each input needs a nonempty path")
    return (base / spec["path"]).resolve()


def read_input(spec: dict, base: Path) -> tuple[str, dict]:
    path = input_path(spec, base)
    content = path.read_bytes()
    text = content.decode("utf-8-sig")
    format_name = spec.get("format")
    provenance = {
        "path": spec["path"],
        "format": format_name,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if format_name == "text":
        return text, provenance
    if format_name != "segments_json":
        raise ValueError("Input format must be text or segments_json")
    field = spec.get("field", "segments")
    if field not in ("segments", "source_segments"):
        raise ValueError("Segment field must be segments or source_segments")
    document = json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get(field), list):
        raise ValueError(f"Expected a JSON object with a {field} array")
    segments = document[field]
    if any(not isinstance(s, dict) or not isinstance(s.get("text"), str) for s in segments):
        raise ValueError("Each segment must have a string text")
    provenance["field"] = field
    # The file order is authoritative: do not sort, deduplicate or hide text errors.
    return " ".join(s["text"] for s in segments), provenance


def validate_manifest(manifest: dict) -> list[dict]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Manifest schema_version must be 1")
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        raise ValueError("Manifest samples must be an array")
    ids = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("Each sample must be an object")
        sample_id = sample.get("id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in ids:
            raise ValueError("Sample IDs must be nonempty and unique")
        ids.add(sample_id)
        if sample.get("language") not in LANGUAGES:
            raise ValueError(f"Sample {sample_id}: language must be ru, kk or mixed")
        if sample.get("dataset_kind") not in ("synthetic", "human_recording"):
            raise ValueError(
                f"Sample {sample_id}: dataset_kind must be synthetic or human_recording"
            )
        if sample.get("status", "ready") not in ("ready", "not_evaluated"):
            raise ValueError(f"Sample {sample_id}: status must be ready or not_evaluated")
        if sample.get("status") == "not_evaluated":
            reason = sample.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"Sample {sample_id}: not_evaluated requires a reason")
    return samples


def evaluate_sample(sample: dict, base: Path) -> dict:
    result = {key: sample[key] for key in ("id", "language", "dataset_kind")}
    result.update(wer=None, cer=None)
    if sample.get("status") == "not_evaluated":
        return {**result, "status": "not_evaluated", "reason": sample["reason"]}
    values = {}
    for name in ("reference", "hypothesis"):
        spec = sample.get(name)
        if spec is None:
            return {**result, "status": "missing", "reason": f"missing_{name}"}
        try:
            values[name], result[name] = read_input(spec, base)
        except FileNotFoundError:
            return {**result, "status": "missing", "reason": f"missing_{name}_file"}
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            return {
                **result,
                "status": "not_evaluated",
                "reason": f"invalid_{name}",
                "detail": str(error),
            }
    return {**result, **score_text(values["reference"], values["hypothesis"])}


def aggregate(samples: list[dict]) -> dict:
    evaluated = [sample for sample in samples if sample["status"] == "evaluated"]
    counts = Counter(sample["status"] for sample in samples)
    if evaluated:
        status = "evaluated" if len(evaluated) == len(samples) else "partial"
    else:
        status = "missing" if not samples or counts["missing"] == len(samples) else "not_evaluated"
    result = {
        "status": status,
        "samples": len(samples),
        "evaluated": len(evaluated),
        "missing": counts["missing"],
        "not_evaluated": counts["not_evaluated"],
        "coverage_complete": bool(samples) and len(samples) == len(evaluated),
        "dataset_kinds_evaluated": sorted({sample["dataset_kind"] for sample in evaluated}),
        "word_errors": sum(sample["word_errors"] for sample in evaluated),
        "reference_words": sum(sample["reference_words"] for sample in evaluated),
        "character_errors": sum(sample["character_errors"] for sample in evaluated),
        "reference_characters": sum(sample["reference_characters"] for sample in evaluated),
        "wer": None,
        "cer": None,
    }
    if evaluated:
        result["wer"] = result["word_errors"] / result["reference_words"]
        result["cer"] = result["character_errors"] / result["reference_characters"]
    return result


def evaluate_manifest(manifest_path: Path) -> dict:
    content = manifest_path.read_bytes()
    manifest = json.loads(content.decode("utf-8-sig"))
    samples = [
        evaluate_sample(sample, manifest_path.parent) for sample in validate_manifest(manifest)
    ]
    languages = {
        language: aggregate([sample for sample in samples if sample["language"] == language])
        for language in LANGUAGES
    }
    return {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "normalization": NORMALIZATION,
        "scope": "Text accuracy only; synthetic speech is not validation on real meetings",
        "samples": samples,
        "by_language": languages,
        "overall": aggregate(samples),
        "all_languages_evaluated": all(item["evaluated"] > 0 for item in languages.values()),
    }

"""Measure saved transcription against labelled text; no model or network is used."""

import argparse
import json
from pathlib import Path

from autoprotocol.evaluation import evaluate_manifest, input_path, validate_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Exit 1 if a sample is missing/not evaluated or a language has no evaluated sample",
    )
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        protected = {args.manifest.resolve()}
        for sample in validate_manifest(manifest):
            for name in ("reference", "hypothesis"):
                if sample.get(name) is not None:
                    protected.add(input_path(sample[name], args.manifest.parent))
        if args.output.resolve() in protected:
            parser.error("Output must not overwrite the manifest or an input file")
        result = evaluate_manifest(args.manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Cannot evaluate corpus: {error}\n")
    print(json.dumps({"overall": result["overall"], "by_language": result["by_language"]}))
    if args.require_complete and not (
        result["overall"]["coverage_complete"] and result["all_languages_evaluated"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

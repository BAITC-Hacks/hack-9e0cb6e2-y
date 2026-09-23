"""Real local LLM evaluation on labelled synthetic text, not an ASR accuracy test."""

import argparse
import json
from pathlib import Path

from autoprotocol.config import Settings
from autoprotocol.pipeline.extract import extract, server, validate_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        action="append",
        choices=[
            "deadline_changed",
            "no_actions_and_injection",
            "no_deadline",
            "reports_with_metrics",
        ],
    )
    args = parser.parse_args()
    config = Settings()
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "tests/fixtures/extraction_cases.json").read_text(encoding="utf-8"))
    cases += json.loads((root / "tests/fixtures/report_cases.json").read_text(encoding="utf-8"))
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    reports = []
    suffix = "-" + "-".join(args.case) if args.case else ""
    output = config.data_dir / f"extraction-evaluation{suffix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with server(config.llm_server, config.llm_model) as request:
        for case in cases:
            traces = []

            def traced_request(path, payload):
                response = request(path, payload)
                if path == "/v1/chat/completions":
                    choice = response["choices"][0]
                    trace = {
                        "finish_reason": choice["finish_reason"],
                        "content": choice["message"]["content"],
                    }
                    try:
                        validate_output(trace["content"], case["meeting"])
                    except (ValueError, TypeError) as error:
                        trace["validation_error"] = str(error)
                    traces.append(trace)
                    # Only the repository's labelled synthetic fixtures are used by this script.
                    (config.data_dir / f"extraction-trace-{case['id']}.json").write_text(
                        json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                return response

            traced_request.metrics = request.metrics
            try:
                # Labels are deliberately excluded from model input.
                result = extract(case["meeting"], traced_request)
                expected = case["expected"]
                checks = {"action_count": len(result["actions"]) == expected["actions"]}
                if "reports" in expected:
                    rows = result["reports"]
                    checks.update(
                        report_count=len(rows) == expected["reports"],
                        indicator=any(
                            expected["indicator_contains"] in (r["indicator"] or "") for r in rows
                        ),
                        problem=any(
                            expected["problem_contains"] in (r["problem"] or "") for r in rows
                        ),
                        unknown_indicator=sum(r["indicator"] is None for r in rows)
                        == expected["null_indicators"],
                    )
                if result["actions"] and expected["actions"] == 1:
                    action = result["actions"][0]
                    checks["assignee"] = action["assignee_text"] == expected["assignee"]
                    if "due" in expected:
                        checks["deadline"] = action["due_text"] == expected["due"]
                    if "due_contains" in expected:
                        checks["deadline"] = expected["due_contains"] in (action["due_text"] or "")
                    if "source_ids" in expected:
                        checks["sources"] = set(expected["source_ids"]).issubset(
                            {e["segment_id"] for e in action["evidence"]}
                        )
                report = {
                    "case": case["id"],
                    "passed": all(checks.values()),
                    "checks": checks,
                    "result": result,
                }
            except RuntimeError:
                report = {"case": case["id"], "passed": False, "error": "extraction_failed"}
            reports.append(report)
            output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": case["id"], "passed": report["passed"]}), flush=True)
    if not all(report["passed"] for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Command-line tools for independent, reproducible agent evaluations."""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from folio_eval.checkpoint import Checkpoint, atomic_json, digest
from folio_eval.dataset import generate_suite, save_suite
from folio_eval.runner import execute_suite_cases
from folio_eval.schema import BenchmarkSuite, validate_action
from folio_eval.scoring import gate, summarize
from folio_eval.site import serve, server
from folio_eval.tracing import TraceWriter


def load_candidate(path: str | None, suite: BenchmarkSuite) -> dict | None:
    if not path:
        return None
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("cases"), dict):
        raise ValueError("Candidate format must be {version: 1, cases: {case_id: [actions]}}")
    known = {case.id for case in suite.cases}
    if set(raw["cases"]) != known:
        missing = sorted(known - set(raw["cases"]))
        extra = sorted(set(raw["cases"]) - known)
        raise ValueError(f"Candidate must specify every case: missing={missing[:5]}, extra={extra[:5]}")
    return {case_id: [validate_action(a) for a in actions] for case_id, actions in raw["cases"].items()}


def run(args: argparse.Namespace) -> int:
    suite = BenchmarkSuite.load(args.suite) if args.suite else generate_suite(args.count)
    cases = suite.cases[:args.limit] if args.limit else suite.cases
    selected = {c.id for c in cases}
    # Candidates must match the entire suite; slicing happens only after validation.
    candidate = load_candidate(args.candidate, suite)
    fp = digest({"suite": suite.fingerprint, "cases": sorted(selected),
                 "candidate": candidate, "timeout_ms": args.timeout_ms})
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint(output / "checkpoint.json", fp, resume=args.resume)
    trace = TraceWriter(output / "events.jsonl")
    fixture = server(port=0)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    host, port = fixture.server_address
    base_url = f"http://127.0.0.1:{port}"
    try:
        pending = [case for case in cases if not checkpoint.has(case.id)]
        def completed(case, result):
            checkpoint.append(case.id, result)
            print(f"{case.id}: {result['status']} ({result['latency_ms']}ms)", flush=True)
        if pending:
            execute_suite_cases(pending, base_url, completed, candidate=candidate,
                                trace=trace, timeout_ms=args.timeout_ms)
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=2)
    # Use suite order in outputs even when a resumed run completed cases in a different order.
    results = [checkpoint.data["cases"][case.id] for case in cases if checkpoint.has(case.id)]
    summary = summarize(results, len(cases))
    passed, reason = gate(summary, min_success_rate=args.min_success_rate)
    report = {"version": 1, "suite": suite.name, "suite_sha256": suite.fingerprint,
              "mode": "candidate" if candidate is not None else "reference",
              "candidate_path": args.candidate, "threshold": args.min_success_rate,
              "gate": {"passed": passed, "reason": reason}, "summary": summary, "results": results}
    atomic_json(output / "report.json", report)
    print(json.dumps({"gate": report["gate"], "summary": summary,
                      "report": str(output / "report.json")}, indent=2))
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m folio_eval", description="Folio browser-agent evaluations")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="write a versioned 250-task JSON benchmark")
    generate.add_argument("--count", type=int, default=250)
    generate.add_argument("--output", default="benchmarks/folio-browser-v1.json")
    run_parser = commands.add_parser("run", help="evaluate reference or candidate trajectories")
    run_parser.add_argument("--suite", help="JSON benchmark (defaults to generated v1)")
    run_parser.add_argument("--candidate", help="JSON file mapping all case IDs to candidate actions")
    run_parser.add_argument("--count", type=int, default=250, help="number of generated cases")
    run_parser.add_argument("--limit", type=int, default=0, help="evaluate first N cases of suite")
    run_parser.add_argument("--output", default="outputs/eval")
    run_parser.add_argument("--resume", action="store_true", help="resume matching checkpoint")
    run_parser.add_argument("--timeout-ms", type=int, default=15000)
    run_parser.add_argument("--min-success-rate", type=float, default=1.0)
    site_parser = commands.add_parser("serve", help="serve the standalone benchmark fixture")
    site_parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "generate":
        suite = save_suite(args.output, args.count)
        print(f"Wrote {len(suite.cases)} cases to {args.output} ({suite.fingerprint})")
        return 0
    if args.command == "serve":
        serve(port=args.port)
        return 0
    if args.limit < 0 or args.timeout_ms < 100:
        parser.error("--limit must be >= 0 and --timeout-ms >= 100")
    try:
        return run(args)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

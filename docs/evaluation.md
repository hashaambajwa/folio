# Folio Agent Reliability Benchmark

Folio keeps its original scanner, planner, recorder, renderer, and validator pipeline. This additional package evaluates browser-agent actions against reproducible task outcomes.

## Features
- 250 deterministic benchmark cases across create, complete, edit, delete and filter workflows (50 each).
- Isolated Chromium contexts and a locally hosted interactive test fixture.
- Versioned and validated action/assertion JSON; restricted cross-origin navigation.
- Objective DOM success checks; tool success and actual task success are measured separately.
- Atomic, fingerprint-verified checkpoints and resume support.
- Redacted JSONL traces, per-category grading, median/p95 latency, failure categories and configurable pass-rate gates.
- GitHub Actions unit tests, browser integration checks, and an automated ten-case smoke gate.

## Run

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m unittest discover -s tests -v

python -m folio_eval generate --output benchmarks/folio-browser-v1.json
python -m folio_eval run --count 5 --output outputs/eval-smoke
python -m folio_eval run --suite benchmarks/folio-browser-v1.json --output outputs/eval-250
python -m folio_eval run --suite benchmarks/folio-browser-v1.json --output outputs/eval-250 --resume

FOLIO_BROWSER_TEST=1 python -m unittest discover -s tests -v
```

Artifacts: outputs/eval-250/report.json, checkpoint.json, events.jsonl. The full suite can be evaluated against recorded gold trajectories, or candidate actions produced by any model or agent.

## Candidate format

The optional --candidate path must hold a JSON object with version: 1 and cases: {case_id: [actions]}. Every case ID from the suite must be present. An example for one case:

```json
{
  "version": 1,
  "cases": {
    "create-0001": [
      {"type": "fill", "selector": "#new-task", "value": "folio task 0001"},
      {"type": "click", "selector": "#add-task"}
    ]
  }
}
```

This example is intentionally incomplete; it must include every case of the chosen suite. Use --count 1 or a one-case generated suite to try it. Pass --candidate, --suite, --output, and optionally --min-success-rate to run real candidate actions. Actions are validated and independently scored against expected DOM outcomes.

## Interpretation

The 250 tasks are 250 deterministic variants of **five workflows on a single synthetic site**, not 250 different real websites. Reference success rates measure fixture and runner correctness, not an LLM's performance. Candidate accuracy, actual token costs, cost reductions, model comparisons, and failure recovery must be measured before being reported as achievements.

Folio's existing scanner already has screenshot and DOM context for some LLM workflow-repair stages. This isolated benchmark does not yet run or benchmark that repair loop. It also does not render videos; use the original pipeline and main.py validate for production demo-video checks.

The benchmark server binds to loopback and browser external requests are blocked. Do not run arbitrary untrusted websites or enter secrets in this fixture. Store only redacted trace events and avoid publishing private screenshot artifacts.

## Future extension

Implement a live planner-to-candidate exporter, evaluate the planner on held-out multi-site tasks, instrument actual model usage/cost and support screenshot-grounded recovery within evaluation runs. Compare model revisions using pinned suites, identical context budgets and matched environments.

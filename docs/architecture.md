# Folio architecture

## What the system does

Folio is built around two related but distinct workflows: making an automated demonstration of an application and measuring the reliability of browser-agent actions. The second is independently testable and does not require rendering a video.

    Target web app
       |
       v
    scanner.py  -- discover bounded page-state transitions and candidate paths
       |
       v
    planner.py  -- choose meaningful, validated workflows / feature coverage
       |
       v
    recorder.py -- replay selected interactions in Playwright; capture evidence
       |
       v
    renderer.py -- assemble final demo video with FFmpeg
       |
       v
    validator.py -- check recorded artifacts and output-quality signals

    Separate reliability track:
    folio_eval/ -- versioned cases -> candidate actions -> Chromium execution
               -> DOM assertions -> traces/checkpoints -> report/paired comparison

## Design decisions

### Browser execution before claims

The scanner builds a bounded state graph, and candidate workflows are only promoted after Playwright validates their actions. LLM-generated selectors and proposed repairs are untrusted until execution. This avoids equating fluent planning text with a successful UI action.

### Explicit coverage

The planner records covered and uncovered feature areas. This makes feature-selection tradeoffs inspectable instead of treating every recorded navigation step as useful.

### Independent evaluation

The evaluation runner uses deterministic assertions against a local fixture rather than visual judgement of a final video. It distinguishes successful tool execution from the intended task outcome. Candidate actions are represented as structured data with schema validation, constrained navigation and isolated execution.

### Reproducible outputs

Per-run artifacts, redacted traces, resumable checkpoints and regression comparisons allow repeated runs and debugging without recording new videos. Outputs are not committed to Git by default.

## Tradeoffs and limitations

- The 250 synthetic cases cover only five workflow families on a single app. They are not an estimate of live browser-agent success across the web.
- Gold-trajectory results primarily test harness correctness. To establish model quality, run pinned model-generated candidates on multiple held-out sites.
- LLM workflow repairs and the standalone evaluation package are not yet evaluated together as one end-to-end benchmark.
- Actual token costs, end-to-end cost improvements and cross-model comparisons must be measured before making claims about them.
- Recording and rendering are separate from task-success grading.

## Run it

See the root README for the CLI flow and docs/evaluation.md for evaluator commands, candidate format, environment setup and interpreting reports.

## Next engineering milestones

1. Add a live agent-to-candidate exporter with pinned prompts/models and trace provenance.
2. Create held-out multi-site tasks with robust outcome assertions and controlled state reset.
3. Evaluate selector repair and recovery under injected failures.
4. Record actual latency/cost per tool and model call; publish confidence intervals for observed pass rates.
5. Produce a short, reproducible demonstration video and benchmark results table with artifact hashes.

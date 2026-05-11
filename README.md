# Folio

AI demo video generator engine prototype.

## Setup

Create and activate a local Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Install FFmpeg on macOS:

```bash
brew install ffmpeg
```

Verify local tools:

```bash
python --version
ffmpeg -version
python main.py --help
```

## Smoke Workflow

Run the current end-to-end flow against TodoMVC:

```bash
python main.py scan https://todomvc.com/examples/react/dist --job-id smoke
python main.py plan outputs/smoke/scan.json
python main.py record outputs/smoke/plan.json
python main.py render outputs/smoke/recording.json
```

Expected final output:

```text
outputs/smoke/final.mp4
```

## Current Pipeline

```text
scanner.py  -> scan.json
planner.py  -> plan.json
recorder.py -> recording.webm + recording.json
renderer.py -> final.mp4 + render.json
```

Generated artifacts live under `outputs/` and are ignored by git.

## Scanner Exploration

`scan` captures the initial page plus a bounded graph of post-interaction UI states. By default it probes up to 3 steps deep, keeps up to 16 discovered states, and tries up to 5 safe candidate actions per state.

```bash
python main.py scan https://example.com --probe-depth 3 --max-states 16 --max-actions-per-state 5
```

Use `--no-probes` when you only want the first loaded page state.

When probes are enabled, `scan.json` also includes `candidate_paths`: scored, replayable workflows assembled from the discovered state graph. Paths that create or mutate state are ranked above passive navigation because they usually make stronger demos. Plans include `planner.coverage`, which tracks selected validated workflows, covered feature areas, uncovered feature areas, and missing workflows.

Successful probe transitions include `outcome_summary` with URL, visible text, control, and control-state changes. These summaries help planners explain why a path is useful instead of relying only on selectors.

Use `--llm-expand` when the initial probe graph finds useful pages but misses the core workflow. Folio asks the LLM for bounded workflow candidates using selectors from discovered states, then validates every proposed action in Playwright before adding it to `candidate_paths`.

```bash
python main.py scan https://example.com --llm-expand --max-llm-expansions 2
```

Add `--source-root` when you also have the app code locally. Folio will add a bounded source summary to `scan.json` with route, component, package, README, and UI-string hints for the planner.

```bash
python main.py scan https://example.com --source-root /path/to/app
```

The source collector prioritizes route/page files, app entry points, and likely components before spending the remaining file budget. If a larger app is clipped, increase the relevant budget and check `source_context.diagnostics` in `scan.json`.

```bash
python main.py scan https://example.com \
  --source-root /path/to/app \
  --source-max-files 500 \
  --source-max-tree 400 \
  --source-max-routes 150
```

For untrusted repositories, source scanning is read-only and refuses symlinks, files outside the resolved source root, and files larger than `--source-max-file-bytes`. Skipped file counts are reported in `source_context.diagnostics`.

## LLM Planner

The deterministic planner is the default because it works without credentials:

```bash
python main.py plan outputs/smoke/scan.json --mode heuristic
```

To try the OpenAI-backed planner, set an API key and run:

```bash
export OPENAI_API_KEY=...
python main.py plan outputs/smoke/scan.json --mode llm
```

In LLM mode, Folio first asks the model to rank scanner-tested `candidate_paths` by demo value. The planner then builds a coverage plan from that ranking and selects every validated product workflow needed to cover the discovered functionality. The final plan uses canonical replay actions from those selected paths, inserts reset navigation between independent workflows, and stores coverage details under `planner.coverage`.

By default, `--mode llm` falls back to the heuristic planner if the API key is missing or the model response cannot be used. Use `--no-fallback` when you want failures to stop the workflow.

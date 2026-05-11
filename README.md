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

When probes are enabled, `scan.json` also includes `candidate_paths`: scored, replayable workflows assembled from the discovered state graph. Paths that create or mutate state are ranked above passive navigation because they usually make stronger demos. In LLM mode, a valid `selected_path_id` causes Folio to use the scanner-tested actions from that path while applying the LLM's wording to the plan.

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

By default, `--mode llm` falls back to the heuristic planner if the API key is missing or the model response cannot be used. Use `--no-fallback` when you want failures to stop the workflow.

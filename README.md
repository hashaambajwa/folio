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

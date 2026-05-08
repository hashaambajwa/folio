from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


RENDERER_VERSION = "0.1"
DEFAULT_CRF = 23
DEFAULT_PRESET = "veryfast"


def load_recording(recording_path: str | Path) -> dict:
    return json.loads(Path(recording_path).read_text(encoding="utf-8"))


def render(
    recording_path: str | Path,
    output_video_path: str | Path | None = None,
    report_path: str | Path | None = None,
    ffmpeg_path: str | None = None,
    crf: int = DEFAULT_CRF,
    preset: str = DEFAULT_PRESET,
) -> dict:
    recording_path = Path(recording_path)
    recording = load_recording(recording_path)
    return render_recording(
        recording,
        recording_path=recording_path,
        output_video_path=output_video_path,
        report_path=report_path,
        ffmpeg_path=ffmpeg_path,
        crf=crf,
        preset=preset,
    )


def render_recording(
    recording: dict,
    recording_path: str | Path | None = None,
    output_video_path: str | Path | None = None,
    report_path: str | Path | None = None,
    ffmpeg_path: str | None = None,
    crf: int = DEFAULT_CRF,
    preset: str = DEFAULT_PRESET,
) -> dict:
    recording_path = Path(recording_path) if recording_path else None
    report_path = _default_report_path(recording, recording_path, report_path)
    output_video_path = _default_video_path(recording, recording_path, output_video_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_video_path.parent.mkdir(parents=True, exist_ok=True)

    input_video_path = _input_video_path(recording)
    command = _build_ffmpeg_command(
        ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg",
        input_video_path,
        output_video_path,
        crf=crf,
        preset=preset,
    )

    result = _base_result(
        recording,
        recording_path,
        report_path,
        input_video_path,
        output_video_path,
        command,
    )

    ffmpeg_binary = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg_binary:
        result["failure"] = {
            "error": "MissingDependency",
            "message": "ffmpeg was not found on PATH. Install FFmpeg to render MP4 outputs.",
        }
        _write_report(report_path, result)
        return result

    if recording.get("status") != "completed":
        result["failure"] = {
            "error": "InvalidRecording",
            "message": "Recording status is not completed.",
        }
        _write_report(report_path, result)
        return result

    if not input_video_path or not input_video_path.exists():
        result["failure"] = {
            "error": "MissingInputVideo",
            "message": f"Input video does not exist: {input_video_path}",
        }
        _write_report(report_path, result)
        return result

    command[0] = ffmpeg_binary
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    result["ffmpeg"] = {
        "returncode": completed.returncode,
        "stderr": completed.stderr[-4_000:],
    }

    if completed.returncode != 0:
        result["failure"] = {
            "error": "FFmpegFailed",
            "message": "FFmpeg failed while rendering the MP4.",
        }
        _write_report(report_path, result)
        return result

    result["status"] = "completed"
    result["artifacts"]["video"] = str(output_video_path)
    result["output"] = {
        "size_bytes": output_video_path.stat().st_size,
    }
    _write_report(report_path, result)
    return result


def _base_result(
    recording: dict,
    recording_path: Path | None,
    report_path: Path,
    input_video_path: Path | None,
    output_video_path: Path,
    command: list[str],
) -> dict:
    return {
        "version": RENDERER_VERSION,
        "job_id": recording.get("job_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "source": {
            "recording_json": str(recording_path) if recording_path else recording.get("artifacts", {}).get("recording_json"),
            "input_video": str(input_video_path) if input_video_path else None,
        },
        "artifacts": {
            "render_json": str(report_path),
            "video": None,
        },
        "command": command,
        "ffmpeg": None,
        "failure": None,
    }


def _build_ffmpeg_command(
    ffmpeg_binary: str,
    input_video_path: Path | None,
    output_video_path: Path,
    crf: int,
    preset: str,
) -> list[str]:
    return [
        ffmpeg_binary,
        "-y",
        "-i",
        str(input_video_path),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-movflags",
        "+faststart",
        "-an",
        str(output_video_path),
    ]


def _input_video_path(recording: dict) -> Path | None:
    video = recording.get("artifacts", {}).get("video")
    return Path(video) if video else None


def _default_report_path(
    recording: dict,
    recording_path: Path | None,
    report_path: str | Path | None,
) -> Path:
    if report_path:
        return Path(report_path)

    artifact_path = recording.get("artifacts", {}).get("recording_json")
    if artifact_path:
        return Path(artifact_path).with_name("render.json")

    if recording_path:
        return recording_path.with_name("render.json")

    return Path("outputs") / "render.json"


def _default_video_path(
    recording: dict,
    recording_path: Path | None,
    output_video_path: str | Path | None,
) -> Path:
    if output_video_path:
        return Path(output_video_path)

    artifact_path = recording.get("artifacts", {}).get("recording_json")
    if artifact_path:
        return Path(artifact_path).with_name("final.mp4")

    if recording_path:
        return recording_path.with_name("final.mp4")

    return Path("outputs") / "final.mp4"


def _write_report(report_path: Path, result: dict) -> None:
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

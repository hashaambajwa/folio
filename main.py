from __future__ import annotations

import argparse

from planner import write_plan
from recorder import run_record
from renderer import render
from scanner import DEFAULT_MAX_ACTIONS_PER_STATE, DEFAULT_MAX_STATES, DEFAULT_PROBE_DEPTH, run_scan
from source_context import (
    MAX_COMPONENTS,
    MAX_FILE_BYTES,
    MAX_FILE_CHARS,
    MAX_README_CHARS,
    MAX_README_FILES,
    MAX_ROUTES,
    MAX_SOURCE_FILES,
    MAX_TREE_ENTRIES,
    MAX_UI_STRINGS,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="folio")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="scan a deployed web app")
    scan_parser.add_argument("url", help="URL to scan")
    scan_parser.add_argument("--job-id", help="optional stable output folder name")
    scan_parser.add_argument("--output-root", default="outputs", help="artifact output directory")
    scan_parser.add_argument(
        "--timeout-ms",
        default=30_000,
        type=int,
        help="page navigation timeout in milliseconds",
    )
    scan_parser.add_argument(
        "--probe-depth",
        default=DEFAULT_PROBE_DEPTH,
        type=int,
        help="maximum UI exploration depth after the initial scan",
    )
    scan_parser.add_argument(
        "--max-states",
        default=DEFAULT_MAX_STATES,
        type=int,
        help="maximum discovered UI states to capture",
    )
    scan_parser.add_argument(
        "--max-actions-per-state",
        default=DEFAULT_MAX_ACTIONS_PER_STATE,
        type=int,
        help="maximum probe candidates to try from each state",
    )
    scan_parser.add_argument(
        "--no-probes",
        action="store_true",
        help="capture only the initial page state",
    )
    scan_parser.add_argument(
        "--source-root",
        help="optional local app source directory to summarize for planning",
    )
    scan_parser.add_argument(
        "--source-max-tree",
        default=MAX_TREE_ENTRIES,
        dest="source_max_tree_entries",
        type=int,
        help="maximum source tree paths to keep",
    )
    scan_parser.add_argument(
        "--source-max-files",
        default=MAX_SOURCE_FILES,
        type=int,
        help="maximum source files to inspect",
    )
    scan_parser.add_argument(
        "--source-max-readme-chars",
        default=MAX_README_CHARS,
        type=int,
        help="maximum characters to keep per README",
    )
    scan_parser.add_argument(
        "--source-max-readmes",
        default=MAX_README_FILES,
        dest="source_max_readme_files",
        type=int,
        help="maximum README files to keep",
    )
    scan_parser.add_argument(
        "--source-max-routes",
        default=MAX_ROUTES,
        type=int,
        help="maximum route candidates to keep",
    )
    scan_parser.add_argument(
        "--source-max-components",
        default=MAX_COMPONENTS,
        type=int,
        help="maximum component candidates to keep",
    )
    scan_parser.add_argument(
        "--source-max-ui-strings",
        default=MAX_UI_STRINGS,
        type=int,
        help="maximum UI string candidates to keep",
    )
    scan_parser.add_argument(
        "--source-max-file-chars",
        default=MAX_FILE_CHARS,
        type=int,
        help="maximum characters to inspect per source file",
    )
    scan_parser.add_argument(
        "--source-max-file-bytes",
        default=MAX_FILE_BYTES,
        type=int,
        help="maximum byte size for a source file before it is skipped",
    )
    scan_parser.set_defaults(handler=handle_scan)

    plan_parser = subparsers.add_parser("plan", help="generate a demo plan from scan.json")
    plan_parser.add_argument("scan_json", help="path to a scanner output JSON file")
    plan_parser.add_argument("--output", help="optional plan JSON output path")
    plan_parser.add_argument(
        "--mode",
        choices=["heuristic", "llm"],
        default="heuristic",
        help="planner mode",
    )
    plan_parser.add_argument("--llm-model", help="OpenAI model for --mode llm")
    plan_parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="fail instead of using the heuristic planner if LLM planning fails",
    )
    plan_parser.set_defaults(handler=handle_plan)

    record_parser = subparsers.add_parser("record", help="record a browser walkthrough from plan.json")
    record_parser.add_argument("plan_json", help="path to a planner output JSON file")
    record_parser.add_argument("--output", help="optional recording JSON output path")
    record_parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser while recording",
    )
    record_parser.add_argument(
        "--timeout-ms",
        default=10_000,
        type=int,
        help="action timeout in milliseconds",
    )
    record_parser.add_argument(
        "--slow-mo-ms",
        default=0,
        type=int,
        help="slow down Playwright actions by this many milliseconds",
    )
    record_parser.set_defaults(handler=handle_record)

    render_parser = subparsers.add_parser("render", help="render final MP4 from recording.json")
    render_parser.add_argument("recording_json", help="path to a recorder output JSON file")
    render_parser.add_argument("--output", help="optional MP4 output path")
    render_parser.add_argument("--report", help="optional render JSON report path")
    render_parser.add_argument("--ffmpeg-path", help="explicit path to an ffmpeg binary")
    render_parser.add_argument("--crf", default=23, type=int, help="x264 quality value")
    render_parser.add_argument("--preset", default="veryfast", help="x264 encoding preset")
    render_parser.set_defaults(handler=handle_render)

    return parser


def handle_scan(args: argparse.Namespace) -> int:
    result = run_scan(
        args.url,
        output_root=args.output_root,
        job_id=args.job_id,
        timeout_ms=args.timeout_ms,
        probe_depth=0 if args.no_probes else args.probe_depth,
        max_states=args.max_states,
        max_actions_per_state=args.max_actions_per_state,
        source_root=args.source_root,
        source_max_tree_entries=args.source_max_tree_entries,
        source_max_files=args.source_max_files,
        source_max_readme_files=args.source_max_readme_files,
        source_max_readme_chars=args.source_max_readme_chars,
        source_max_routes=args.source_max_routes,
        source_max_components=args.source_max_components,
        source_max_ui_strings=args.source_max_ui_strings,
        source_max_file_chars=args.source_max_file_chars,
        source_max_file_bytes=args.source_max_file_bytes,
    )

    dom = result["dom"]
    print(f"Scan complete: {result['job_id']}")
    print(f"Title: {result['page']['title']}")
    print(f"Final URL: {result['page']['final_url']}")
    print(f"Screenshot: {result['artifacts']['screenshot']}")
    print(f"Scan JSON: {result['artifacts']['scan_json']}")
    print(f"States: {len(result.get('states', []))}")
    print(f"Transitions: {len(result.get('transitions', []))}")
    print(f"Candidate paths: {len(result.get('candidate_paths', []))}")
    source_context = result.get("source_context")
    if source_context:
        summary = source_context.get("summary", {})
        diagnostics = source_context.get("diagnostics", {})
        skipped_count = sum(
            diagnostics.get(key, 0)
            for key in (
                "skipped_symlinks",
                "skipped_outside_root",
                "skipped_large_files",
                "skipped_unreadable_files",
                "skipped_policy_files",
            )
        )
        print(
            "Source context: "
            f"{source_context.get('status')} "
            f"({summary.get('source_files_inspected', 0)}/{summary.get('source_file_count', 0)} source files inspected, "
            f"{summary.get('route_count', 0)} routes, "
            f"{summary.get('component_count', 0)} components, "
            f"{skipped_count} skipped, "
            f"truncated={diagnostics.get('truncated', False)})"
        )
    print(
        "Elements: "
        f"{len(dom['buttons'])} buttons, "
        f"{len(dom['inputs'])} inputs, "
        f"{len(dom['links'])} links, "
        f"{len(dom['forms'])} forms"
    )
    return 0


def handle_plan(args: argparse.Namespace) -> int:
    try:
        plan = write_plan(
            args.scan_json,
            output_path=args.output,
            mode=args.mode,
            llm_model=args.llm_model,
            fallback_to_heuristic=not args.no_fallback,
        )
    except RuntimeError as exc:
        print(f"Plan failed: {exc}")
        return 1

    scenes = plan["scenes"]
    print(f"Plan complete: {plan['job_id']}")
    print(f"Mode: {plan['planner']['mode']}")
    if plan["planner"].get("model"):
        print(f"Model: {plan['planner']['model']}")
    if plan["planner"].get("fallback_used"):
        print(f"Fallback: {plan['planner'].get('error')}")
    print(f"Plan JSON: {plan['artifacts']['plan_json']}")
    print(f"Scenes: {len(scenes)}")
    for scene in scenes:
        print(f"- {scene['scene_id']}: {scene['title']}")
    return 0


def handle_record(args: argparse.Namespace) -> int:
    result = run_record(
        args.plan_json,
        output_path=args.output,
        headless=not args.headed,
        action_timeout_ms=args.timeout_ms,
        slow_mo_ms=args.slow_mo_ms,
    )

    successful_actions = [action for action in result["actions"] if action["status"] == "success"]
    print(f"Recording {result['status']}: {result['job_id']}")
    print(f"Recording JSON: {result['artifacts']['recording_json']}")
    print(f"Video: {result['artifacts']['video']}")
    print(f"Actions: {len(successful_actions)}/{len(result['actions'])} succeeded")
    if result["failure"]:
        print(f"Failure: {result['failure'].get('message')}")
        return 1
    return 0


def handle_render(args: argparse.Namespace) -> int:
    result = render(
        args.recording_json,
        output_video_path=args.output,
        report_path=args.report,
        ffmpeg_path=args.ffmpeg_path,
        crf=args.crf,
        preset=args.preset,
    )

    print(f"Render {result['status']}: {result['job_id']}")
    print(f"Render JSON: {result['artifacts']['render_json']}")
    print(f"Video: {result['artifacts']['video']}")
    if result["failure"]:
        print(f"Failure: {result['failure']['message']}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

import argparse

from planner import write_plan
from recorder import run_record
from renderer import render
from scanner import run_scan


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
    scan_parser.set_defaults(handler=handle_scan)

    plan_parser = subparsers.add_parser("plan", help="generate a demo plan from scan.json")
    plan_parser.add_argument("scan_json", help="path to a scanner output JSON file")
    plan_parser.add_argument("--output", help="optional plan JSON output path")
    plan_parser.add_argument(
        "--mode",
        choices=["heuristic"],
        default="heuristic",
        help="planner mode",
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
    )

    dom = result["dom"]
    print(f"Scan complete: {result['job_id']}")
    print(f"Title: {result['page']['title']}")
    print(f"Final URL: {result['page']['final_url']}")
    print(f"Screenshot: {result['artifacts']['screenshot']}")
    print(f"Scan JSON: {result['artifacts']['scan_json']}")
    print(
        "Elements: "
        f"{len(dom['buttons'])} buttons, "
        f"{len(dom['inputs'])} inputs, "
        f"{len(dom['links'])} links, "
        f"{len(dom['forms'])} forms"
    )
    return 0


def handle_plan(args: argparse.Namespace) -> int:
    plan = write_plan(args.scan_json, output_path=args.output, mode=args.mode)

    scenes = plan["scenes"]
    print(f"Plan complete: {plan['job_id']}")
    print(f"Mode: {plan['planner']['mode']}")
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

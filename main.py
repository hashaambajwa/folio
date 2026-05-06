import argparse

from planner import write_plan
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

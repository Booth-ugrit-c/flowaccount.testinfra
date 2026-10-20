import argparse
import sys
from pathlib import Path

from trials.dispatch.clock import Clock
from trials.dispatch.collect import collect
from trials.dispatch.gh import GhClient, GhError
from trials.dispatch.replay import build_plan, format_plan, run_replay
from trials.dispatch.specs import SPLITS, SpecError, load_calibration, parse_lane_map


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m trials.dispatch")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="dispatch gates at their recorded arrival offsets")
    replay.add_argument("--calibration", required=True, type=Path)
    replay.add_argument("--split", required=True, choices=SPLITS)
    replay.add_argument("--labels", required=True, help="lane=label,lane=label")
    replay.add_argument("--factor", required=True, help="lane=cpu-seconds per recorded second")
    replay.add_argument("--threads", required=True, help="lane=threads")
    replay.add_argument("--memory", required=True, help="lane=mib")
    replay.add_argument("--compress", required=True, type=float, help="arrival time compression, 1 = real time")
    replay.add_argument("--repo", required=True, help="owner/name")
    replay.add_argument("--ref", required=True)
    replay.add_argument("--trial-id", required=True)
    replay.add_argument("--out", required=True, type=Path)
    replay.add_argument("--limit-gates", type=int)
    replay.add_argument("--dry-run", action="store_true")
    replay.add_argument("--discovery-timeout-s", type=float, default=120.0)

    collect_cmd = sub.add_parser("collect", help="gather every dispatched job's timeline and check completeness")
    collect_cmd.add_argument("--out", required=True, type=Path)
    collect_cmd.add_argument("--poll-s", type=float, default=15.0)
    collect_cmd.add_argument("--timeout-s", type=float, default=21600.0)
    return parser


def _replay(args, gh: GhClient, clock: Clock) -> int:
    plan = build_plan(
        load_calibration(args.calibration),
        split=args.split,
        limit=args.limit_gates,
        labels=parse_lane_map(args.labels, str, "--labels"),
        factors=parse_lane_map(args.factor, float, "--factor"),
        threads=parse_lane_map(args.threads, int, "--threads"),
        memory=parse_lane_map(args.memory, int, "--memory"),
        compress=args.compress,
        repo=args.repo,
        ref=args.ref,
        trial_id=args.trial_id,
    )
    print(format_plan(plan))
    if args.dry_run:
        return 0
    ok = run_replay(plan, gh, clock, args.out, discovery_timeout_s=args.discovery_timeout_s)
    print(f"dispatch {'complete' if ok else 'FAILED'}: state in {args.out / 'dispatch.json'}")
    return 0 if ok else 1


def _collect(args, gh: GhClient, clock: Clock) -> int:
    ok, lines = collect(args.out, gh, clock, poll_s=args.poll_s, timeout_s=args.timeout_s)
    print("\n".join(lines))
    print("collect: complete" if ok else "collect: INCOMPLETE")
    return 0 if ok else 1


def main(argv: list[str] | None = None, gh: GhClient | None = None, clock: Clock | None = None) -> int:
    args = build_parser().parse_args(argv)
    clock = clock or Clock()
    gh = gh or GhClient(clock=clock)
    try:
        return _replay(args, gh, clock) if args.command == "replay" else _collect(args, gh, clock)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

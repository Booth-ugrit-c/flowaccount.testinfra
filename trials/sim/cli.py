import argparse
import json
from pathlib import Path

from trials.sim.core import Gate, UnitWL, load_e2e, load_unit, run_ids, simulate, timeline

OPTIONAL = {
    "w": float, "accounts": int, "unit_cpu": float, "pool": float, "pod_overhead": float,
    "sched_latency": float, "slice_T": float, "release_lag": float,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m trials.sim")
    sub = parser.add_subparsers(dest="command", required=True)

    runs = sub.add_parser("runs", help="list the run ids in a model file")
    runs.add_argument("model", type=Path)

    run = sub.add_parser("run", help="simulate one arm and print the summary as JSON")
    run.add_argument("--arm", required=True, choices=["A", "B", "C"])
    run.add_argument("--unit-model", type=Path)
    run.add_argument("--unit-run")
    run.add_argument("--e2e-model", type=Path)
    run.add_argument("--e2e-run")
    run.add_argument("--build-lanes", type=int, default=1)
    run.add_argument("--offsets", default="0", help="comma-separated gate arrival offsets in seconds")
    run.add_argument("--a-conc", help="three comma-separated step concurrencies for arm A")
    run.add_argument("--timeline-step", type=float, help="add per-step averages of this many seconds")
    for name, kind in OPTIONAL.items():
        run.add_argument("--" + name.replace("_", "-"), dest=name, type=kind)
    return parser


def _workloads(args, parser):
    for model, run_id, what in ((args.unit_model, args.unit_run, "unit"), (args.e2e_model, args.e2e_run, "e2e")):
        if (model is None) != (run_id is None):
            parser.error(f"--{what}-model and --{what}-run go together")
    unit = load_unit(args.unit_model, args.unit_run, args.build_lanes) if args.unit_model else UnitWL()
    e2e = load_e2e(args.e2e_model, args.e2e_run) if args.e2e_model else None
    return unit, e2e


def _run(args, parser) -> int:
    unit, e2e = _workloads(args, parser)
    offsets = [float(x) for x in args.offsets.split(",")]
    kw = {k: getattr(args, k) for k in OPTIONAL if getattr(args, k) is not None}
    if args.a_conc:
        kw["a_conc"] = tuple(int(x) for x in args.a_conc.split(","))
    res = simulate(args.arm, [Gate(o, unit, e2e) for o in offsets], keep_rows=args.timeline_step is not None, **kw)
    if args.timeline_step is not None:
        res["timeline"] = timeline(res, args.timeline_step)
        del res["rows"]
    print(json.dumps(res, indent=1))
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "runs":
        print("\n".join(run_ids(args.model)))
        return 0
    return _run(args, parser)


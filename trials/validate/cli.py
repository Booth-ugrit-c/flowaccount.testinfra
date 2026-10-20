import argparse
import json
import sys
from pathlib import Path

from trials.validate.stats import fit_factors, noise_floor, paired, real_jobs, replay_jobs, verdict


def _factors(text: str) -> dict[str, float]:
    out = {}
    for part in text.split(","):
        lane, _, value = part.partition("=")
        out[lane.strip()] = float(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trials.validate")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fit", "verdict"):
        p = sub.add_parser(name)
        p.add_argument("--windows", required=True, nargs="+", type=Path, help="window calibration files")
        p.add_argument("--collected", required=True, nargs="+", type=Path, help="collect output directories")
        p.add_argument("--compress", required=True, type=float)
    sub.choices["fit"].add_argument("--factor", required=True, type=_factors, help="factors used for the replay, lane=value,...")
    sub.choices["verdict"].add_argument("--split", default="holdout")
    aa = sub.add_parser("aa", help="noise floor across repeated runs of the same windows")
    aa.add_argument("--windows", required=True, nargs="+", type=Path)
    aa.add_argument("--runs", required=True, nargs="+", type=Path, help="one collect directory per run")
    aa.add_argument("--compress", required=True, type=float)
    args = parser.parse_args(argv)

    if args.command == "aa":
        try:
            real = real_jobs(args.windows)
            rows = noise_floor([paired(real, replay_jobs([run], args.compress)) for run in args.runs])
        except ValueError as exc:
            print(f"validate: {exc}", file=sys.stderr)
            return 2
        for row in rows:
            print(f"{row['lane']:<12} {row['metric']:<5} runs={row['runs']} mean_median={row['mean_median']:.1f} cv={row['cv'] * 100:.2f}%")
        return 0

    try:
        pairs = paired(real_jobs(args.windows), replay_jobs(args.collected, args.compress))
        if args.command == "fit":
            fitted = fit_factors(pairs, args.factor)
            print(",".join(f"{lane}={value:.4f}" for lane, value in sorted(fitted.items())))
            return 0
        rows = verdict(pairs, args.split)
    except ValueError as exc:
        print(f"validate: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print(
            f"{row['lane']:<12} {row['metric']:<5} n={row['n']:<3} real IQR {row['real_q1']:.0f}-{row['real_q3']:.0f} "
            f"(median {row['real_median']:.0f}) replay median {row['replay_median']:.0f} {'PASS' if row['pass'] else 'FAIL'}"
        )
    print(json.dumps({"pass": all(r["pass"] for r in rows)}))
    return 0 if all(r["pass"] for r in rows) else 1

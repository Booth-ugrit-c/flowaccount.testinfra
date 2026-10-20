import argparse
import json
import math
import sys
from pathlib import Path

from trials.compare.stats import kinds_of, run_metrics, span, summarize, usage
from trials.validate.stats import replay_jobs


def _run(text: str) -> tuple[str, Path]:
    design, sep, path = text.partition("=")
    if not sep or not design or not path:
        raise argparse.ArgumentTypeError(f"expected design=collect-dir, got {text!r}")
    return design, Path(path)


def _fmt(value: float) -> str:
    return "n/a" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value:.1f}"


def render(rows: list[dict], baseline: str) -> str:
    lines = [
        f"| metric | design | n | mean | sd | cv | delta vs {baseline} | minimum detectable | detectable |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        delta = "" if "delta" not in r else f"{_fmt(r['delta'])} ({r['delta_pct'] * 100:+.1f}%)"
        lines.append(
            f"| {r['key']} | {r['design']} | {r['n']} | {_fmt(r['mean'])} | {_fmt(r['sd'])} | "
            f"{'n/a' if math.isnan(r['cv']) else f'{r['cv'] * 100:.1f}%'} | {delta} | "
            f"{_fmt(r.get('mde')) if 'mde' in r else ''} | {'' if 'detectable' not in r else ('yes' if r['detectable'] else 'no')} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trials.compare")
    parser.add_argument("--window", required=True, type=Path, help="window calibration file every run replayed")
    parser.add_argument("--compress", required=True, type=float)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--run", required=True, action="append", type=_run, help="design=collect-dir, repeat per run")
    parser.add_argument("--json", type=Path, help="write per-run metrics and the summary here")
    args = parser.parse_args(argv)
    try:
        jobs = kinds_of(args.window)
        per_design: dict[str, list[dict]] = {}
        runs = []
        for design, directory in args.run:
            metrics = run_metrics(jobs, replay_jobs([directory], args.compress))
            samples = sorted(directory.parent.glob(f"{directory.name}-samples-*.csv"))
            if samples:
                metrics.update({f"host/{k}": v for k, v in usage(samples, *span(directory)).items()})
            per_design.setdefault(design, []).append(metrics)
            runs.append({"design": design, "run": directory.name, "metrics": metrics})
        rows = summarize(per_design, args.baseline)
    except (OSError, ValueError, KeyError) as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 2
    print(render(rows, args.baseline))
    if args.json:
        args.json.write_text(json.dumps({"runs": runs, "summary": rows}, indent=2, default=str) + "\n", encoding="utf-8")
    return 0

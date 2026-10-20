import argparse
import json
import sys
from pathlib import Path

from trials.windows.select import choose, jobs_of, window_calibration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trials.windows")
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--length-s", type=float, default=7200.0)
    parser.add_argument("--target", type=int, default=30, help="jobs wanted per lane and split")
    parser.add_argument("--max-windows", type=int, default=8)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
    jobs = jobs_of(calibration)
    if not jobs:
        print("windows: no usable jobs in the calibration file", file=sys.stderr)
        return 2
    windows = choose(jobs, args.length_s, args.target, args.max_windows)
    if not windows:
        print("windows: no window starts on an idle queue", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    manifest, totals = [], {}
    for index, window in enumerate(windows, 1):
        name = f"w{index:02d}"
        (args.out / f"{name}.json").write_text(json.dumps(window_calibration(calibration, window, name)), encoding="utf-8")
        counts = {f"{lane}/{split}": n for (lane, split), n in sorted(window.counts().items())}
        for key, n in counts.items():
            totals[key] = totals.get(key, 0) + n
        manifest.append({"window": name, "start_offset_s": window.start, "length_s": window.length, "busy_s": round(window.busy), "jobs": counts})
        print(f"{name} busy_s={round(window.busy)} jobs={counts}")
    (args.out / "windows.json").write_text(json.dumps({"windows": manifest, "totals": totals}, indent=1), encoding="utf-8")
    print(f"windows: {len(windows)} chosen, totals {totals}")
    short = [k for k, n in totals.items() if n < args.target]
    if short:
        print(f"windows: below target {args.target}: {short}", file=sys.stderr)
        return 1
    return 0

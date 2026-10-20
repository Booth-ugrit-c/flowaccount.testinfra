import argparse
import sys
from pathlib import Path


def largest_gap(csv_text: str) -> tuple[float, int]:
    stamps = [float(line.split(",", 1)[0]) for line in csv_text.splitlines()[1:] if line.strip()]
    if len(stamps) < 2:
        raise ValueError("fewer than two samples")
    return max(b - a for a, b in zip(stamps, stamps[1:])), len(stamps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trials.sampler.gaps")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    limit, bad = 3 * args.interval, 0
    for path in args.files:
        try:
            gap, n = largest_gap(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path.name}: {exc}")
            bad += 1
            continue
        ok = gap <= limit
        bad += not ok
        print(f"{path.name}: samples={n} largest_gap_s={gap:.3f} limit_s={limit:g} {'ok' if ok else 'GAP'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

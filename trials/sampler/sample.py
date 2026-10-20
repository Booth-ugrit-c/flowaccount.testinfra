import argparse
import sys
import time
from pathlib import Path


def cpu_times(stat_text: str) -> tuple[int, int]:
    fields = [int(x) for x in stat_text.splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields[:8]), idle


def mem_used_mib(meminfo_text: str) -> tuple[int, int]:
    values = {}
    for line in meminfo_text.splitlines():
        key, _, rest = line.partition(":")
        values[key] = int(rest.split()[0])
    total = values["MemTotal"]
    return (total - values["MemAvailable"]) // 1024, total // 1024


def busy_fraction(before: tuple[int, int], after: tuple[int, int]) -> float:
    total = after[0] - before[0]
    idle = after[1] - before[1]
    return 0.0 if total <= 0 else round((total - idle) / total, 4)


def run(out: Path, interval: float, proc: Path = Path("/proc"), clock=time.time, sleep=time.sleep, limit: int | None = None) -> None:
    previous = cpu_times((proc / "stat").read_text())
    count = 0
    with out.open("a", encoding="utf-8") as handle:
        if handle.tell() == 0:
            handle.write("epoch,cpu_busy,mem_used_mib,mem_total_mib\n")
        while limit is None or count < limit:
            sleep(interval)
            current = cpu_times((proc / "stat").read_text())
            used, total = mem_used_mib((proc / "meminfo").read_text())
            handle.write(f"{clock():.3f},{busy_fraction(previous, current)},{used},{total}\n")
            handle.flush()
            previous = current
            count += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trials.sampler.sample")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    run(args.out, args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())

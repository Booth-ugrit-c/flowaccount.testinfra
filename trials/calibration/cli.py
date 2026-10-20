from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .build import assign_splits, build, split_rule
from .collect import collect
from .config import Config, load_config
from .errors import CalibrationError, DataError, ZeroMatchError
from .ghapi import GhClient
from .summary import render


def write_all(out: Path, files: dict[str, object]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    staged = {}
    for name, data in files.items():
        staged[name] = out / f"{name}.partial"
        staged[name].write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for name, partial in staged.items():
        os.replace(partial, out / name)


def extract(config_path: Path, out: Path, client: GhClient | None = None) -> str:
    cfg = load_config(config_path)
    client = client or GhClient()
    observed, seen = collect(client, cfg)
    result = build(observed, cfg)
    if not result.today["gates"]:
        raise ZeroMatchError(
            f"filter matched zero on-prem lane jobs among {seen['jobs']} jobs in {seen['runs']} runs"
        )
    write_all(out, {"today.json": result.today, "id-map.json": result.id_map})
    api = {**seen, "calls": client.calls, "rate_probes": client.rate_probes, "rate_waits": client.rate_waits}
    return render(result.today, [lane.name for lane in cfg.lanes], result.warnings, result.off_runner_jobs, api)


def resplit(config_path: Path, source: Path, out: Path) -> str:
    cfg = load_config(config_path)
    try:
        today = json.loads(Path(source).read_text(encoding="utf-8"))
        gates = today["gates"]
    except (OSError, ValueError, KeyError, TypeError):
        raise DataError("calibration file is missing or not a calibration file") from None
    check_lanes(today, cfg)
    warnings = assign_splits(gates, [lane.name for lane in cfg.lanes], cfg.split_until)
    today["split_rule"] = split_rule(cfg.split_until)
    write_all(out, {"today.json": today})
    return render(today, [lane.name for lane in cfg.lanes], warnings)


def check_lanes(today: dict, cfg: Config) -> None:
    known = {lane.name for lane in cfg.lanes}
    if not today["gates"]:
        raise ZeroMatchError("calibration file holds no gates")
    for gate in today["gates"]:
        for job in gate["jobs"]:
            if job["lane"] not in known:
                raise DataError(f"calibration file has a job in lane {job['lane']!r} unknown to the config")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trials.calibration")
    sub = parser.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract")
    ext.add_argument("--config", required=True, type=Path)
    ext.add_argument("--out", required=True, type=Path)
    res = sub.add_parser("resplit")
    res.add_argument("--config", required=True, type=Path)
    res.add_argument("--in", dest="source", required=True, type=Path)
    res.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            print(extract(args.config, args.out))
        else:
            print(resplit(args.config, args.source, args.out))
    except CalibrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0

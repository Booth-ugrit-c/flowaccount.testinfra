from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .build import build
from .collect import collect
from .config import load_config
from .errors import CalibrationError, ZeroMatchError
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
    return render(result, [lane.name for lane in cfg.lanes], api)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trials.calibration")
    sub = parser.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract")
    ext.add_argument("--config", required=True, type=Path)
    ext.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(extract(args.config, args.out))
    except CalibrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0

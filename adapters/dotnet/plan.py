from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from adapters.dotnet import affected

LANES = ("unit", "integration")
INTEGRATION_MARKER = "IntegrationTests"


def lane_tests(tests: Sequence[str], lane: str) -> list:
    wanted = lane == "integration"
    return [path for path in tests if (INTEGRATION_MARKER in path) == wanted]


def unit_extras(build: Sequence[str]) -> list:
    return [str(path) for path in build if INTEGRATION_MARKER not in str(path)]


def read_graph(root: str, cwd: Path) -> dict:
    base = cwd / root
    files = {
        path.relative_to(cwd).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*.csproj"))
        if not {"bin", "obj"} & set(path.relative_to(base).parts)
    }
    if not files:
        raise affected.GraphError(f"no .csproj found under {root}")
    return affected.build_graph(files)


def build_plan(graph: dict, tests: Sequence[str]) -> list:
    for path in tests:
        if path not in graph:
            raise affected.GraphError(f"{path} is not a project under the root")
    needed, pending = set(), list(tests)
    while pending:
        path = pending.pop()
        if path not in needed:
            needed.add(path)
            pending.extend(graph[path].references)
    return [path for path in affected.topological_order(graph) if path in needed]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m adapters.dotnet.plan")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--affected")
    source.add_argument("--all", action="store_true")
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--emit", required=True, choices=("build", "test"))
    parser.add_argument("--root", default=affected.DEFAULT_ROOT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.all:
            graph = read_graph(args.root, Path.cwd())
            order = affected.topological_order(graph)
            selection = {"build": order, "test": [path for path in order if graph[path].is_test]}
        else:
            selection = json.loads(Path(args.affected).read_text(encoding="utf-8"))
        tests = lane_tests([str(path) for path in selection["test"]], args.lane)
        if args.emit == "build":
            seeds = tests + (unit_extras(selection["build"]) if args.lane == "unit" else [])
            result = build_plan(read_graph(args.root, Path.cwd()), seeds)
        else:
            result = tests
    except (OSError, ValueError, KeyError, TypeError, affected.GraphError) as exc:
        print(f"dotnet.plan: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(("\n".join(result) + "\n" if result else "").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

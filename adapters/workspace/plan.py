from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from adapters.workspace import affected

LANES = ("unit",)


def load(selection, every=False) -> tuple:
    projects = selection["projects"]
    if not isinstance(projects, dict):
        raise affected.GraphError("projects is not an object")
    chosen = [label for label, entry in projects.items() if entry["test"] is True] if every else selection["test"]
    tests = [str(path) for path in chosen]
    if not tests:
        raise affected.GraphError("the selection has no test project; an empty run would pass with zero tests")
    graph = {}
    for label, entry in projects.items():
        dependencies = [str(d) for d in entry["dependencies"]]
        for dependency in dependencies:
            if dependency not in projects:
                raise affected.GraphError(f"{label} depends on {dependency}, which is not in projects")
        graph[label] = affected.Project(str(label), str(label), tuple(dependencies), entry["test"] is True)
    for path in tests:
        if path not in graph:
            raise affected.GraphError(f"{path} is not a project in the selection")
        if not graph[path].has_test:
            raise affected.GraphError(f"{path} has no {affected.TEST_TARGET} target")
    return graph, tests, {label: str(entry["name"]) for label, entry in projects.items()}


def build_plan(graph: dict, tests: Sequence[str]) -> list:
    needed, pending = set(), list(tests)
    while pending:
        path = pending.pop()
        if path not in needed:
            needed.add(path)
            pending.extend(graph[path].dependencies)
    return affected.order(graph, needed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m adapters.workspace.plan")
    parser.add_argument("--affected", required=True)
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--emit", required=True, choices=("build", "test"))
    parser.add_argument("--names", action="store_true", help="print Nx project names instead of project roots")
    parser.add_argument("--all", action="store_true", help="plan every test project in the selection's graph")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        selection = json.loads(Path(args.affected).read_text(encoding="utf-8"))
        graph, tests, names = load(selection, args.all)
        result = build_plan(graph, tests) if args.emit == "build" else tests
        if args.names:
            result = [names[path] for path in result]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, affected.GraphError) as exc:
        print(f"workspace.plan: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(("\n".join(result) + "\n" if result else "").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

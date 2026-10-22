from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

KEY_UNSAFE = re.compile(rb"[^A-Za-z0-9._-]")
DIGITS = re.compile(r"[0-9]+")


def count_key(project: str) -> str:
    return KEY_UNSAFE.sub(b"_", project.encode("utf-8")).decode("ascii")


def read_projects(path: Path) -> list[str]:
    projects: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        project = line.strip()
        if project and project not in projects:
            projects.append(project)
    return projects


def read_count(counts: Path, project: str) -> int | None:
    try:
        text = (counts / f"{count_key(project)}.count").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if DIGITS.fullmatch(text) else None


def check(projects: list[str], counts: Path) -> tuple[int, list[str]]:
    if not projects:
        return 0, ["0 projects selected"]
    lines: list[str] = []
    failed = False
    owners: dict[str, str] = {}
    for project in projects:
        key = count_key(project)
        if key in owners:
            lines.append(f"::error title=zero tests::{project} and {owners[key]} share the count key {key}")
            failed = True
        owners.setdefault(key, project)
    for project in projects:
        count = read_count(counts, project)
        if count is None:
            lines.append(f"::error title=zero tests::no test count for {project}")
            failed = True
        elif count == 0:
            lines.append(f"::error title=zero tests::{project} ran 0 tests")
            failed = True
        else:
            lines.append(f"total={count} {project}")
    lines.append(f"{len(projects)} projects selected")
    return (1 if failed else 0), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.zero_tests")
    parser.add_argument("--list", required=True, type=Path)
    parser.add_argument("--counts", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        projects = read_projects(args.list)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"zero_tests: cannot read the project list: {exc}", file=sys.stderr)
        return 2
    code, lines = check(projects, args.counts)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json
import sys
from pathlib import Path

EXECUTED_FIELDS = ("numPassedTests", "numFailedTests")


class JestJsonError(Exception):
    pass


def _refuse_constant(name: str) -> None:
    raise JestJsonError(f"{name} is not valid JSON")


def _whole(report: dict, name: str) -> int:
    value = report.get(name)
    if type(value) is not int or value < 0:
        raise JestJsonError(f"{name} is not a whole number")
    return value


def parse_executed(data: bytes) -> int:
    try:
        report = json.loads(data.decode("utf-8"), parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise JestJsonError(f"malformed JSON: {exc}") from None
    if not isinstance(report, dict):
        raise JestJsonError("the report is not a JSON object")
    return sum(_whole(report, name) for name in EXECUTED_FIELDS)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: jest_counts.py JEST_JSON", file=sys.stderr)
        return 2
    try:
        count = parse_executed(Path(argv[0]).read_bytes())
    except (OSError, JestJsonError) as exc:
        print(f"jest_counts: no count: {exc}", file=sys.stderr)
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

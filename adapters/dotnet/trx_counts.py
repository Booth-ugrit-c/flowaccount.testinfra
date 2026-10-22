from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.parsers import expat

TRX_NS = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"
COUNTERS_PATH = [f"{TRX_NS} TestRun", f"{TRX_NS} ResultSummary", f"{TRX_NS} Counters"]
DIGITS = re.compile(r"[0-9]+")


class TrxError(Exception):
    pass


def _refuse_doctype(*_args) -> None:
    raise TrxError("a DTD is not allowed in a TRX file")


def parse_total(data: bytes) -> int:
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartDoctypeDeclHandler = _refuse_doctype
    stack: list[str] = []
    totals: list[str] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        stack.append(name)
        if stack == COUNTERS_PATH:
            totals.append(attrs.get("total", ""))

    def end(_name: str) -> None:
        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise TrxError(f"malformed XML: {exc}") from None
    if len(totals) != 1:
        raise TrxError(f"expected one ResultSummary/Counters, found {len(totals)}")
    if not DIGITS.fullmatch(totals[0]):
        raise TrxError("Counters/@total is not a whole number")
    return int(totals[0])


def total(path: Path) -> int | None:
    try:
        return parse_total(path.read_bytes())
    except (OSError, TrxError):
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: trx_counts.py TRX_FILE", file=sys.stderr)
        return 2
    try:
        count = parse_total(Path(argv[0]).read_bytes())
    except (OSError, TrxError) as exc:
        print(f"trx_counts: no count: {exc}", file=sys.stderr)
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

from __future__ import annotations

import argparse
import os
import re
import sys

PASS = "pass"
RED_TEST = "red [test]"
RED_INFRA_ONCE = "red [infra failed 1x]"
RED_INFRA_TWICE = "red [infra failed 2x]"
VERDICTS = ("pass", "test", "infra")
SEVERITY = (PASS, RED_INFRA_ONCE, RED_INFRA_TWICE, RED_TEST)
LANE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
VERDICT_TEXT = re.compile(r"^[A-Za-z0-9_-]*$")


def parse_lane(raw):
    name, separator, verdicts = raw.partition("=")
    parts = verdicts.split(",")
    if not separator or not LANE_NAME.match(name) or len(parts) > 2:
        raise ValueError(f"lane must look like NAME=ATTEMPT[,RETRY], got {raw!r}")
    if not all(VERDICT_TEXT.match(part) for part in parts):
        raise ValueError(f"lane {name}: verdicts must be plain words")
    return name, parts[0], parts[1] if len(parts) == 2 else ""


def lane_result(attempt, retry):
    if attempt == "pass":
        return PASS
    if attempt != "infra":
        return RED_TEST
    if retry in ("", "skipped"):
        return RED_INFRA_ONCE
    if retry == "pass":
        return PASS
    if retry == "infra":
        return RED_INFRA_TWICE
    return RED_TEST


def overall(lanes):
    if not lanes:
        return RED_TEST
    return max((lane_result(attempt, retry) for _, attempt, retry in lanes), key=SEVERITY.index)


def shown(verdict, allow_empty):
    if verdict in VERDICTS:
        return verdict
    return "none" if allow_empty and verdict in ("", "skipped") else "unknown"


def summary(lanes, verdict):
    rows = [
        f"| {name} | {shown(attempt, False)} | {shown(retry, True)} | {lane_result(attempt, retry)} |"
        for name, attempt, retry in lanes
    ]
    header = ["## sim-gate", "", f"Result: **{verdict}**", "", "| lane | attempt | cloud retry | lane result |", "|---|---|---|---|"]
    return "\n".join(header + rows) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(prog="gate")
    parser.add_argument("--lane", action="append", default=[], type=parse_lane)
    args = parser.parse_args(argv)
    if not args.lane:
        parser.error("at least one --lane is required")
    verdict = overall(args.lane)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(summary(args.lane, verdict))
    print(f"gate: {verdict}")
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

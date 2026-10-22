from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from core.classify import TRIPWIRE_REASON

PASS = "pass"
RED_TEST = "red [test]"
RED_INFRA_ONCE = "red [infra failed 1x]"
RED_INFRA_TWICE = "red [infra failed 2x]"
NOT_RUN = "not run"
BUILD = "build"
VERDICTS = ("pass", "test", "infra")
EMPTY = ("", "skipped")
REASON_LABEL = "red [infra: {}]"
SEVERITY = (NOT_RUN, PASS, RED_INFRA_ONCE, RED_INFRA_TWICE, "reason", RED_TEST)
LANE_NAME = re.compile(r"[A-Za-z0-9._-]{1,32}")
VERDICT_TEXT = re.compile(r"[A-Za-z0-9_-]*")
REASON_TEXT = re.compile(r"[A-Za-z0-9 _-]{0,40}")
COUNT = re.compile(r"[0-9]{1,9}")
SCHEMA = 1
MAX_LANES = 16
MAX_STATE_BYTES = 65536
SHA = r"[0-9a-f]{40}"
RUN_ID = r"[1-9][0-9]{0,19}"
STACK = r"[a-z0-9-]{1,32}"
ATTEMPT = r"[1-9][0-9]{0,3}"
INPUT_FIELDS = {
    "pr_number": r"([1-9][0-9]{0,9})?",
    "pr_branch": r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}",
    "pr_sha": SHA,
    "pr_author": r"([A-Za-z0-9-]{1,39}(\[bot\])?)?",
    "base_sha": SHA,
    "cache": r"use|off",
    "fault": r"[a-z0-9_]{0,32}",
}


class Refused(ValueError):
    pass


def parse_lane(raw):
    name, separator, verdict = raw.partition("=")
    if not separator or not LANE_NAME.fullmatch(name) or not VERDICT_TEXT.fullmatch(verdict):
        raise ValueError(f"lane must look like NAME=VERDICT with one plain word, got {raw!r}")
    return name, verdict


def parse_reason(raw):
    name, separator, reason = raw.partition("=")
    if not separator or not LANE_NAME.fullmatch(name) or not REASON_TEXT.fullmatch(reason):
        raise ValueError(f"reason must look like NAME=TEXT with letters, digits, space, dash or underscore, got {raw!r}")
    return name, reason.strip()


def parse_executed(raw):
    name, separator, count = raw.partition("=")
    if not separator or not LANE_NAME.fullmatch(name) or not (count == "" or COUNT.fullmatch(count)):
        raise ValueError(f"executed must look like NAME=DIGITS or NAME=, got {raw!r}")
    return name, int(count) if count else None


def parse_input(raw):
    key, separator, value = raw.partition("=")
    if not separator or key not in INPUT_FIELDS:
        raise ValueError(f"input must be one of {sorted(INPUT_FIELDS)} as KEY=VALUE, got {raw!r}")
    return key, value


def verdicts(lanes):
    given = dict(lanes)
    upstream_failed = BUILD in given and given[BUILD] != PASS
    result = []
    for name, verdict in lanes:
        if verdict in VERDICTS:
            result.append((name, verdict))
        elif verdict in EMPTY and upstream_failed and name != BUILD:
            result.append((name, "none"))
        else:
            result.append((name, "test"))
    return result


def lane_label(verdict, reason="", retest=False):
    if verdict == "none":
        return NOT_RUN
    if verdict == "test":
        return RED_TEST
    if reason == TRIPWIRE_REASON or (reason and verdict == "infra"):
        return REASON_LABEL.format(reason)
    if verdict == "infra":
        return RED_INFRA_TWICE if retest else RED_INFRA_ONCE
    return PASS


def severity(label):
    return SEVERITY.index("reason" if label.startswith("red [infra: ") else label)


def overall(lanes, reasons=None, retest=False):
    reasons = reasons or {}
    labels = [lane_label(verdict, reasons.get(name, ""), retest) for name, verdict in verdicts(lanes)]
    worst = max(labels, key=severity, default=NOT_RUN)
    return RED_TEST if worst == NOT_RUN else worst


def retest_lanes(lanes):
    judged = verdicts(lanes)
    infra = [name for name, verdict in judged if verdict == "infra"]
    upstream = BUILD in infra
    return [name for name, verdict in judged if verdict == "infra" or (upstream and verdict == "none")]


def summary(lanes, verdict, reasons=None, executed=None, retest=False):
    reasons, executed = reasons or {}, executed or {}
    rows = []
    for name, judged in verdicts(lanes):
        count = executed.get(name)
        rows.append(f"| {name} | {judged} | {'-' if count is None else count} | {lane_label(judged, reasons.get(name, ''), retest)} |")
    header = ["## sim-gate", "", f"Result: **{verdict}**", "", "| lane | verdict | executed | lane result |", "|---|---|---|---|"]
    return "\n".join(header + rows) + "\n"


def text(value, pattern, name):
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise Refused(name)
    return value


def state(lanes, reasons, executed, retest, verdict, stack, retest_of, inputs, env):
    if len(lanes) > MAX_LANES:
        raise Refused("lanes")
    if set(inputs) != set(INPUT_FIELDS):
        raise Refused("inputs")
    attempt = text(env.get("GITHUB_RUN_ATTEMPT", ""), ATTEMPT, "run_attempt")
    document = {
        "schema": SCHEMA,
        "stack": text(stack, STACK, "stack"),
        "run_id": text(env.get("GITHUB_RUN_ID", ""), RUN_ID, "run_id"),
        "run_attempt": int(attempt),
        "retest": retest,
        "retest_of": text(retest_of, f"({RUN_ID})?", "retest_of"),
        "result": verdict,
        "retest_lanes": retest_lanes(lanes),
        "inputs": {key: text(inputs[key], pattern, f"inputs.{key}") for key, pattern in INPUT_FIELDS.items()},
        "lanes": [
            {"name": name, "verdict": judged, "reason": reasons.get(name, ""), "executed": executed.get(name)}
            for name, judged in verdicts(lanes)
        ],
    }
    if retest != (retest_of != "" or int(attempt) != 1):
        raise Refused("retest")
    data = json.dumps(document, indent=2).encode("utf-8") + b"\n"
    if len(data) > MAX_STATE_BYTES:
        raise Refused("size")
    return data


def unique(pairs, what):
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError(f"a {what} is given twice")
    return dict(pairs)


def append(variable, line):
    path = os.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)


def main(argv):
    parser = argparse.ArgumentParser(prog="gate")
    parser.add_argument("--lane", action="append", default=[], type=parse_lane)
    parser.add_argument("--reason", action="append", default=[], type=parse_reason)
    parser.add_argument("--executed", action="append", default=[], type=parse_executed)
    parser.add_argument("--input", action="append", default=[], type=parse_input)
    parser.add_argument("--retest", action="store_true")
    parser.add_argument("--retest-of", default="")
    parser.add_argument("--stack", default="")
    parser.add_argument("--verdict-file")
    args = parser.parse_args(argv)
    if not args.lane:
        parser.error("at least one --lane is required")
    try:
        unique(args.lane, "lane")
        reasons = unique(args.reason, "reason")
        executed = unique(args.executed, "executed count")
        inputs = unique(args.input, "input")
    except ValueError as exc:
        parser.error(str(exc))
    names = {name for name, _ in args.lane}
    if not set(reasons) <= names or not set(executed) <= names:
        parser.error("a reason or executed count names a lane that was not given")
    verdict = overall(args.lane, reasons, args.retest)
    if args.verdict_file:
        try:
            data = state(args.lane, reasons, executed, args.retest, verdict, args.stack, args.retest_of, inputs, os.environ)
        except Refused as exc:
            print(f"::error::verdict.json refused: {exc}")
            print(f"gate: {RED_TEST}")
            return 1
        Path(args.verdict_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.verdict_file).write_bytes(data)
    append("GITHUB_STEP_SUMMARY", summary(args.lane, verdict, reasons, executed, args.retest))
    append("GITHUB_OUTPUT", f"retest_lanes={','.join(retest_lanes(args.lane))}\n")
    print(f"gate: {verdict}")
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

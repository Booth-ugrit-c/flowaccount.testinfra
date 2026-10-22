from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error

from core.comment import PRIOR_ATTEMPT, RUN_ID, SHA, Refused, call_json, text, verified_state
from core.gate import BUILD, LANE_NAME, PASS
from core.retest_sim import NoDispatch, WORKFLOW, check_run
from core.route import REPOSITORY, api_base

DEFAULT_WAIT_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 10.0


def parse_selected(raw):
    name, separator, value = raw.partition("=")
    if not separator or not LANE_NAME.fullmatch(name) or name == BUILD or value not in ("true", "false"):
        raise ValueError(f"selected must look like LANE=true or LANE=false, got {raw!r}")
    return name, value == "true"


def first_attempt(base, repository, token, run_id, wait, poll, sleep, clock):
    deadline = clock() + wait
    while True:
        run = call_json("GET", f"{base}/repos/{repository}/actions/runs/{run_id}/attempts/{PRIOR_ATTEMPT}", token)
        if not isinstance(run, dict):
            raise Refused("previous run")
        if run.get("status") == "completed":
            return run
        if clock() >= deadline:
            raise Refused("previous run not completed")
        sleep(poll)


def check(env, run_id, workflow, stack, selected, wait, poll, sleep=time.sleep, clock=time.monotonic):
    text(run_id, RUN_ID, "retest_of")
    text(workflow, WORKFLOW, "workflow")
    head = text(env.get("GITHUB_SHA", ""), SHA, "head")
    token, repository, base = env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), api_base(env)
    if not token or base is None or not REPOSITORY.match(repository):
        raise Refused("environment")
    if run_id == env.get("GITHUB_RUN_ID"):
        raise Refused("a run cannot retest itself")
    run = first_attempt(base, repository, token, run_id, wait, poll, sleep, clock)
    try:
        check_run(run, run_id, workflow)
    except NoDispatch as exc:
        raise Refused(str(exc)) from None
    if run.get("head_sha") != head:
        raise Refused("previous run tested another commit")
    state = verified_state(base, repository, token, run_id, PRIOR_ATTEMPT)
    if state["retest_of"] or state["retest"]:
        raise Refused("previous run is itself a retest")
    if state["stack"] != stack or state["inputs"]["pr_sha"] != head:
        raise Refused("previous verdict is for another stack or commit")
    verdicts = {lane["name"]: lane["verdict"] for lane in state["lanes"]}
    if set(verdicts) != {BUILD, *selected}:
        raise Refused("previous run selected other lanes")
    prior = {}
    for name, chosen in selected.items():
        if not chosen and verdicts[name] != PASS:
            raise Refused(f"lane {name} is not rerun and did not pass")
        if not chosen:
            prior[name] = PASS
    if not {lane for lane in state["retest_lanes"] if lane != BUILD} <= {name for name, chosen in selected.items() if chosen}:
        raise Refused("a lane to retest is not selected")
    return prior


def main(argv):
    parser = argparse.ArgumentParser(prog="prior")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("check")
    command.add_argument("--run-id", required=True)
    command.add_argument("--workflow", required=True)
    command.add_argument("--stack", required=True)
    command.add_argument("--selected", action="append", default=[], type=parse_selected)
    command.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    command.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    selected = dict(args.selected)
    if not selected or len(selected) != len(args.selected):
        parser.error("every lane must be given once with --selected")
    try:
        prior = check(os.environ, args.run_id, args.workflow, args.stack, selected, args.wait, args.poll)
    except Refused as exc:
        print(f"prior: refused: {exc}")
        return 1
    except Exception as exc:
        code = f" {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
        print(f"prior: refused: {type(exc).__name__}{code}")
        return 1
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.writelines(f"prior_{name}={verdict}\n" for name, verdict in prior.items())
    print(f"prior: verified run {args.run_id} attempt {PRIOR_ATTEMPT}; not rerun: {','.join(prior) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
from urllib.parse import quote

from core.classify import TRIPWIRE_REASON
from core.comment import BRANCH, GATE_RUNNING, RUN_ID, SHA, Refused, call, call_json, fetch_state, gate_agrees, gate_conclusion, text
from core.gate import BUILD
from core.route import REPOSITORY, api_base

WORKFLOW = r"[A-Za-z0-9._-]{1,100}\.ya?ml"
TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
DEFAULT_WAIT_SECONDS = 900.0
DEFAULT_POLL_SECONDS = 15.0


class NoDispatch(Exception):
    pass


def wait_for_run(base, repository, token, run_id, wait, poll, sleep, clock):
    deadline = clock() + wait
    while True:
        run = call_json("GET", f"{base}/repos/{repository}/actions/runs/{run_id}", token)
        if not isinstance(run, dict):
            raise Refused("run")
        if run.get("status") == "completed":
            return run
        if clock() >= deadline:
            raise NoDispatch(f"run {run_id} not completed within {wait:g}s")
        sleep(poll)


def wait_for_gate(base, repository, token, run_id, wait, poll, sleep, clock):
    deadline = clock() + wait
    while True:
        try:
            return gate_conclusion(base, repository, token, run_id)
        except Refused as exc:
            if str(exc) != GATE_RUNNING:
                raise
        if clock() >= deadline:
            raise NoDispatch(f"gate job of run {run_id} not completed within {wait:g}s")
        sleep(poll)


def check_run(run, run_id, workflow):
    if run.get("id") != int(run_id):
        raise Refused("run id")
    path = run.get("path")
    if type(path) is not str or path.split("@", 1)[0] != f".github/workflows/{workflow}":
        raise NoDispatch("run is not from the workflow")
    if run.get("event") != "workflow_dispatch":
        raise NoDispatch("run was not dispatched")
    if run.get("conclusion") != "failure":
        raise NoDispatch(f"run conclusion is {run.get('conclusion')!r:.40}")
    text(run.get("head_sha"), SHA, "head_sha")
    text(run.get("created_at"), TIMESTAMP, "created_at")
    return text(run.get("head_branch"), BRANCH, "head_branch")


def retest_lanes(state):
    if state["retest_of"] or state["retest"]:
        raise NoDispatch("the run is already a retest")
    lanes = state["lanes"]
    if any(lane["verdict"] == "test" for lane in lanes):
        raise NoDispatch("a lane failed with test")
    if any(lane["reason"] == TRIPWIRE_REASON for lane in lanes):
        raise NoDispatch("cache tripwire")
    infra = [lane["name"] for lane in lanes if lane["verdict"] == "infra"]
    if not infra:
        raise NoDispatch("no infra lane")
    rerunnable = {lane["name"] for lane in lanes if lane["verdict"] in ("infra", "none")}
    chosen = state["retest_lanes"]
    if not set(chosen) <= rerunnable or not set(infra) <= set(chosen):
        raise NoDispatch("retest_lanes disagree with the lane verdicts")
    return chosen


def already_retested(base, repository, token, workflow, branch, run):
    query = f"branch={quote(branch, safe='')}&event=workflow_dispatch&per_page=100&created=%3E%3D{run['created_at']}"
    listing = call_json("GET", f"{base}/repos/{repository}/actions/workflows/{workflow}/runs?{query}", token)
    runs = listing["workflow_runs"]
    if not isinstance(runs, list) or listing.get("total_count") != len(runs):
        raise NoDispatch("run listing incomplete")
    pattern = re.compile(rf"\bretest of {run['id']}\b", re.IGNORECASE)
    return any(isinstance(r, dict) and pattern.search(str(r.get("display_title", ""))) for r in runs)


def branch_tip(base, repository, token, branch):
    document = call_json("GET", f"{base}/repos/{repository}/branches/{quote(branch, safe='')}", token)
    return text(document["commit"]["sha"], SHA, "branch tip")


def run(env, run_id, workflow, wait, poll, sleep=time.sleep, clock=time.monotonic):
    text(run_id, RUN_ID, "run id")
    text(workflow, WORKFLOW, "workflow")
    token, repository, base = env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), api_base(env)
    if not token or base is None or not REPOSITORY.match(repository):
        raise Refused("environment")
    gate_run = wait_for_run(base, repository, token, run_id, wait, poll, sleep, clock)
    branch = check_run(gate_run, run_id, workflow)
    state = fetch_state(base, repository, token, run_id)
    if state["run_id"] != run_id:
        raise Refused("verdict run id")
    gate_agrees(state, wait_for_gate(base, repository, token, run_id, wait, poll, sleep, clock))
    lanes = [lane for lane in retest_lanes(state) if lane != BUILD]
    if not lanes:
        raise NoDispatch("no test lane to retest")
    inputs = state["inputs"]
    if inputs["pr_sha"] != gate_run["head_sha"]:
        raise NoDispatch("verdict commit differs from the run commit")
    if branch_tip(base, repository, token, branch) != gate_run["head_sha"]:
        raise NoDispatch("head moved")
    if already_retested(base, repository, token, workflow, branch, gate_run):
        raise NoDispatch(f"run {run_id} already has a retest")
    payload = {
        "ref": branch,
        "inputs": {
            "pr_number": inputs["pr_number"],
            "pr_branch": branch,
            "pr_sha": gate_run["head_sha"],
            "pr_author": inputs["pr_author"],
            "base": inputs["base_sha"],
            "stack": state["stack"],
            "lanes": ",".join(lanes),
            "cache": inputs["cache"],
            "fault": inputs["fault"],
            "retest_of": run_id,
        },
    }
    call("POST", f"{base}/repos/{repository}/actions/workflows/{workflow}/dispatches", token, payload)
    return f"dispatched {workflow} on {branch} retest_of={run_id} lanes={','.join(lanes)}"


def main(argv):
    parser = argparse.ArgumentParser(prog="retest_sim")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("run")
    command.add_argument("--run-id", required=True)
    command.add_argument("--workflow", required=True)
    command.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    command.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    try:
        print(f"retest_sim: {run(os.environ, args.run_id, args.workflow, args.wait, args.poll)}")
    except (NoDispatch, Refused) as exc:
        print(f"retest_sim: no dispatch: {exc}")
    except Exception as exc:
        code = f" {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
        print(f"retest_sim: no dispatch: {type(exc).__name__}{code}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

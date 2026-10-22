from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

from core.route import MAX_BODY_BYTES, REPOSITORY, TIMEOUT_SECONDS, NoRedirect, allowed_url, api_base

BOT_TYPE = "Bot"
BOT_LOGIN = "github-actions[bot]"
ARTIFACT = "sim-gate-verdict"
STATE_FILE = "verdict.json"
MAX_COMMENT_CHARS = 65000
MAX_STATE_BYTES = 65536
MAX_ZIP_BYTES = 1 << 20
MAX_LANES = 16
MAX_PAGES = 10
PER_PAGE = 100
TRUNCATED = "\n\n_Truncated: the comment reached the size limit._"

MARKER = r"sim-gate:[a-z0-9-]{1,32}"
PR_NUMBER = r"[1-9][0-9]{0,9}"
RUN_ID = r"[1-9][0-9]{0,19}"
SHA = r"[0-9a-f]{40}"
BRANCH = r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}"
TOP_FIELDS = {
    "stack": r"[a-z0-9-]{1,32}",
    "run_id": RUN_ID,
    "retest_of": f"({RUN_ID})?",
    "result": r"pass|red \[test\]|red \[infra failed [12]x\]|red \[infra: [A-Za-z0-9 _-]{1,40}\]",
}
INPUT_FIELDS = {
    "pr_number": f"({PR_NUMBER})?",
    "pr_branch": BRANCH,
    "pr_sha": SHA,
    "pr_author": r"([A-Za-z0-9-]{1,39}(\[bot\])?)?",
    "base_sha": SHA,
    "cache": r"use|off",
    "fault": r"[a-z0-9_]{0,32}",
}
LANE_FIELDS = {
    "name": r"[A-Za-z0-9._-]{1,32}",
    "verdict": r"pass|test|infra|none",
    "reason": r"[A-Za-z0-9 _-]{0,40}",
}
COUNT = r"[0-9]{1,9}"
ATTEMPT = r"[1-9][0-9]{0,3}"
TOP_KEYS = {"schema", "run_attempt", "retest", "retest_lanes", "inputs", "lanes", *TOP_FIELDS}


class Refused(ValueError):
    pass


def text(value, pattern, name):
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise Refused(name)
    return value


def number(value, pattern, name):
    if type(value) is not int or not re.fullmatch(pattern, str(value)):
        raise Refused(name)
    return value


def exact_keys(document, keys, name):
    if not isinstance(document, dict) or set(document) != set(keys):
        raise Refused(name)


def parse_lane(lane):
    exact_keys(lane, {"executed", *LANE_FIELDS}, "lane")
    for key, pattern in LANE_FIELDS.items():
        text(lane[key], pattern, f"lanes.{key}")
    if lane["executed"] is not None:
        number(lane["executed"], COUNT, "lanes.executed")
    return lane


def parse_state(raw):
    if len(raw) > MAX_STATE_BYTES:
        raise Refused("size")
    state = json.loads(raw)
    exact_keys(state, TOP_KEYS, "keys")
    if type(state["schema"]) is not int or state["schema"] != 1:
        raise Refused("schema")
    for key, pattern in TOP_FIELDS.items():
        text(state[key], pattern, key)
    number(state["run_attempt"], ATTEMPT, "run_attempt")
    if type(state["retest"]) is not bool:
        raise Refused("retest")
    if state["retest"] != (state["retest_of"] != "" or state["run_attempt"] != 1):
        raise Refused("retest")
    exact_keys(state["inputs"], INPUT_FIELDS, "inputs")
    for key, pattern in INPUT_FIELDS.items():
        text(state["inputs"][key], pattern, f"inputs.{key}")
    lanes = state["lanes"]
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= MAX_LANES:
        raise Refused("lanes")
    names = [parse_lane(lane)["name"] for lane in lanes]
    if len(set(names)) != len(names):
        raise Refused("lanes.name")
    retest_lanes = state["retest_lanes"]
    if not isinstance(retest_lanes, list):
        raise Refused("retest_lanes")
    if len(set(retest_lanes)) != len(retest_lanes) or not set(retest_lanes) <= set(names):
        raise Refused("retest_lanes")
    return state


def call(method, url, token, payload=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sim-gate-report"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        if not 200 <= response.status < 300:
            raise ValueError("status")
        return response.read(MAX_BODY_BYTES)


def call_json(method, url, token, payload=None):
    return json.loads(call(method, url, token, payload))


def download(url, limit):
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "sim-gate-report"})
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError("status")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise Refused("artifact size")
    return data


def fetch_state(base, repository, token, run_id):
    text(run_id, RUN_ID, "run id")
    listing = call_json("GET", f"{base}/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&name={ARTIFACT}", token)
    live = [a for a in listing["artifacts"] if isinstance(a, dict) and a.get("name") == ARTIFACT and a.get("expired") is False]
    if len(live) != 1:
        raise Refused("artifact count")
    artifact_id = number(live[0].get("id"), RUN_ID, "artifact id")
    size = live[0].get("size_in_bytes")
    if type(size) is not int or not 0 < size <= MAX_ZIP_BYTES:
        raise Refused("artifact size")
    try:
        archive = call("GET", f"{base}/repos/{repository}/actions/artifacts/{artifact_id}/zip", token)
    except urllib.error.HTTPError as redirect:
        if redirect.code not in (301, 302, 307):
            raise
        location = redirect.headers.get("Location", "")
        if not allowed_url(location):
            raise Refused("artifact location") from None
        archive = download(location, MAX_ZIP_BYTES)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle, bundle.open(STATE_FILE) as member:
        return parse_state(member.read(MAX_STATE_BYTES + 1))


def cell(value):
    return re.sub(r"\r\n|\r|\n", " ", str(value).replace("|", "\\|"))


def merged_rows(state, run_url, prior, prior_url):
    current = {lane["name"]: (lane, f"[this run]({run_url})") for lane in state["lanes"]}
    rows = []
    for lane in prior["lanes"] if prior else []:
        if lane["name"] in current:
            rows.append(current.pop(lane["name"]))
        else:
            rows.append((lane, f"[previous run]({prior_url})"))
    return rows + list(current.values())


def row(lane, source):
    executed = "-" if lane["executed"] is None else lane["executed"]
    reason = lane["reason"] or "-"
    return "| " + " | ".join(cell(part) for part in (lane["name"], lane["verdict"], executed, reason, source)) + " |"


def cap(body, limit=MAX_COMMENT_CHARS):
    if len(body) <= limit:
        return body
    return body[: limit - len(TRUNCATED)] + TRUNCATED


def render(marker, state, run_url, prior=None, prior_url="", note=""):
    title = f"### sim-gate {state['stack']}" + (" (Retest)" if state["retest"] else "")
    lines = [f"<!-- {marker} -->", title, "", f"Result: **{cell(state['result'])}**", ""]
    lines += ["| Lane | Verdict | Executed | Reason | Run |", "|---|---|---|---|---|"]
    lines += [row(lane, source) for lane, source in merged_rows(state, run_url, prior, prior_url)]
    if note:
        lines += ["", note]
    lines += ["", f"[Run log]({run_url})"]
    return cap("\n".join(lines) + "\n")


def is_own(comment, header):
    if not isinstance(comment, dict) or not isinstance(comment.get("user"), dict):
        return False
    user = comment["user"]
    body = comment.get("body")
    return user.get("type") == BOT_TYPE and user.get("login") == BOT_LOGIN and isinstance(body, str) and body.startswith(header)


def own_comment_id(base, repository, token, pr, header):
    found = []
    for page in range(1, MAX_PAGES + 1):
        comments = call_json("GET", f"{base}/repos/{repository}/issues/{pr}/comments?per_page={PER_PAGE}&page={page}", token)
        if not isinstance(comments, list):
            raise Refused("comments")
        found += [number(c.get("id"), RUN_ID, "comment id") for c in comments if is_own(c, header)]
        if len(comments) < PER_PAGE:
            break
    return min(found) if found else None


def prior_state(base, repository, token, state):
    try:
        prior = fetch_state(base, repository, token, state["retest_of"])
    except Exception as exc:
        print(f"comment: previous run unreadable: {type(exc).__name__}", file=sys.stderr)
        return None, "Rows of the previous run could not be read."
    same = prior["run_id"] == state["retest_of"] and prior["stack"] == state["stack"]
    if not same or prior["inputs"]["pr_sha"] != state["inputs"]["pr_sha"]:
        return None, "Rows of the previous run are not shown: it tested a different commit or stack."
    return prior, ""


def upsert(env, pr, marker, state_path, run_url):
    token, repository, base = env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), api_base(env)
    if not token or base is None or not REPOSITORY.match(repository):
        raise Refused("environment")
    text(pr, PR_NUMBER, "pr")
    text(marker, MARKER, "marker")
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if not server.startswith("https://"):
        raise Refused("server")
    text(run_url, re.escape(f"{server}/{repository}/actions/runs/") + f"{RUN_ID}(/attempts/{ATTEMPT})?", "run url")
    with open(state_path, "rb") as handle:
        state = parse_state(handle.read(MAX_STATE_BYTES + 1))
    if marker != f"sim-gate:{state['stack']}":
        raise Refused("marker")
    if pr != state["inputs"]["pr_number"]:
        raise Refused("pr")
    prior, note = (None, "")
    if state["retest_of"]:
        prior, note = prior_state(base, repository, token, state)
    prior_url = f"{server}/{repository}/actions/runs/{state['retest_of']}"
    body = render(marker, state, run_url, prior, prior_url, note)
    target = own_comment_id(base, repository, token, pr, f"<!-- {marker} -->")
    if target is None:
        call("POST", f"{base}/repos/{repository}/issues/{pr}/comments", token, {"body": body})
        return f"created comment on #{pr}"
    call("PATCH", f"{base}/repos/{repository}/issues/comments/{target}", token, {"body": body})
    return f"updated comment {target} on #{pr}"


def main(argv):
    parser = argparse.ArgumentParser(prog="comment")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("upsert")
    command.add_argument("--pr", required=True)
    command.add_argument("--marker", required=True)
    command.add_argument("--state", required=True)
    command.add_argument("--run-url", required=True)
    args = parser.parse_args(argv)
    try:
        print(f"comment: {upsert(os.environ, args.pr, args.marker, args.state, args.run_url)}")
    except Refused as exc:
        print(f"comment: refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        code = f" {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
        print(f"comment: failed: {type(exc).__name__}{code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from core import retest_sim

ROOT = Path(__file__).resolve().parents[2]
REPO = "o/r"
WORKFLOW = "sim-gate.yml"
TOKEN = "tok-SENTINEL-9d2e"
SHA_A = "a" * 40
SHA_B = "b" * 40
DISPATCH = f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"


def zipped(document):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("verdict.json", json.dumps(document))
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self):
        self.runs = {}
        self.artifacts = {}
        self.listings = {}
        self.workflow_runs = []
        self.fail = {}
        self.requests = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else None
                parent.requests.append((method, self.path, payload, dict(self.headers)))
                status, body, headers = parent.route(method, self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.answer("GET")

            def do_POST(self):
                self.answer("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, raw_path):
        path = urlsplit(raw_path).path
        for (fail_method, prefix), status in self.fail.items():
            if method == fail_method and path.startswith(prefix):
                return status, b"{}", {}
        if path == DISPATCH and method == "POST":
            return 204, b"", {}
        if path == f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs":
            return 200, json.dumps({"total_count": len(self.workflow_runs), "workflow_runs": self.workflow_runs}).encode(), {}
        if match := re.fullmatch(rf"/repos/{REPO}/actions/runs/(\d+)", path):
            sequence = self.runs.get(match.group(1))
            if not sequence:
                return 404, b"{}", {}
            current = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return 200, json.dumps(current).encode(), {}
        if match := re.fullmatch(rf"/repos/{REPO}/actions/runs/(\d+)/artifacts", path):
            run_id = match.group(1)
            listed = []
            if run_id in self.listings:
                listed = self.listings[run_id]
            elif run_id in self.artifacts:
                size = len(self.artifacts[run_id])
                listed = [{"id": int(run_id) + 5000, "name": "sim-gate-verdict", "expired": False, "size_in_bytes": size}]
            return 200, json.dumps({"total_count": len(listed), "artifacts": listed}).encode(), {}
        if match := re.fullmatch(rf"/repos/{REPO}/actions/artifacts/(\d+)/zip", path):
            return 302, b"", {"Location": f"{self.url}/blob/{int(match.group(1)) - 5000}"}
        if match := re.fullmatch(r"/blob/(\d+)", path):
            return 200, self.artifacts[match.group(1)], {}
        return 404, b"{}", {}

    def dispatches(self):
        return [payload for method, path, payload, _ in self.requests if method == "POST" and path == DISPATCH]

    def polls(self, run_id):
        return [p for m, p, _, _ in self.requests if m == "GET" and urlsplit(p).path == f"/repos/{REPO}/actions/runs/{run_id}"]


def lane(name, verdict, reason="", executed=None):
    return {"name": name, "verdict": verdict, "reason": reason, "executed": executed}


def make_state(**changes):
    state = {
        "schema": 1,
        "stack": "dotnet",
        "run_id": "100",
        "run_attempt": 1,
        "retest": False,
        "retest_of": "",
        "result": "red [infra failed 1x]",
        "retest_lanes": ["unit"],
        "inputs": {
            "pr_number": "7",
            "pr_branch": "feature-a",
            "pr_sha": SHA_A,
            "pr_author": "someone",
            "base_sha": SHA_B,
            "cache": "use",
            "fault": "fail_health_once",
        },
        "lanes": [lane("build", "pass"), lane("unit", "infra"), lane("integration", "pass", executed=4)],
    }
    state.update(changes)
    return state


def make_run(**changes):
    run = {
        "id": 100,
        "status": "completed",
        "conclusion": "failure",
        "event": "workflow_dispatch",
        "path": f".github/workflows/{WORKFLOW}",
        "head_branch": "feature-a",
        "head_sha": SHA_A,
        "created_at": "2026-01-02T03:04:05Z",
    }
    run.update(changes)
    return run


@pytest.fixture
def github(monkeypatch):
    with FakeGitHub() as fake:
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setenv("GITHUB_API_URL", fake.url)
        fake.runs["100"] = [make_run()]
        fake.artifacts["100"] = zipped(make_state())
        yield fake


def run_tool(capsys, run_id="100", wait="2", poll="0.01"):
    code = retest_sim.main(["run", "--run-id", run_id, "--workflow", WORKFLOW, "--wait", wait, "--poll", poll])
    out, err = capsys.readouterr()
    assert code == 0
    assert TOKEN not in out + err
    return out


def test_dispatches_exactly_once_with_the_retest_inputs(github, capsys):
    out = run_tool(capsys)
    assert github.dispatches() == [
        {
            "ref": "feature-a",
            "inputs": {
                "pr_number": "7",
                "pr_branch": "feature-a",
                "pr_sha": SHA_A,
                "pr_author": "someone",
                "base": SHA_B,
                "stack": "dotnet",
                "lanes": "unit",
                "cache": "use",
                "fault": "fail_health_once",
                "retest_of": "100",
            },
        }
    ]
    assert out.strip() == "retest_sim: dispatched sim-gate.yml on feature-a retest_of=100 lanes=unit"


def test_lanes_are_the_infra_lanes_plus_lanes_never_run(github, capsys):
    state = make_state(
        retest_lanes=["build", "unit", "integration"],
        lanes=[lane("build", "infra"), lane("unit", "none"), lane("integration", "none")],
    )
    github.artifacts["100"] = zipped(state)
    run_tool(capsys)
    assert [d["inputs"]["lanes"] for d in github.dispatches()] == ["build,unit,integration"]


def test_a_dispatch_error_is_reported_once_and_never_retried(github, capsys):
    github.fail[("POST", DISPATCH)] = 500
    out = run_tool(capsys)
    assert len(github.dispatches()) == 1
    assert out.strip() == "retest_sim: no dispatch: HTTPError 500"


def no_dispatch(github, capsys, reason):
    out = run_tool(capsys)
    assert github.dispatches() == []
    assert out.startswith("retest_sim: no dispatch: ")
    assert reason in out


def test_never_dispatches_when_any_lane_is_test(github, capsys):
    github.artifacts["100"] = zipped(make_state(result="red [test]", lanes=[lane("build", "pass"), lane("unit", "infra"), lane("integration", "test", executed=4)]))
    no_dispatch(github, capsys, "a lane failed with test")


def test_never_dispatches_on_a_cache_tripwire(github, capsys):
    github.artifacts["100"] = zipped(make_state(result="red [infra: cache tripwire]", lanes=[lane("build", "pass"), lane("unit", "infra", reason="cache tripwire"), lane("integration", "pass", executed=4)]))
    no_dispatch(github, capsys, "cache tripwire")


def test_never_dispatches_when_retest_of_is_set(github, capsys):
    github.artifacts["100"] = zipped(make_state(retest=True, retest_of="99", result="red [infra failed 2x]"))
    no_dispatch(github, capsys, "already a retest")


def test_never_dispatches_for_an_in_place_rerun(github, capsys):
    github.artifacts["100"] = zipped(make_state(retest=True, run_attempt=2, result="red [infra failed 2x]"))
    no_dispatch(github, capsys, "already a retest")


def test_never_dispatches_without_an_infra_lane(github, capsys):
    github.runs["100"] = [make_run(conclusion="failure")]
    github.artifacts["100"] = zipped(make_state(result="pass", retest_lanes=[], lanes=[lane("build", "pass"), lane("unit", "pass", executed=3)]))
    no_dispatch(github, capsys, "no infra lane")


def test_never_dispatches_when_retest_lanes_leave_out_an_infra_lane(github, capsys):
    github.artifacts["100"] = zipped(make_state(retest_lanes=["integration"]))
    no_dispatch(github, capsys, "retest_lanes disagree")


@pytest.mark.parametrize("prefix", [f"/repos/{REPO}/actions/runs/100/artifacts", f"/repos/{REPO}/actions/runs/100", f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs"])
def test_an_api_error_prints_the_reason_and_never_dispatches(github, capsys, prefix):
    github.fail[("GET", prefix)] = 500
    no_dispatch(github, capsys, "HTTPError 500")


def test_never_dispatches_when_the_run_does_not_complete_in_time(github, capsys):
    github.runs["100"] = [make_run(status="in_progress")]
    out = run_tool(capsys, wait="0.3", poll="0.05")
    assert github.dispatches() == []
    assert "not completed within 0.3s" in out
    assert len(github.polls("100")) >= 3


def test_waits_for_the_run_to_complete_then_dispatches(github, capsys):
    github.runs["100"] = [make_run(status="queued", conclusion=None), make_run(status="in_progress", conclusion=None), make_run()]
    run_tool(capsys)
    assert len(github.polls("100")) == 3
    assert len(github.dispatches()) == 1


def test_ref_comes_from_the_run_api_not_from_the_artifact(github, capsys):
    state = make_state()
    state["inputs"]["pr_branch"] = "artifact-branch"
    github.artifacts["100"] = zipped(state)
    run_tool(capsys)
    [dispatch] = github.dispatches()
    assert dispatch["ref"] == "feature-a"
    assert dispatch["inputs"]["pr_branch"] == "feature-a"


def test_never_dispatches_when_the_head_moved(github, capsys):
    github.runs["100"] = [make_run(head_sha=SHA_B)]
    no_dispatch(github, capsys, "head moved")


def test_never_dispatches_twice_for_the_same_run(github, capsys):
    github.workflow_runs = [{"id": 101, "display_title": "sim-gate dotnet feature-a retest of 100 (Retest)"}]
    no_dispatch(github, capsys, "already has a retest")


def test_a_retest_of_another_run_does_not_block(github, capsys):
    github.workflow_runs = [{"id": 101, "display_title": "sim-gate dotnet feature-a retest of 1000 (Retest)"}]
    run_tool(capsys)
    assert len(github.dispatches()) == 1


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"path": ".github/workflows/other.yml"}, "not from the workflow"),
        ({"event": "push"}, "not dispatched"),
        ({"conclusion": "cancelled"}, "conclusion is 'cancelled'"),
        ({"head_branch": "-x"}, "head_branch"),
        ({"head_branch": "a..b"}, "head_branch"),
        ({"id": 101}, "run id"),
    ],
)
def test_never_dispatches_for_an_unexpected_run(github, capsys, changes, reason):
    github.runs["100"] = [make_run(**changes)]
    no_dispatch(github, capsys, reason)


def test_a_run_path_with_a_ref_suffix_is_accepted(github, capsys):
    github.runs["100"] = [make_run(path=f".github/workflows/{WORKFLOW}@refs/heads/feature-a")]
    run_tool(capsys)
    assert len(github.dispatches()) == 1


def test_never_dispatches_when_the_verdict_names_another_run(github, capsys):
    github.artifacts["100"] = zipped(make_state(run_id="99"))
    no_dispatch(github, capsys, "verdict run id")


BAD_FIELDS = [
    (("schema",), 2),
    (("stack",), "dot net"),
    (("run_id",), "100;"),
    (("run_attempt",), "1"),
    (("retest",), "false"),
    (("retest_of",), "x"),
    (("result",), "pass\n"),
    (("retest_lanes",), ["unit\n"]),
    (("inputs", "pr_number"), "7 "),
    (("inputs", "pr_branch"), "feature a"),
    (("inputs", "pr_sha"), "A" * 40),
    (("inputs", "pr_author"), "some one"),
    (("inputs", "base_sha"), "main"),
    (("inputs", "cache"), "maybe"),
    (("inputs", "fault"), "fail;rm"),
    (("lanes", 1, "name"), "unit|x"),
    (("lanes", 1, "verdict"), "flaky"),
    (("lanes", 1, "reason"), "x\ny"),
    (("lanes", 1, "executed"), -1),
    (("lanes", 1, "executed"), "3"),
]


@pytest.mark.parametrize("path, value", BAD_FIELDS, ids=[".".join(map(str, p)) for p, _ in BAD_FIELDS])
def test_every_verdict_field_is_validated(github, capsys, path, value):
    state = make_state()
    target = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    github.artifacts["100"] = zipped(state)
    out = run_tool(capsys)
    assert github.dispatches() == []
    assert out.startswith("retest_sim: no dispatch: ")


def test_an_artifact_location_off_the_allowed_schemes_is_refused(github, capsys, monkeypatch):
    original = github.route

    def route(method, raw_path):
        if urlsplit(raw_path).path.endswith("/zip"):
            return 302, b"", {"Location": "http://example.invalid/blob/100"}
        return original(method, raw_path)

    monkeypatch.setattr(github, "route", route)
    no_dispatch(github, capsys, "artifact location")


def live_artifact(size, artifact_id=5100):
    return {"id": artifact_id, "name": "sim-gate-verdict", "expired": False, "size_in_bytes": size}


def test_more_than_one_verdict_artifact_is_refused(github, capsys):
    size = len(github.artifacts["100"])
    github.listings["100"] = [live_artifact(size), live_artifact(size, 5101)]
    no_dispatch(github, capsys, "artifact count")


def test_an_oversized_artifact_is_never_downloaded(github, capsys):
    github.listings["100"] = [live_artifact((1 << 20) + 1)]
    no_dispatch(github, capsys, "artifact size")
    assert not [p for _, p, _, _ in github.requests if p.endswith("/zip")]


def test_an_oversized_verdict_file_is_refused(github, capsys):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("verdict.json", json.dumps(make_state()) + " " * 70000)
    github.artifacts["100"] = buffer.getvalue()
    no_dispatch(github, capsys, "size")


def test_bad_arguments_never_dispatch(github, capsys):
    out = run_tool(capsys, run_id="100/../1")
    assert github.requests == []
    assert out.strip() == "retest_sim: no dispatch: run id"


def test_cli_entry_point_dispatches(github):
    env = {**os.environ, "GITHUB_TOKEN": TOKEN, "GITHUB_REPOSITORY": REPO, "GITHUB_API_URL": github.url}
    result = subprocess.run(
        [sys.executable, "-m", "core.retest_sim", "run", "--run-id", "100", "--workflow", WORKFLOW, "--wait", "2", "--poll", "0.01"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "dispatched sim-gate.yml on feature-a" in result.stdout
    assert TOKEN not in result.stdout + result.stderr
    assert len(github.dispatches()) == 1
    assert parse_qs(urlsplit([p for m, p, _, _ in github.requests if "/workflows/" in p and m == "GET"][0]).query)["created"] == [">=2026-01-02T03:04:05Z"]
